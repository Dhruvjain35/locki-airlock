"""Security dispatch: who hears about an incident, over which channel, and what actually happened to each message.

SpacetimeDB's module decides incidents and their severity (Sid's rules). This only decides who is told, deterministically:

- A person alone is a soft observation there and opens no incident, so nobody outside the desk hears of it (LOCKI warns locally).
- An unknown USB device or an unapproved app while armed is the owner's to know.
- A USB device within 10 s of a person, or the owner's own panic, goes to the owner and to the authorized security desk.
- Any other rule follows its severity: high or critical for both, medium for the owner, lower for nobody.

Each incident goes to each recipient once, and again only if its severity rises. The owner's console and the security desk (a reviewer
identity, never the owner's) see incidents through their own authenticated SpacetimeDB subscriptions; this Mac cannot see what a browser
displayed, so those are reported as "published" (the row is where their subscription reads it) and "acknowledged" once SpacetimeDB says so.
What the Mac sends itself is the owner's optional phone notification, through a provider the owner set up in data/notify/phone.json (never
in Git). "accepted" means that provider's receipt and nothing more: never "delivered" or "seen". Sending runs on its own thread with
bounded retries, so local warnings and incidents never wait on it.
"""

from __future__ import annotations

import json
import logging
import queue
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .contracts import IncidentState, IncidentUpdateV1

log = logging.getLogger(__name__)

POLICY_VERSION = 1
RULE_RECIPIENTS: dict[str, tuple[str, ...]] = {
    "soft_observation": (),
    "unknown_usb": ("owner",),
    "unapproved_app": ("owner",),
    "correlated_usb_physical": ("owner", "desk"),
    "owner_panic": ("owner", "desk"),
}
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
DISPATCH_MAX_AGE_MS = 120_000  # an incident the server last changed longer ago than this (a replay after reconnecting) is recorded, never sent
RETRY_DELAYS_S = (2.0, 6.0, 15.0)  # after the first attempt; only for transient failures
MAX_INCIDENTS = 200


def recipients_for(rule_id: str | None, severity: str) -> tuple[str, ...]:
    if rule_id in RULE_RECIPIENTS:
        return RULE_RECIPIENTS[rule_id]
    rank = SEVERITY_RANK.get(severity, 0)
    return ("owner", "desk") if rank >= SEVERITY_RANK["high"] else ("owner",) if rank >= SEVERITY_RANK["medium"] else ()


class TransientError(Exception):
    """Worth trying again: the network, a timeout, a rate limit or a server error."""


class PermanentError(Exception):
    """Trying again will not help: a refused request, a bad destination or bad credentials."""


