"""The native takeover: whether LOCKI Alert.app should cover this Mac's screen right now, derived read-only from what the SecurityService
already holds. The ModeEngine and SpacetimeDB stay the only security authorities. Nothing here arms, disarms, locks, speaks, grants,
writes evidence or changes an incident, and dismissing the window only hides it.

Active only while armed with a live session and one of:
  A  the ModeEngine is CRITICAL for an unknown USB device (unknown_usb, unknown_usb_with_person) and evidence_ids[0] is a
     usb_device_added event in the record, from this Mac's own device id, not a fixture, observed after arming, at most
     TAKEOVER_MAX_AGE_MS old
  B  the ModeEngine is CRITICAL for SpacetimeDB's correlated incident, and that incident qualifies as in C
  C  SpacetimeDB's latest incident qualifies with no local CRITICAL: not a fixture, this session, correlated (a person event and a
     device event), HIGH or CRITICAL, still open, opened at most TAKEOVER_MAX_AGE_MS ago by the server's clock (the Mac's agreed with
     it to ~0.2 s). Then local_mode_verified is false and the window says the server confirmed it, not this Mac.
Never from someone in view alone (WATCHFUL), DEFENSIVE, a camera outage, an app, another session, another device, a stale row, or a
fixture. A rehearsal fixture (the demo in rehearsal mode, its own fixture id) gives a record with live=false, rehearsal=true, and is
never active. One record per takeover: its id survives the local USB -> SpacetimeDB incident upgrade (digital_event_id is the Mac's
USB event id) and a reconnect re-delivering the incident, and an acknowledgement survives re-evaluation. Disarming or a new session
clears the record and the hand-off.

Response lines restate real records only (warnings, the tool gate, the lock adapter and its results, the monitor, investigations, and a
cached TCP connect to the investigator), and no line says contained, isolated or blocked unless macOS confirmed a screen lock.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..config import PROJECT_ROOT
from ..telemetry.events import EventType
from .contracts import (
    IncidentState,
    IncidentUpdateV1,
    LockStatus,
    SecurityEventV1,
    SecurityKind,
    SecuritySource,
    Severity,
)
from .modes import SecurityMode

log = logging.getLogger(__name__)

TAKEOVER_MAX_AGE_MS = 120_000  # the module's own window for admitting an observation; older is history, shown in the dashboard only
NATIVE_CONNECTED_MS = 2_000  # the alert window polls every 200 ms: ten missed polls and it counts as gone
PERSON_WINDOW_MS = 20_000  # a camera observation this long before the device gives the sentence its timing (service.PHYSICAL_WINDOW_S)
PROBE_TTL_S = 10.0
PROBE_TIMEOUT_S = 0.2
DEFAULT_INVESTIGATOR_URL = "http://127.0.0.1:18787"
USB_REASONS = frozenset({"unknown_usb", "unknown_usb_with_person"})
OPEN_STATES = frozenset({IncidentState.SUSPECTED_INTRUSION, IncidentState.LOCK_REQUESTED, IncidentState.SCREEN_LOCKED})
HIGH = frozenset({Severity.HIGH, Severity.CRITICAL})
VIEWS = ("explain", "evidence", "stop")
CLAIM = re.compile(r"\b(contain(?:ed|ment)|isolat(?:ed|ion)|block(?:ed|ing))\b", re.IGNORECASE)
PERSON_LABELS = {
    "person_in_zone": "Person at desk", "person_in_view": "Person near desk", "person_approaching": "Person near desk",
    "unrecognized_face": "Face not recognized as the owner", "face_uncertain": "Face could not be checked", "no_face_visible": "Face could not be checked",
    "owner_face_match": "Owner's face matched", "keyboard_interaction": "Hand on the keyboard", "hand_near_keyboard": "Hand near the keyboard",
    "laptop_interaction": "Laptop touched", "protected_device_interaction": "Protected device touched",
    "usb_zone_interaction": "Hand at the USB ports", "hand_near_usb_zone": "Hand at the USB ports",
}
CLEAR_KINDS = frozenset({"person_left_zone", "zone_clear"})
USB_WORDS = {"storage": "mass storage", "input": "input device", "audio": "audio device", "video": "video device", "network": "network adapter", "hub": "hub",
             "phone_or_camera": "phone or camera", "printer": "printer", "smart_card": "smart card reader", "wireless": "wireless adapter",
             "vendor_specific": "vendor-specific device"}
LOCK_STATES = {LockStatus.LOCK_REQUESTED.value: "requested", LockStatus.LOCK_EXECUTED.value: "executed", LockStatus.LOCK_CONFIRMED.value: "confirmed",
               LockStatus.LOCK_REFUSED.value: "refused", LockStatus.LOCK_FAILED.value: "failed", LockStatus.LOCK_DRY_RUN.value: "dry_run"}
LOCK_RANK = {"confirmed": 6, "executed": 5, "failed": 4, "requested": 3, "dry_run": 2, "refused": 1}


# ------------------------------------------------------------------ the token the native window authenticates with
def token_path(data_dir: Path | None) -> Path:
    """LOCKI_ALERT_TOKEN_FILE when set. Otherwise ~/.locki/run/alert.token for a backend on its own data folder (what LOCKI Alert.app
    reads), and <data_dir>/security/alert.token for anything started on another data folder (the tests, the check scripts), so those
    never rotate the token the real alert window holds."""
    override = os.environ.get("LOCKI_ALERT_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    if data_dir is None or Path(data_dir).resolve() == (PROJECT_ROOT / "data").resolve():
        return Path.home() / ".locki" / "run" / "alert.token"
    return Path(data_dir) / "security" / "alert.token"


def write_token(path: Path, token: str) -> None:
    """Owner-only and atomic: the folder 0700, the file 0600, written beside it and renamed over the old one."""
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if created or parent == Path.home() / ".locki" / "run":
        os.chmod(parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".alert-token-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _tcp_probe(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Only whether something accepts a connection there. No request is sent and no key is read."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ one takeover
@dataclass
class _Record:
    id: str
    live: bool
    rehearsal: bool
    detected_at_ms: int  # when this Mac received the triggering fact (its own wall clock)
    usb: SecurityEventV1 | None = None
    incident: IncidentUpdateV1 | None = None  # SpacetimeDB's, qualifying
    local_incident_id: str | None = None
    person: tuple[SecurityEventV1, int] | None = None  # the camera observation and when this Mac received it (wall ms)
    acknowledged_at_ms: int | None = None
    shown_at_ms: int | None = None
    shown_latency_ms: int | None = None

    def same(self, other: _Record) -> bool:
        """The same takeover: one id, one USB event, one incident, or the Mac's USB event and the incident built on it (either first)."""
        if self.id == other.id:
            return True
        if self.usb is not None and other.usb is not None and self.usb.event_id == other.usb.event_id:
            return True
        if self.incident is not None and other.incident is not None and self.incident.incident_id == other.incident.incident_id:
            return True
        return any(a.usb is not None and b.incident is not None and b.incident.digital_event_id == a.usb.event_id for a, b in ((self, other), (other, self)))

    @property
    def incident_id(self) -> str | None:
        return self.incident.incident_id if self.incident is not None else self.local_incident_id


