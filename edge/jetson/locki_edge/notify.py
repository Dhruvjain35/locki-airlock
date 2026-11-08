"""Presence push (ntfy) for SERVER-ACCEPTED person_in_zone events. Python 3.6 compatible, stdlib only.

Alert policy: a person alone is an observation, not proof of a threat. So the edge sends exactly ONE kind of push,
presence, at ntfy priority 3 or lower (it never pages). It is rate limited per workstation+zone and sent at most once
per visit. Incident and alarm notifications come only from the M4 backend's security dispatch, which reads
SpacetimeDB's own incident rows and sends one ntfy message per incident per severity. The Jetson identity cannot
read incidents, so nothing here ever names a severity or claims an incident.

- Off unless AIRLOCK_PUSH_URL or AIRLOCK_PUSH_URL_FILE is set (config.py). The topic is the secret: it is never
  logged, printed or shown. It is described only as "ntfy …<last4>".
- Pushed only after SpacetimeDB answered "accepted" for that very event, so the event id in the text is a real
  accepted event. Face kinds never push.
- A visit starts with a person_in_zone and ends at the runtime's LOCAL zone-clear signal (person_cleared from the
  normalizer, whether or not the module accepts that kind). Camera loss does not end a visit.
- Sent on one background worker with bounded retries. The outbox worker only enqueues, so publishing never waits.
- "accepted" in the push audit means ntfy's own receipt (the message id it assigned) and nothing more: it does not
  prove that any phone or Mac showed the notification.
- The payload is text only: no image, attachment, confidence, face or biometric field, frame URL or camera data.
"""
import json
import os
import queue
import re
import socket
import threading
import time
from collections import OrderedDict

from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PRESENCE_KIND = "person_in_zone"
PUSH_KINDS = (PRESENCE_KIND,)                         # the ONLY kind the edge ever pushes
VISIT_END_KINDS = ("person_left_zone", "person_cleared")  # local zone-clear signals (normalizer exit kind first)
PRESENCE_TAGS = ("eyes", "locki-presence")
PAYLOAD_KEYS = ("topic", "title", "message", "priority", "tags")
MIN_PRIORITY, MAX_PRIORITY = 1, 3                     # presence never goes above ntfy "default"
RETRY_DELAYS_S = (2.0, 6.0, 15.0)                     # after the first attempt; transient failures only
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TRACKED_EVENTS = 1000
AUDIT_MAX_BYTES = 1000000


class PushConfigError(ValueError):
    """The push destination is unusable. Messages never contain the URL or the topic."""


class _Transient(Exception):
    pass


class _Permanent(Exception):
    pass


