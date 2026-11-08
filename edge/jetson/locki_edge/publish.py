"""SecurityEventV1 construction, validation, sequencing, and delivery to SpacetimeDB.

SpacetimeDB has no Python client SDK, so the Jetson calls reducers over the
documented HTTP API:  POST /v1/database/<db>/call/<reducer>
with body = JSON array of reducer args and `Authorization: Bearer <token>`.
The reducer takes ONE string arg (the event JSON). The device is identified by
the token's Identity (ctx.sender) on the server, never by a field in the body.
"""
import collections
import json
import os
import threading
import time
import uuid

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover
    raise

SCHEMA_VERSION = 1
KINDS = ("person_in_zone", "zone_clear", "camera_offline", "camera_online", "heartbeat")
SOURCES = ("jetson", "mac_fallback_camera")
REQUIRED = ("schema_version", "event_id", "source", "kind", "observed_at", "seq", "confidence", "metadata")
MAX_EVENT_BYTES = 4096  # metadata only; frames never leave the device


def now_ms():
    return int(round(time.time() * 1000))


class SequenceStore(object):
    """Per-device monotonic sequence persisted across restarts (Jetson has no RTC; don't use clock)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.value = 0
        try:
            with open(path) as f:
                self.value = int(f.read().strip() or 0)
        except (IOError, OSError, ValueError):
            self.value = 0

    def next(self):
        with self.lock:
            self.value += 1
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.write(str(self.value))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return self.value


def make_event(seq_store, source, kind, confidence=None, metadata=None, observed_at=None):
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "source": source,
        "kind": kind,
        "observed_at": int(observed_at if observed_at is not None else now_ms()),
        "seq": seq_store.next(),
        "confidence": None if confidence is None else round(float(confidence), 4),
        "metadata": metadata or {},
    }
    validate_event(event)
    return event


def validate_event(e):
    """Same rules as contracts/security_event_v1.schema.json. Raises ValueError."""
    missing = [k for k in REQUIRED if k not in e]
    if missing:
        raise ValueError("missing fields: %s" % missing)
    extra = set(e) - set(REQUIRED)
    if extra:
        raise ValueError("unknown fields: %s" % sorted(extra))
    if e["schema_version"] != SCHEMA_VERSION:
        raise ValueError("schema_version must be 1")
    if e["source"] not in SOURCES:
        raise ValueError("bad source")
    if e["kind"] not in KINDS:
        raise ValueError("bad kind")
    if not isinstance(e["seq"], int) or isinstance(e["seq"], bool) or e["seq"] < 1:
        raise ValueError("seq must be a positive int")
    if not isinstance(e["observed_at"], int) or e["observed_at"] <= 0:
        raise ValueError("observed_at must be unix ms int")
    c = e["confidence"]
    if c is not None and not (isinstance(c, float) and 0.0 <= c <= 1.0):
        raise ValueError("confidence must be null or 0..1")
    if not isinstance(e["metadata"], dict):
        raise ValueError("metadata must be an object")
    uuid.UUID(e["event_id"])
    if len(json.dumps(e)) > MAX_EVENT_BYTES:
        raise ValueError("event too large")
    return True


class JsonlSink(object):
    """Local append-only audit log of every emitted event (also used by tools/trials.py)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def send(self, event):
        with self.lock:
            with open(self.path, "a") as f:
                f.write(json.dumps(event, sort_keys=True) + "\n")
        return "ok"


class SpacetimeSink(object):
    """One HTTP reducer call per event. Returns 'ok', 'retry', or 'reject'."""

    def __init__(self, base_url, database, token, reducer="ingest_device_event", timeout=3.0):
        self.url = "%s/v1/database/%s/call/%s" % (base_url.rstrip("/"), database, reducer)
        self.token = token
        self.timeout = timeout
        self.last_error = None
        self.last_latency_ms = None

    def send(self, event):
        body = json.dumps([json.dumps(event, sort_keys=True)]).encode("utf-8")
        req = Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        t0 = time.time()
        try:
            resp = urlopen(req, timeout=self.timeout)
            resp.read()
            self.last_latency_ms = (time.time() - t0) * 1000.0
            self.last_error = None
            return "ok"
        except HTTPError as err:
            detail = err.read()[:300].decode("utf-8", "replace")
            self.last_error = "http_%d: %s" % (err.code, detail)
            # 530 = SpacetimeDB "reducer returned an error" (e.g. replayed seq). Retrying can't help.
            if err.code == 530:
                return "reject"
            # 408/429/5xx are transient. Other 4xx = rejected request; retrying won't help.
            return "retry" if err.code in (408, 429) or err.code >= 500 else "reject"
        except Exception as err:  # URLError, timeouts, http.client.HTTPException, bad URL: never kill the outbox
            self.last_error = "network: %s: %s" % (type(err).__name__, err)
            return "retry"


class Outbox(object):
    """Bounded background delivery with retry. Heartbeats are never retried (a fresh one comes in 3 s)."""

    def __init__(self, sink, maxlen=500, max_backoff=5.0, max_attempts=12):
        self.sink = sink
        self.max_attempts = max_attempts  # ~1 min of backoff, then give up on that event (logged in stats)
        self.queue = collections.deque(maxlen=maxlen)
        self.cond = threading.Condition()
        self.max_backoff = max_backoff
        self.stats = {"sent": 0, "rejected": 0, "retries": 0, "dropped_heartbeats": 0, "overflow_dropped": 0,
                      "gave_up": 0}
        self.stopping = False
        self.thread = threading.Thread(target=self._run, name="outbox")
        self.thread.daemon = True
        self.thread.start()

    def put(self, event):
        with self.cond:
            if len(self.queue) == self.queue.maxlen:
                self.stats["overflow_dropped"] += 1
            self.queue.append(event)
            self.cond.notify()

    def depth(self):
        with self.cond:
            return len(self.queue)

    def _run(self):
        backoff = 0.25
        attempts = 0
        while True:
            with self.cond:
                while not self.queue and not self.stopping:
                    self.cond.wait(0.5)
                if self.stopping and not self.queue:
                    return
                event = self.queue[0]
            result = self.sink.send(event)
            attempts += 1
            with self.cond:
                if result == "retry" and event["kind"] != "heartbeat" and attempts < self.max_attempts:
                    self.stats["retries"] += 1
                else:
                    attempts = 0
                    if self.queue and self.queue[0] is event:
                        self.queue.popleft()
                    if result == "ok":
                        self.stats["sent"] += 1
                        backoff = 0.25
                        continue
                    if result == "reject":
                        self.stats["rejected"] += 1
                        continue
                    if event["kind"] == "heartbeat":
                        self.stats["dropped_heartbeats"] += 1
                    else:
                        self.stats["gave_up"] += 1
            time.sleep(backoff)
            backoff = min(self.max_backoff, backoff * 2)

    def stop(self, flush_seconds=2.0):
        deadline = time.time() + flush_seconds
        while self.depth() and time.time() < deadline:
            time.sleep(0.05)
        with self.cond:
            self.stopping = True
            self.cond.notify()
        self.thread.join(timeout=1.0)


def load_token(path):
    if not path:
        return None
    with open(os.path.expanduser(path)) as f:
        return f.read().strip()