def _evidence(service, event_id: str | None) -> SecurityEventV1 | None:
    if not event_id:
        return None
    return next((e for e in reversed(service.evidence) if e.event_id == event_id), None)


def _own_usb(service, event_id: str | None) -> SecurityEventV1 | None:
    """This Mac's own real USB arrival with that id, if it is in the record."""
    event = _evidence(service, event_id)
    identity = service.identity
    if event is None or event.is_fixture or event.kind != SecurityKind.USB_DEVICE_ADDED or identity is None or event.device_id != identity.device_id:
        return None
    return event


def _rehearsal_fixture(service, event_id: str | None) -> dict | None:
    run = getattr(getattr(service, "demo", None), "run", None)
    if run is None or run.mode != "rehearsal" or not event_id:
        return None
    return next((f for f in run.fixtures if f.get("event_id") == event_id), None)


def _critical_reason(modes) -> str:
    """Why the ModeEngine is CRITICAL. Its own hold says so when the last transition had another name (the owner walking away, say)."""
    reason = modes.state.mode_reason
    if reason in USB_REASONS or reason == "correlated_incident":
        return reason
    held = getattr(modes, "_critical", None)
    return held[1] if held else reason


def _usb_fact(service, session, event_id: str | None, now_ms: int) -> tuple[SecurityEventV1 | None, list[str], dict | None]:
    """Rule A on the event the ModeEngine cites: (the event if it qualifies, why not, the rehearsal fixture it is)."""
    event = _evidence(service, event_id)
    if event is None:
        fixture = _rehearsal_fixture(service, event_id)
        return None, ["fixture" if fixture is not None else "evidence_missing"], fixture
    if event.is_fixture:
        return None, ["fixture"], _rehearsal_fixture(service, event_id)
    why = []
    if event.kind != SecurityKind.USB_DEVICE_ADDED:
        why.append("evidence_missing")
    identity = service.identity
    if identity is None or event.device_id != identity.device_id:
        why.append("other_device")
    if event.observed_at < session.armed_at_ms or now_ms - event.observed_at > TAKEOVER_MAX_AGE_MS:
        why.append("stale")
    return (None if why else event), why, None