class NtfyChannel:
    """ntfy (https://ntfy.sh or a self-hosted server): a POST to the owner's private topic. Its JSON reply carries a message id, which is the
    receipt. The topic acts as the secret, so it is never logged or shown; the destination is shown by its last four characters only."""

    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str | None = None, timeout_s: float = 8.0):
        if not topic or len(topic) < 16:
            raise ValueError("an ntfy topic of at least 16 characters is needed (it is the secret)")
        if not server.startswith("https://") and not server.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("the ntfy server must use https")
        self.topic, self.server, self.token, self.timeout_s = topic, server.rstrip("/"), token, timeout_s

    def describe(self) -> dict:
        return {"provider": self.name, "destination": f"ntfy …{self.topic[-4:]}", "server": self.server}

    PRIORITIES = {"default": 3, "high": 4, "urgent": 5}

    def send(self, title: str, body: str, priority: str) -> str:
        """ntfy's JSON publish (UTF-8 title and message, numeric priority). Returns the message id ntfy assigned: its receipt."""
        import httpx

        payload = {"topic": self.topic, "title": title, "message": body, "priority": self.PRIORITIES.get(priority, 3),
                   "tags": ["rotating_light"] if priority in ("urgent", "high") else ["warning"]}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = httpx.post(f"{self.server}/", json=payload, headers=headers, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise TransientError(f"{type(exc).__name__}") from None  # never the URL: it holds the topic
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise PermanentError(f"HTTP {r.status_code}")
        try:
            message_id = str(r.json().get("id") or "")
        except ValueError:
            message_id = ""
        if not message_id:
            raise TransientError("no receipt id in the reply")
        return message_id


def load_phone_channel(path: Path):
    """The owner's phone channel from data/notify/phone.json, or None. The file holds a secret, so it must be readable by the owner only."""
    if not path.exists():
        return None, None
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return None, f"{path.name} is readable by other users; chmod 600 it"
    try:
        cfg = json.loads(path.read_text())
        if cfg.get("provider") != "ntfy":
            return None, f"unsupported provider {cfg.get('provider')!r}"
        return NtfyChannel(str(cfg["topic"]), str(cfg.get("server") or "https://ntfy.sh"), cfg.get("token") or None), None
    except (KeyError, ValueError, TypeError) as exc:
        return None, f"{path.name} is not a valid phone channel: {type(exc).__name__}"


@dataclass
class Delivery:
    recipient: str  # "owner" or "desk"
    channel: str  # "console" (their own SpacetimeDB subscription) or "phone"
    severity: str
    status: str  # console: published, acknowledged. phone: queued, sending, accepted, failed (and acknowledged once SpacetimeDB says so)
    queued_at_ms: int
    attempts: int = 0
    provider_message_id: str | None = None
    error: str | None = None
    accepted_at_ms: int | None = None
    failed_at_ms: int | None = None


@dataclass
class IncidentRecord:
    incident_id: str
    session_id: str | None
    severity: str
    rule_id: str | None
    state: str
    summary: str | None
    created_at_ms: int | None
    notified_for_severity: str | None = None
    skipped: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at_ms: int | None = None
    deliveries: list[Delivery] = field(default_factory=list)


class Dispatcher:
    def __init__(self, workstation: str, phone_config: Path, *, clock=lambda: int(time.time() * 1000), retry_delays=RETRY_DELAYS_S, start_worker: bool = True):
        self.workstation = workstation or "this workstation"
        self.phone_config = phone_config
        self._phone_stamp = self._stamp()
        self.phone, self.phone_error = load_phone_channel(phone_config)
        self.clock, self.retry_delays = clock, tuple(retry_delays)
        self.incidents: dict[str, IncidentRecord] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="security-dispatch", daemon=True) if start_worker else None
        if self._worker is not None:
            self._worker.start()

    def _stamp(self):
        try:
            st = self.phone_config.stat()
            return (st.st_mtime_ns, st.st_size, st.st_mode)
        except OSError:
            return None

    def refresh_phone(self) -> None:
        """The owner may set up the phone channel while the backend runs (scripts/airlock_notify.py): re-read it when the file changes."""
        stamp = self._stamp()
        if stamp != self._phone_stamp:
            self._phone_stamp = stamp
            self.phone, self.phone_error = load_phone_channel(self.phone_config)

    # ------------------------------------------------------------------ decisions
    def observe(self, update: IncidentUpdateV1, *, active_session_id: str | None) -> IncidentRecord | None:
        """Every incident row from SpacetimeDB comes here. Fixtures are never dispatched; replays, other sessions and acknowledgements only
        update the record."""
        if update.is_fixture:
            return None
        now = self.clock()
        with self._lock:
            rec = self.incidents.get(update.incident_id)
            if rec is None:
                rec = IncidentRecord(update.incident_id, update.session_id, update.severity.value, update.rule_id, update.state.value, update.summary, update.created_at)
                self.incidents[update.incident_id] = rec
                while len(self.incidents) > MAX_INCIDENTS:
                    self.incidents.pop(next(iter(self.incidents)))
            rec.severity, rec.state, rec.rule_id, rec.summary = update.severity.value, update.state.value, update.rule_id or rec.rule_id, update.summary or rec.summary
            if update.acknowledged_by and not rec.acknowledged_by:
                rec.acknowledged_by, rec.acknowledged_at_ms = update.acknowledged_by, update.updated_at or now
                for d in rec.deliveries:
                    if d.status in ("published", "accepted"):
                        d.status = "acknowledged"
            if update.state == IncidentState.RESOLVED:
                return rec
            if update.session_id and active_session_id and update.session_id != active_session_id:
                rec.skipped = "not the armed session"
                return rec
            if update.updated_at and now - update.updated_at > DISPATCH_MAX_AGE_MS:
                rec.skipped = "older than the dispatch window (a replay after reconnecting)"
                return rec
            rank = SEVERITY_RANK.get(rec.severity, 0)
            if rec.notified_for_severity is not None and rank <= SEVERITY_RANK.get(rec.notified_for_severity, 0):
                return rec  # already told at this severity or higher
            who = recipients_for(rec.rule_id, rec.severity)
            if not who:
                rec.skipped = "this rule notifies nobody outside the desk"
                return rec
            rec.notified_for_severity, rec.skipped = rec.severity, None
            self.refresh_phone()
            for recipient in who:
                rec.deliveries.append(Delivery(recipient, "console", rec.severity, "acknowledged" if rec.acknowledged_by else "published", now))
            if "owner" in who and self.phone is not None:
                d = Delivery("owner", "phone", rec.severity, "queued", now)
                rec.deliveries.append(d)
                self._queue.put((rec.incident_id, d))
            return rec

    def message(self, rec: IncidentRecord) -> tuple[str, str, str]:
        severity = rec.severity.capitalize()
        when = time.strftime("%H:%M:%S", time.localtime((rec.created_at_ms or self.clock()) / 1000))
        summary = (rec.summary or "Security incident while armed.").strip()
        body = f"{when} · {summary} Incident {rec.incident_id[-12:]}. Review and acknowledge it in the LOCKI console."
        priority = "urgent" if rec.severity == "critical" else "high" if rec.severity == "high" else "default"
        return f"LOCKI {severity} incident · {self.workstation}", body[:500], priority

    # ------------------------------------------------------------------ sending (worker thread)
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                incident_id, d = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.deliver(incident_id, d)

    def deliver(self, incident_id: str, d: Delivery) -> None:
        with self._lock:
            rec = self.incidents.get(incident_id)
            title, body, priority = self.message(rec) if rec else ("LOCKI incident", "Security incident.", "default")
        delays = (0.0, *self.retry_delays)
        for attempt, delay in enumerate(delays, start=1):
            if delay and self._stop.wait(delay):
                return
            with self._lock:
                d.status, d.attempts = "sending", attempt
            try:
                receipt = self.phone.send(title, body, priority)
            except TransientError as exc:
                with self._lock:
                    d.error = str(exc)[:160]
                    if attempt == len(delays):
                        d.status, d.failed_at_ms = "failed", self.clock()
                log.warning("phone notification for %s: attempt %d failed (%s)", incident_id, attempt, exc)
                continue
            except PermanentError as exc:
                with self._lock:
                    d.status, d.error, d.failed_at_ms = "failed", str(exc)[:160], self.clock()
                log.warning("phone notification for %s refused: %s", incident_id, exc)
                return
            with self._lock:
                d.status, d.provider_message_id, d.error, d.accepted_at_ms = ("acknowledged" if rec and rec.acknowledged_by else "accepted"), receipt, None, self.clock()
            return

    def close(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ reading
    def snapshot(self) -> dict:
        with self._lock:
            self.refresh_phone()
            phone = {"provider": None, "configured": False, "destination": None, "last_error": self.phone_error}
            if self.phone is not None:
                phone = {**self.phone.describe(), "configured": True, "last_error": self.phone_error}
                phone.pop("server", None) if phone.get("server") == "https://ntfy.sh" else None
            return {"policy": {"version": POLICY_VERSION, "rules": {k: "+".join(v) or "none" for k, v in RULE_RECIPIENTS.items()},
                               "other_rules": "high or critical: owner+desk; medium: owner; lower: none"},
                    "phone": phone,
                    "meaning": {"published": "in SpacetimeDB where that recipient's own authenticated subscription reads it; not proof it was displayed",
                                "accepted": "the provider's receipt; not proof the phone showed it",
                                "acknowledged": "SpacetimeDB records an acknowledgement by the owner or the authorized desk"},
                    "incidents": [{**{k: v for k, v in asdict(r).items() if k != "deliveries"}, "deliveries": [asdict(d) for d in r.deliveries]}
                                  for r in reversed(list(self.incidents.values()))][:50]}