def parse_push_url(url):
    """<server>/<topic> -> (server, topic). https only; http:// only for 127.0.0.1 or localhost (tests)."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise PushConfigError("the push URL is not a valid URL")
    if not (parts.scheme == "https" or (parts.scheme == "http" and host in ("127.0.0.1", "localhost"))):
        raise PushConfigError("the push URL must be https:// (http:// only for 127.0.0.1 or localhost)")
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        raise PushConfigError("the push URL must be <server>/<topic> with no credentials, query or fragment")
    prefix, _, topic = parts.path.rstrip("/").rpartition("/")
    if not TOPIC_RE.match(topic):
        raise PushConfigError("the push URL must end in an ntfy topic (letters, digits, '-' and '_', up to 64)")
    return "%s://%s%s" % (parts.scheme, parts.netloc, prefix), topic


def describe_destination(topic):
    return "ntfy \u2026%s" % topic[-4:]


def clamp_priority(value):
    return max(MIN_PRIORITY, min(MAX_PRIORITY, int(value)))


def hhmmss_utc(epoch_ms):
    return time.strftime("%H:%M:%SZ", time.gmtime(epoch_ms / 1000.0))


def presence_text(workstation_id, zone_id, observed_at_ms, event_id, device_id, simulated=False):
    """-> (title, message). Wording is fixed by the alert policy; the Mac listener parses it."""
    title = "LOCKI presence \u00b7 %s" % workstation_id
    message = "Person observed in %s at %s. Observation only, not an incident. Event %s \u00b7 device %s." % (
        zone_id or "the desk zone", hhmmss_utc(observed_at_ms), event_id, device_id)
    if simulated:
        message = "Simulated rehearsal. " + message
    return title, message


class PresenceLimiter(object):
    """Per (workstation, zone): at most one push per visit, and at least min_interval_s between pushes."""

    def __init__(self, min_interval_s=600.0, clock=time.monotonic):
        self.min_interval_s = float(min_interval_s)
        self.clock = clock
        self.visit = {}         # key -> current visit number (bumped by every local zone clear)
        self.pushed_visit = {}  # key -> visit number that already pushed
        self.last_sent = {}     # key -> clock() of the last allowed push

    def visit_of(self, key):
        return self.visit.get(key, 0)

    def end_visit(self, key):
        self.visit[key] = self.visit.get(key, 0) + 1

    def decide(self, key, visit):
        """-> None (send) or the suppression reason. visit None = unknown (the once-per-visit check is skipped)."""
        if visit is not None and self.pushed_visit.get(key) == visit:
            return "once_per_visit"
        now = self.clock()
        last = self.last_sent.get(key)
        if last is not None and now - last < self.min_interval_s:
            return "rate_limited"
        self.last_sent[key] = now
        if visit is not None:
            self.pushed_visit[key] = visit
        return None


class PresencePusher(object):
    """The edge's only push path. Thread-safe: note_event/visit_ended run on the camera thread, on_receipt on the
    outbox worker, sending on its own worker."""

    def __init__(self, url, workstation_id, device_id, priority=3, min_interval_s=600.0, timeout_s=8.0,
                 retry_delays=RETRY_DELAYS_S, opener=None, log=None, audit_path=None, clock=time.monotonic,
                 wall=time.time, start_worker=True, max_queue=20):
        self.server, self.topic = parse_push_url(url)
        self.destination = describe_destination(self.topic)
        self.workstation_id = workstation_id or "unassigned"
        self.device_id = device_id or "unenrolled"
        self.priority = clamp_priority(priority)
        self.timeout_s = float(timeout_s)
        self.retry_delays = tuple(float(d) for d in retry_delays)
        self.opener = opener or urlopen
        self.log = log or (lambda *a, **k: None)
        self.audit_path = audit_path
        self.wall = wall
        self.limiter = PresenceLimiter(min_interval_s, clock)
        self.lock = threading.Lock()
        self.events = OrderedDict()   # event_id -> (zone_id, visit, observed_at_ms, simulated)
        self.queue = queue.Queue(maxsize=max_queue)
        self.counts = {"accepted": 0, "failed": 0, "suppressed": 0, "dropped": 0, "unknown": 0}
        self.last = None
        self._stop = threading.Event()
        self._worker = None
        if start_worker:
            self._worker = threading.Thread(target=self._run, name="presence-push")
            self._worker.daemon = True
            self._worker.start()

    # -- camera thread ---------------------------------------------------------------------------
    def note_event(self, event_id, zone_id, observed_at_ms, simulated=False, quiet=False):
        """A person_in_zone was enqueued for SpacetimeDB: remember where/when and which visit it belongs to.
        `quiet` marks a re-assert of a visit already under way: sent to SpacetimeDB, never pushed."""
        with self.lock:
            visit = self.limiter.visit_of((self.workstation_id, zone_id or ""))
            self.events[event_id] = (zone_id, visit, int(observed_at_ms), bool(simulated), bool(quiet))
            while len(self.events) > MAX_TRACKED_EVENTS:
                self.events.popitem(last=False)

    def visit_ended(self, zone_id):
        """The runtime's local zone-clear signal: the next person_in_zone in this zone is a new visit."""
        with self.lock:
            self.limiter.end_visit((self.workstation_id, zone_id or ""))

    # -- outbox thread (must return at once) ------------------------------------------------------
    def on_receipt(self, receipt):
        """-> "queued", "suppressed", "dropped" or None (not a push). Only an accepted person_in_zone pushes."""
        if receipt.kind not in PUSH_KINDS:
            return None
        with self.lock:
            if receipt.status in ("retryable", "ambiguous"):
                return None   # still pending: keep where/when for the final answer
            info = self.events.pop(receipt.event_id, None)
            if receipt.status != "accepted":
                return None
            if info is None:
                # Not an event this process enqueued (an outbox replay after a restart, or one evicted from the
                # bounded map): its zone, visit, time and re-assert flag are unknown, so it is never pushed.
                self.counts["unknown"] += 1
                return None
            zone_id, visit, observed_ms, simulated, quiet = info
            if quiet:
                return None   # a re-assert of a visit: never a push of its own
            reason = self.limiter.decide((self.workstation_id, zone_id or ""), visit)
            if reason:
                self.counts["suppressed"] += 1
        if reason:
            self._record("suppressed", receipt.event_id, zone_id, reason=reason)
            return "suppressed"
        title, message = presence_text(self.workstation_id, zone_id, observed_ms, receipt.event_id, self.device_id,
                                       simulated)
        payload = {"topic": self.topic, "title": title, "message": message, "priority": self.priority,
                   "tags": list(PRESENCE_TAGS)}
        try:
            self.queue.put_nowait((receipt.event_id, zone_id, payload))
        except queue.Full:
            with self.lock:
                self.counts["dropped"] += 1
            self._record("dropped", receipt.event_id, zone_id, reason="push queue full")
            return "dropped"
        return "queued"

    # -- push worker ------------------------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.deliver(*item)

    def send_once(self, payload):
        """ntfy JSON publish to the server root. -> ntfy's message id (its receipt). Errors never carry the URL."""
        req = Request(self.server + "/", data=json.dumps(payload).encode("utf-8"), method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            resp = self.opener(req, timeout=self.timeout_s)
        except HTTPError as e:
            code = e.code
            try:
                e.close()
            except Exception:
                pass
            raise (_Transient if code == 429 or code >= 500 else _Permanent)("HTTP %d" % code)
        except (URLError, socket.timeout, OSError, ValueError) as e:
            raise _Transient(type(e).__name__)
        try:
            code = int(getattr(resp, "status", None) or resp.getcode())
            body = resp.read() if hasattr(resp, "read") else b""
        finally:
            if hasattr(resp, "close"):
                resp.close()
        if code == 429 or code >= 500:
            raise _Transient("HTTP %d" % code)
        if not 200 <= code < 300:
            raise _Permanent("HTTP %d" % code)
        try:
            message_id = str(json.loads(body.decode("utf-8")).get("id") or "")
        except (ValueError, AttributeError, UnicodeDecodeError):
            message_id = ""
        if not message_id:
            raise _Transient("no receipt id in the reply")
        return message_id

    def deliver(self, event_id, zone_id, payload):
        """Blocking send with bounded retries. Never raises."""
        error, attempt = "", 0
        for attempt, delay in enumerate((0.0,) + self.retry_delays, start=1):
            if delay and self._stop.wait(delay):
                error = error or "stopped"
                break
            try:
                receipt = self.send_once(payload)
            except _Transient as e:
                error = str(e)
                continue
            except _Permanent as e:
                error = str(e)
                break
            except Exception as e:  # noqa: BLE001 - a push must never take the runtime down
                error = type(e).__name__
                break
            with self.lock:
                self.counts["accepted"] += 1
                self.last = {"status": "accepted", "event_id": event_id, "provider_receipt": receipt,
                             "attempt": attempt, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.wall()))}
            self._record("accepted", event_id, zone_id, provider_receipt=receipt, attempts=attempt)
            return receipt
        with self.lock:
            self.counts["failed"] += 1
            self.last = {"status": "failed", "event_id": event_id, "error": error, "attempt": attempt,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.wall()))}
        self._record("failed", event_id, zone_id, reason=error, attempts=attempt)
        return None

    # -- records ------------------------------------------------------------------------------------
    def _record(self, status, event_id, zone_id, **fields):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.wall())), "kind": PRESENCE_KIND,
               "status": status, "event_id": event_id, "zone_id": zone_id, "destination": self.destination}
        rec.update(fields)
        level = "warn" if status in ("failed", "dropped") else "info"
        self.log(level, "presence_push", **rec)
        if not self.audit_path:
            return
        try:
            if os.path.exists(self.audit_path) and os.path.getsize(self.audit_path) > AUDIT_MAX_BYTES:
                os.replace(self.audit_path, self.audit_path + ".1")
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError:
            pass

    def describe(self):
        with self.lock:
            return {"enabled": True, "kinds": list(PUSH_KINDS), "destination": self.destination,
                    "priority": self.priority, "min_interval_s": self.limiter.min_interval_s,
                    "accepted": self.counts["accepted"], "failed": self.counts["failed"],
                    "suppressed": self.counts["suppressed"], "dropped": self.counts["dropped"],
                    "queued": self.queue.qsize(), "last": self.last}

    def close(self, timeout_s=1.0):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout_s)