def _incident_fact(service, session, now_ms: int) -> tuple[IncidentUpdateV1 | None, list[str]]:
    """Rules B and C on SpacetimeDB's latest incident: (the incident if it qualifies, why not)."""
    update = service._last_incident_update
    if update is None:
        return None, []
    if update.is_fixture:
        return None, ["fixture"]
    why = []
    if update.session_id != session.session_id:
        why.append("other_session")
    if not (update.physical_event_id and update.digital_event_id):
        why.append("not_correlated")
    if update.severity not in HIGH:
        why.append("severity_below_high")
    if update.state not in OPEN_STATES:
        why.append("incident_not_open")
    opened = update.created_at or update.updated_at
    if opened is None or abs(now_ms - opened) > TAKEOVER_MAX_AGE_MS:
        why.append("stale")
    return (None if why else update), why


def _person(service, before_ms: int, prefer_id: str | None = None) -> tuple[SecurityEventV1, int] | None:
    """The camera observation that put a person at the desk just before ``before_ms`` (this Mac's clock), with when this Mac received it.
    Times are this Mac's receive times, never a remote camera's own clock. Nothing after someone left counts."""
    now_wall, now_mono = time.time(), time.monotonic()
    items = [(e, int((now_wall - (now_mono - t)) * 1000)) for t, e in list(service.physical)]
    if prefer_id:
        hit = next(((e, at) for e, at in items if e.event_id == prefer_id), None)
        if hit is not None:
            return hit
    found = None
    for e, at in reversed(items):
        if at > before_ms + 1000:
            continue
        if before_ms - at > PERSON_WINDOW_MS or e.kind.value in CLEAR_KINDS:
            break
        if e.kind.value == "person_in_zone":
            return e, at
        if found is None and e.kind.value in PERSON_LABELS:
            found = (e, at)
    return found


def _local_incident_id(service, usb: SecurityEventV1 | None) -> str | None:
    incident = service.incident or {}
    if usb is not None and incident.get("source") == "local" and usb.event_id in (incident.get("evidence_ids") or []):
        return incident.get("incident_id")
    return None


