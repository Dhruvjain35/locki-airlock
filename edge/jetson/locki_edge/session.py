"""Live armed-session discovery for an edge device.

The deployed module rejects camera events with "No matching live armed session" unless session_id names
the workstation's live armed session (answered live 2026-11-08 00:43:21Z). So the edge reads, as its OWN
enrolled identity: my_workstations.active_session_id, then that row in my_sessions (state + expiry).
It never arms, disarms, or writes anything. Python 3.6 compatible.
"""
import re
import threading
import time

SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")   # also keeps SQL interpolation safe
ARMED_STATES = ("armed",)
# active_session_id is only set while armed (it was "" when disarmed). A state string we don't know is
# treated as live: the reducer is the judge and its receipt says so if we're wrong.
INACTIVE_STATES = ("disarmed", "expired", "ended", "closed", "cancelled", "canceled", "inactive", "stopped")


class SessionTracker(object):
    """static_session_id=None -> auto discovery; a string -> always that id (armed iff non-empty)."""

    def __init__(self, client, workstation_id, poll_s=2.0, static_session_id=None, stale_after_s=30.0,
                 clock=time.time, on_change=None):
        self.client = client
        self.workstation_id = workstation_id
        self.poll_s = float(poll_s)
        self.static = static_session_id
        self.stale_after_s = float(stale_after_s)
        self.clock = clock
        self.on_change = on_change
        self.lock = threading.Lock()
        self.session_id = static_session_id or ""
        self.armed = bool(static_session_id)
        self.detail = "static" if static_session_id is not None else "not polled yet"
        self.last_ok_at = None
        self.checked_at_ms = None
        self.error = None
        self.polls = 0
        self.changes = 0
        self.running = False
        self.thread = None

    @property
    def mode(self):
        return "auto" if self.static is None else "static"

    def current(self):
        with self.lock:
            return self.session_id, self.armed

    def _lookup(self):
        """-> (session_id, armed, detail). Raises RuntimeError on a failed read."""
        if not SESSION_ID.match(self.workstation_id or ""):
            raise RuntimeError("workstation_id %r is not a safe id" % self.workstation_id)
        status, rows, err = self.client.sql_rows("SELECT * FROM my_workstations WHERE id = '%s'" % self.workstation_id)
        if status != 200:
            raise RuntimeError("my_workstations http %s %s" % (status, (err or "")[:120]))
        if not rows:
            return "", False, "workstation not visible to this device"
        sid = str(rows[0].get("active_session_id") or "")
        if not sid:
            return "", False, "no active session (disarmed)"
        if not SESSION_ID.match(sid):
            raise RuntimeError("active_session_id has unexpected characters")
        status, srows, err = self.client.sql_rows("SELECT * FROM my_sessions WHERE id = '%s'" % sid)
        if status != 200:
            raise RuntimeError("my_sessions http %s %s" % (status, (err or "")[:120]))
        if not srows:
            return sid, True, "active_session_id set; session row not visible to this device"
        row = srows[0]
        state = str(row.get("state") or "").lower()
        now_ms = int(self.clock() * 1000)
        expires = int(row.get("expires_at_ms") or 0)
        if state in INACTIVE_STATES:
            return sid, False, "session state %s" % state
        if row.get("disarmed_at_ms"):
            return sid, False, "session disarmed"
        if expires and expires <= now_ms:
            return sid, False, "session expired"
        known = "" if state in ARMED_STATES else " (state %r assumed live)" % state
        return sid, True, "armed until %s%s" % (expires or "no expiry", known)

    def poll_once(self):
        if self.static is not None:
            return self.current()
        self.polls += 1
        now = self.clock()
        try:
            sid, armed, detail = self._lookup()
            error = None
            self.last_ok_at = now
        except Exception as err:  # network/HTTP: keep the last answer until it goes stale
            error = "%s: %s" % (type(err).__name__, str(err)[:160])
            with self.lock:
                sid, armed, detail = self.session_id, self.armed, self.detail
            if self.last_ok_at is None or now - self.last_ok_at > self.stale_after_s:
                sid, armed, detail = "", False, "unknown (session reads failing)"
        with self.lock:
            changed = (sid, armed) != (self.session_id, self.armed)
            self.session_id, self.armed, self.detail, self.error = sid, armed, detail, error
            self.checked_at_ms = int(now * 1000)
            if changed:
                self.changes += 1
        if changed and self.on_change is not None:
            self.on_change(sid, armed, detail)
        return sid, armed

    def _loop(self):
        while self.running:
            self.poll_once()
            slept = 0.0
            while self.running and slept < self.poll_s:
                time.sleep(0.1)
                slept += 0.1

    def start(self):
        if self.static is not None or self.thread is not None:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, name="session")
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def describe(self):
        with self.lock:
            return {"mode": self.mode, "session_id": self.session_id, "armed": self.armed, "detail": self.detail,
                    "error": self.error, "checked_at_ms": self.checked_at_ms, "polls": self.polls,
                    "changes": self.changes}