def candidates(service, now_ms: int, received: dict[str, int] | None = None) -> tuple[list[_Record], list[str]]:
    """What qualifies right now, most specific first, and why nothing live does. Reads the service; changes nothing."""
    session = service.session
    if session is None:
        return [], ["not_armed"]
    state = service.modes.state
    critical = state.current_security_mode == SecurityMode.CRITICAL and state.armed_status and state.session_id == session.session_id
    blocked: list[str] = [] if critical else ["no_critical_evidence"]
    usb = fixture = None
    if critical and _critical_reason(service.modes) in USB_REASONS:
        usb, why, fixture = _usb_fact(service, session, state.evidence_ids[0] if state.evidence_ids else None, now_ms)
        blocked += why
    incident, why = _incident_fact(service, session, now_ms)
    blocked += why
    out: list[_Record] = []
    if usb is not None:
        merged = incident if incident is not None and incident.digital_event_id == usb.event_id else None
        out.append(_Record(f"usb:{usb.event_id}", True, False, usb.observed_at, usb=usb, incident=merged, local_incident_id=_local_incident_id(service, usb),
                           person=_person(service, usb.observed_at, merged.physical_event_id if merged else None)))
        if merged is not None:
            incident = None
    if incident is not None:
        local = _own_usb(service, incident.digital_event_id)
        detected = local.observed_at if local is not None else (received or {}).get(incident.incident_id, now_ms)
        out.append(_Record(f"usb:{local.event_id}" if local is not None else f"incident:{incident.incident_id}", True, False, detected, usb=local, incident=incident,
                           person=_person(service, detected, incident.physical_event_id)))
    if not out and fixture is not None:
        out.append(_Record(f"usb:{fixture['event_id']}", False, True, int(float(fixture.get("at") or now_ms / 1000) * 1000)))
    return out, ([] if any(r.live for r in out) else list(dict.fromkeys(blocked)))


# ------------------------------------------------------------------ the record, in words
def _clean(text: str | None, confirmed: bool) -> str | None:
    """Text from elsewhere (the server, the OS, a speaker error) is used only if it claims nothing a confirmed lock has not earned."""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()[:240]
    return text if confirmed or not CLAIM.search(text) else None


def _line(state: str, detail: str) -> dict:
    return {"state": state, "detail": detail}


def _short(session_id: str) -> str:
    return session_id if len(session_id) <= 14 else session_id[:12] + "…"


def _who(event: SecurityEventV1) -> str:
    return "the Jetson" if event.source == SecuritySource.JETSON_VISION else "this Mac's camera"


def _person_item(event: SecurityEventV1, at_ms: int) -> dict:
    remote = event.source == SecuritySource.JETSON_VISION
    where = f"Jetson {event.device_id}" if remote else "This Mac's camera"
    conf = f" · {round(event.confidence * 100)}% confidence" if event.confidence is not None else ""
    return {"kind": "person", "label": PERSON_LABELS.get(event.kind.value, "Person seen"), "detail": where + conf, "at_ms": at_ms,
            "source": "remote_camera" if remote else "mac_camera", "event_id": event.event_id}


def _usb_item(event: SecurityEventV1) -> dict:
    m = event.metadata  # a device chooses its own name, so only the OS's category and ids are shown
    ids = f"{str(m.get('vendor_id', '0x????'))[2:]}:{str(m.get('product_id', '0x????'))[2:]}"
    return {"kind": "usb", "label": "Unknown USB device connected", "detail": f"Unknown USB {USB_WORDS.get(m.get('category'), 'device')} · {ids}",
            "at_ms": event.observed_at, "source": "mac_usb_watcher", "event_id": event.event_id}


def _audio(service, ids: set[str]) -> dict:
    records = [w for w in list(service.warnings) if w.get("event_id") in ids]
    if not records:
        return _line("unknown", "No warning was recorded for this event")
    played = next((w for w in records if w.get("played")), None)
    if played is not None:
        a = played.get("audio") or {}
        volume = getattr(service.rt, "_os_volume", None)
        if isinstance(volume, dict) and volume.get("muted") is True:
            return _line("muted", "The warning played, but this Mac's sound output is muted")
        label, ms = a.get("source_label") or a.get("source") or "Speaker", a.get("to_audio_ms")
        return _line("played", f"{label} · audio {ms:.0f} ms after detection" if isinstance(ms, (int, float)) else f"{label} · the warning played")
    if any(w.get("spoken") and w.get("audio") is None for w in records):
        return _line("pending", "The warning is queued; its audio has not finished yet")
    heard = next((w for w in records if w.get("audio") is not None), None)
    if heard is not None:
        a = heard["audio"]
        why = _clean(a.get("detail"), False)
        return _line("failed", f"No audio played ({a.get('outcome') or 'unknown outcome'}, {a.get('source_label') or a.get('source') or 'no source'})" + (f": {why}" if why else ""))
    if any(w.get("skipped") for w in records):
        return _line("skipped", "Not spoken again: the same warning played moments earlier")
    return _line("unknown", "A warning was recorded but not spoken")


def _lock(service, session, since_ms: int) -> dict:
    latest: dict[str, dict] = {}
    for r in list(service.locks):
        if r.get("session_id") == session.session_id and int((r.get("at") or 0) * 1000) >= since_ms - 2000:
            latest[r.get("action_id")] = r
    best = max(latest.values(), key=lambda r: LOCK_RANK.get(LOCK_STATES.get(r.get("status"), ""), 0), default=None)
    mode = service.lock.mode
    if best is None:
        if not session.policy.auto_lock:
            return _line("not_locked", "No lock was requested: automatic locking is off for this session")
        return _line("not_locked", "No lock was requested" + ("; locks are dry runs on this backend" if mode != "real" else ""))
    state = LOCK_STATES.get(best.get("status"), "requested")
    why = _clean(best.get("detail"), state == "confirmed")
    detail = {
        "dry_run": "Dry run: the screen was not locked",
        "requested": "A lock was requested and has not run yet",
        "executed": "The lock ran, but macOS has not confirmed the screen is locked",
        "confirmed": "macOS reports the screen as locked",
        "refused": "No lock ran" + (f": {why}" if why else ""),
        "failed": "The lock failed" + (f": {why}" if why else ""),
    }[state]
    return _line(state, detail)


def _monitoring(service) -> dict:
    mon = service.monitor
    if mon is None or not mon.running:
        return _line("stopped", "The USB and app watchers are not running")
    problems = [f"{name} watcher: {_clean(err, False) or 'reported a problem'}" for name, err in (mon.errors or {}).items() if err]
    camera = service.modes.state.camera_health
    if camera in ("obstructed", "offline", "unavailable"):
        problems.append(f"desk camera {camera}")
    if problems:
        return _line("degraded", "; ".join(problems)[:240])
    return _line("running", f"USB watcher running ({mon.usb_watcher.method}) · camera {camera} · SpacetimeDB {'connected' if service.spacetime_connected else 'not connected'}")


def _sensors(service) -> str:
    mon = service.monitor
    usb = "USB watcher stopped" if mon is None or not mon.running else "USB watcher degraded" if (mon.errors or {}).get("usb") else "USB watcher running"
    db = "SpacetimeDB connected" if service.spacetime_connected else "SpacetimeDB not connected" if service.subscriber is not None else "SpacetimeDB not configured"
    return f"Camera {service.modes.state.camera_health} · {usb} · {db}"


class TakeoverTracker:
    """Remembers the current takeover record, its acknowledgement, when it was shown, the last hand-off and the native window's
    heartbeat. Everything else is derived again on every read. It never writes to the SecurityService."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.token_file: Path | None = None
        self.record: _Record | None = None
        self.handoff: dict | None = None
        self.native: dict = {"last_poll_ms": None, "pid": None, "version": None}
        self._session: str | None = None
        self._received: dict[str, int] = {}  # SpacetimeDB incident id -> when this Mac first received it (wall ms)
        self._probe: tuple[tuple[str, int], float, bool] | None = None
        self._workstation: tuple[float, str | None] | None = None

    # ------------------------------------------------------------------ the token
    def start(self, data_dir: Path | None) -> None:
        """At backend start: a new token, written owner-only for the alert window. A failure is logged; the endpoints then refuse everyone."""
        self.token = secrets.token_hex(32)
        path = token_path(data_dir)
        try:
            write_token(path, self.token)
            self.token_file = path
            log.info("native alert token written to %s", path)
        except OSError as exc:
            self.token_file = None
            log.warning("could not write the native alert token to %s: %s", path, exc)

    def token_ok(self, given: str | None) -> bool:
        if not self.token or not given:
            return False
        return hmac.compare_digest(given.encode("utf-8", "replace"), self.token.encode())

    # ------------------------------------------------------------------ inputs
    def on_bus_event(self, ev) -> None:
        """apply_incident_update publishes each SpacetimeDB row as it arrives, so this is when this Mac received it."""
        if ev.event_type != EventType.SECURITY_INCIDENT:
            return
        p = ev.payload or {}
        iid = p.get("incident_id")
        if p.get("source") in ("spacetimedb", "fixture") and isinstance(iid, str) and iid not in self._received:
            self._received[iid] = int(ev.timestamp * 1000)
            while len(self._received) > 256:
                self._received.pop(next(iter(self._received)))

    def heard_from_native(self, pid: str | None, version: str | None, now_ms: int | None = None) -> None:
        pid = (pid or "").strip()
        self.native = {"last_poll_ms": int(time.time() * 1000) if now_ms is None else now_ms, "pid": int(pid) if pid.isdigit() and len(pid) <= 9 else None,
                       "version": re.sub(r"[^A-Za-z0-9._+-]", "", version or "")[:32] or None}

    # ------------------------------------------------------------------ the decision
    def decide(self, service, now_ms: int | None = None) -> dict:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        session = service.session
        sid = None if session is None else session.session_id
        if sid != self._session:  # disarmed, or another session: nothing from the last one carries over
            self._session, self.record, self.handoff = sid, None, None
        found, blocked = candidates(service, now_ms, self._received)
        if found:
            self._keep(found)
        active = any(r.live for r in found)
        return {
            "now_ms": now_ms,
            "armed": session is not None,
            "session_id": sid,
            "workstation_id": self._workstation_id(service),
            "active": active,
            "takeover": None if self.record is None or session is None else self._render(service, session, self.record, now_ms),
            "blocked": blocked,
            "native": {"connected": self.native["last_poll_ms"] is not None and now_ms - self.native["last_poll_ms"] <= NATIVE_CONNECTED_MS, **self.native},
            "handoff": None if self.handoff is None else dict(self.handoff),
        }

    def view(self, service) -> dict:
        """For GET /api/security: the same decision, and never an exception that would take the dashboard's state with it."""
        try:
            return self.decide(service)
        except Exception as exc:
            log.exception("takeover decision failed")
            return {"armed": service.session is not None, "active": False, "takeover": None, "blocked": ["error"], "native": None, "handoff": None,
                    "error": f"{type(exc).__name__}: {exc}"[:200]}

    def _keep(self, found: list[_Record]) -> None:
        rec = self.record
        same = next((c for c in found if rec is not None and rec.same(c)), None)
        if same is not None:
            if same.live or not rec.live:  # one id: the incident upgrades it, a reconnect re-delivers it, the acknowledgement stays
                rec.usb = rec.usb or same.usb
                rec.incident = same.incident or rec.incident
                rec.local_incident_id = same.local_incident_id or rec.local_incident_id
                rec.person = rec.person or same.person
                rec.detected_at_ms = min(rec.detected_at_ms, same.detected_at_ms)
            return
        first = found[0]
        if rec is not None and rec.live and not first.live:
            return  # a rehearsal sample never replaces a real takeover
        self.record = first

    # ------------------------------------------------------------------ reports from the native window (never security state)
    def report(self, takeover_id: str, event: str, *, view: str | None = None, at_ms: int | None = None, latency_ms: int | None = None, now_ms: int | None = None) -> dict:
        rec = self.record
        if rec is None or takeover_id != rec.id:
            raise LookupError("not the current takeover")
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        at = at_ms if isinstance(at_ms, int) and at_ms > 0 else now_ms
        if event == "shown":
            if rec.shown_at_ms is None:
                rec.shown_at_ms = at
                rec.shown_latency_ms = latency_ms if isinstance(latency_ms, int) else max(0, at - rec.detected_at_ms)
        elif event == "dismissed":
            if rec.acknowledged_at_ms is None:
                rec.acknowledged_at_ms = at
        elif event == "handoff":
            if view not in VIEWS:
                raise ValueError("a handoff names its view: explain, evidence or stop")
            self.handoff = {"nonce": secrets.token_hex(8), "view": view, "takeover_id": rec.id, "incident_id": rec.incident_id, "at_ms": at}
        else:
            raise ValueError("event is shown, dismissed or handoff")
        return {"ok": True, "takeover_id": rec.id, "event": event, "acknowledged_at_ms": rec.acknowledged_at_ms, "shown_at_ms": rec.shown_at_ms,
                "handoff": None if self.handoff is None else dict(self.handoff)}

    # ------------------------------------------------------------------ rendering
    def _workstation_id(self, service) -> str | None:
        config = getattr(service.subscriber, "config", None)
        if getattr(config, "workstation_id", None):
            return config.workstation_id
        if service.rt.settings.spacetime_workstation:
            return service.rt.settings.spacetime_workstation
        now = time.monotonic()
        if self._workstation is None or now - self._workstation[0] > PROBE_TTL_S:  # spacetime_settings reads a file: at most every 10 s
            found = service.spacetime_settings()
            self._workstation = (now, None if found is None else found.get("workstation_id"))
        return self._workstation[1]

    def _investigator(self) -> tuple[bool | None, str]:
        url = os.environ.get("LOCKI_INVESTIGATOR_URL") or DEFAULT_INVESTIGATOR_URL
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return None, "an unreadable address"
        if not host:
            return None, "an unreadable address"
        now = time.monotonic()
        if self._probe is None or self._probe[0] != (host, port) or now - self._probe[1] > PROBE_TTL_S:
            self._probe = ((host, port), now, _tcp_probe(host, port))
        return self._probe[2], f"{host}:{port}"

    def _nemotron(self, service, incident_ids: set[str]) -> dict:
        task = service._investigating
        if task is not None and not task.done():
            return _line("investigating", "Nemotron is reviewing the record now")
        rt_task, case = getattr(service.rt, "_nemotron_task", None), getattr(service.rt, "_nemotron_case", None)
        if rt_task is not None and not rt_task.done() and case and case[0] in incident_ids:
            return _line("investigating", "Nemotron is reviewing this incident now")
        last = service.investigations[-1] if service.investigations else None
        if isinstance(last, dict) and last.get("incident_id") in incident_ids:
            if last.get("status") == "completed":
                return _line("answered", f"Answered by {last.get('model_id') or 'the investigation model'}" + (f" in {last['total_ms']:.0f} ms" if isinstance(last.get("total_ms"), (int, float)) else ""))
            return _line("failed", f"The investigation did not finish ({last.get('status') or 'no status'})")
        reachable, where = self._investigator()
        if reachable is None:
            return _line("unknown", f"The investigator is configured at {where}")
        return _line("available", f"The investigator answers at {where}; not asked yet") if reachable else _line("unavailable", f"Nothing answers at {where}")

    def _render(self, service, session, rec: _Record, now_ms: int) -> dict:
        state = service.modes.state
        cited = {i for i in (rec.usb.event_id if rec.usb else None, rec.incident.incident_id if rec.incident else None, rec.id.split(":", 1)[1]) if i}
        verified = state.current_security_mode == SecurityMode.CRITICAL and state.session_id == session.session_id and bool(cited & set(state.evidence_ids))
        lock = _lock(service, session, rec.detected_at_ms)
        confirmed = lock["state"] == "confirmed"
        inc = rec.incident
        evidence: list[dict] = []
        if rec.person is not None:
            evidence.append(_person_item(*rec.person))
        if rec.usb is not None:
            evidence.append(_usb_item(rec.usb))
        elif rec.rehearsal:
            evidence.append({"kind": "usb", "label": "Sample USB device (rehearsal)", "detail": "FIXTURE · not real hardware", "at_ms": rec.detected_at_ms,
                             "source": "rehearsal_fixture", "event_id": rec.id.split(":", 1)[1]})
        if inc is not None:
            evidence.append({"kind": "incident", "label": "Incident opened", "at_ms": inc.created_at or inc.updated_at, "source": "spacetimedb", "event_id": f"incident:{inc.incident_id}",
                             "detail": f"{_clean(inc.rule_id, confirmed) or 'correlated incident'} · {inc.severity.value.upper()} (server) · {inc.state.value}", "severity": inc.severity.value})
        elif rec.local_incident_id is not None:
            local = service.incident if (service.incident or {}).get("incident_id") == rec.local_incident_id else {}
            evidence.append({"kind": "incident", "label": "Incident opened", "at_ms": local.get("opened_at"), "source": "local", "event_id": rec.local_incident_id,
                             "detail": f"On this Mac ({local.get('reason') or 'unknown_usb'}) · SpacetimeDB has not confirmed it", "severity": None})
        workstation = self._workstation_id(service)
        evidence.append({"kind": "scope", "label": "Workstation and session", "detail": f"{workstation or 'This Mac'} · {_short(session.session_id)}", "at_ms": None,
                         "source": "spacetimedb" if session.source == "spacetimedb" else "local"})
        evidence.append({"kind": "sensors", "label": "Sensor health", "detail": _sensors(service), "at_ms": None, "source": "backend"})

        summary = _clean(inc.summary, confirmed) if inc is not None else None
        if rec.rehearsal:
            headline, sentence, source = "Rehearsal: sample device alert", "A rehearsal sample, not a real device. Nothing was connected to this Mac.", "usb_only"
        else:
            headline = "Unauthorized device activity detected"
            if summary:
                sentence, source = summary, "spacetimedb_summary"
            elif rec.usb is not None and rec.person is not None and -2000 <= rec.usb.observed_at - rec.person[1] <= PERSON_WINDOW_MS:
                gap = (rec.usb.observed_at - rec.person[1]) / 1000
                who = _who(rec.person[0])
                sentence = (f"An unknown USB device connected {gap:.1f} s after {who} reported a person at this desk." if gap >= 0
                            else f"An unknown USB device connected as {who} reported a person at this desk.")
                source = "local_timing"
            elif rec.usb is not None:
                sentence, source = "An unknown USB device was connected to this Mac while protection was armed.", "usb_only"
            else:
                sentence, source = "SpacetimeDB linked a device event on this workstation to a person seen at the desk.", "spacetimedb_summary"

        authorized = getattr(service.transport, "authorized", None)
        if inc is not None:
            recorded = _line("recorded", f"Recorded in SpacetimeDB ({len(inc.evidence_ids)} evidence id{'s' if len(inc.evidence_ids) != 1 else ''})")
        elif rec.rehearsal:
            recorded = _line("local_only", "Rehearsal sample: kept in the demo timeline only")
        elif rec.usb is not None:
            recorded = _line("local_only", "Kept on this Mac: SpacetimeDB refused this Mac's writes" if authorized is False
                             else "Kept on this Mac and sent to SpacetimeDB; no SpacetimeDB incident yet" if service.spacetime_connected
                             else "Kept on this Mac only: SpacetimeDB is not connected")
        else:
            recorded = _line("unavailable", "No record of this event was found")

        gate, policy = service.rt.tool_gate, session.policy
        tools = (_line("frozen", f"Protected LOCKI tools are frozen ({_clean(gate.frozen, confirmed) or 'an incident is open'}); reading still works") if gate.frozen
                 else _line("not_configured", "Freezing protected tools was not turned on for this session") if not policy.freeze_protected_tools
                 else _line("not_frozen", "Freezing is on for this session, but protected tools are not frozen now"))
        ids = {i for i in (rec.usb.event_id if rec.usb else None, inc.incident_id if inc else None) if i}
        audio = _line("skipped", "Rehearsal sample: no warning is spoken") if rec.rehearsal else _audio(service, ids)
        return {
            "id": rec.id,
            "live": rec.live,
            "rehearsal": rec.rehearsal,
            "trigger": "correlated_incident" if inc is not None else "unknown_usb",
            "detected_at_ms": rec.detected_at_ms,
            "fresh": now_ms - rec.detected_at_ms <= TAKEOVER_MAX_AGE_MS,
            "local_mode": state.current_security_mode.value,
            "local_mode_verified": verified,
            "headline": headline,
            "sentence": sentence,
            "sentence_source": source,
            "evidence": evidence,
            "response": {
                "audio": audio,
                "tools": tools,
                "lock": lock,
                "monitoring": _monitoring(service),
                "nemotron": self._nemotron(service, {i for i in (rec.incident_id, inc.incident_id if inc else None) if i}),
                "evidence": recorded,
            },
            "incident_id": rec.incident_id,
            "acknowledged_at_ms": rec.acknowledged_at_ms,
            "shown_at_ms": rec.shown_at_ms,
            "shown_latency_ms": rec.shown_latency_ms,
        }
