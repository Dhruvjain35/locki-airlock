"""Durable, bounded, ordered outbox for SecurityEventV1 delivery (sqlite3, stdlib).

Guarantees:
  * Event IDs are unique and STABLE through retries and restarts: the exact positional args are
    frozen at enqueue time, so every retry is byte-identical (server dedupes on event_id / seq).
  * Strict seq order (head-of-line): a newer event never overtakes a pending older one, because
    the reducer tracks last_seq per device and would reject the older one as a replay.
  * Bounded: at most `max_pending` undelivered events; overflow drops the OLDEST and counts it.
  * Honest: only `accepted` / `duplicate` receipts mark an event delivered. Timeouts stay pending
    and are flagged ambiguous; a crash mid-send is treated as ambiguous on the next start.
  * Stale events expire after `max_age_s` (recorded as expired, never as delivered).

Python 3.6 compatible on purpose.
"""
import json
import os
import random
import sqlite3
import threading
import time

from .contract import ingest_args
from .stdb import DELIVERED

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  event_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  args_json TEXT NOT NULL,
  observed_at_ms INTEGER NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  ambiguous INTEGER NOT NULL DEFAULT 0,
  enqueued_at REAL NOT NULL,
  next_attempt_at REAL NOT NULL,
  last_status TEXT, last_reason TEXT, last_http INTEGER, last_error TEXT, last_attempt_utc TEXT,
  ack_latency_ms REAL, observed_to_ack_ms REAL
);
CREATE INDEX IF NOT EXISTS outbox_state_seq ON outbox(state, seq);
CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT, http_status INTEGER, server_result TEXT, error TEXT, attempt INTEGER,
  attempted_at_utc TEXT, latency_ms REAL
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""
TERMINAL = ("delivered", "rejected", "expired", "dropped")


class DurableOutbox(object):
    def __init__(self, path, publisher, contract, max_pending=1000, max_age_s=900.0,
                 backoff_s=(0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0), keep_terminal=5000, keep_receipts=10000,
                 wall=time.time, clock=time.monotonic, jitter=0.1, on_receipt=None):
        self.path = path
        self.publisher = publisher
        self.contract = contract
        self.max_pending = int(max_pending)
        self.max_age_s = float(max_age_s)
        self.backoff_s = tuple(backoff_s)
        self.keep_terminal, self.keep_receipts = keep_terminal, keep_receipts
        self.wall, self.clock = wall, clock
        self.jitter = jitter
        self.on_receipt = on_receipt
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        d = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(d):
            os.makedirs(d)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.captured_mono = {}           # event_id -> monotonic capture time (this process only)
        self.consecutive_failures = 0
        self.last_receipt = None
        self.running = False
        self.thread = None
        with self.lock:
            # A send that was in flight when the process died may have committed: retry as ambiguous.
            n = self.db.execute("UPDATE outbox SET state='pending', ambiguous=1 WHERE state='in_flight'").rowcount
            if n:
                self._bump("recovered_in_flight", n)

    # -- counters ----------------------------------------------------------------------
    def _bump(self, name, n=1):
        # No UPSERT: Ubuntu 18.04 (JetPack 4.x) ships SQLite 3.22, and ON CONFLICT DO UPDATE needs 3.24.
        self.db.execute("INSERT OR IGNORE INTO counters(name, value) VALUES(?, 0)", (name,))
        self.db.execute("UPDATE counters SET value = value + ? WHERE name = ?", (n, name))

    def counter(self, name):
        with self.lock:
            row = self.db.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        return row[0] if row else 0

    # -- producer side ---------------------------------------------------------------------
    def enqueue(self, event, captured_mono=None):
        """Validate + persist. Returns False if this event_id was already enqueued (local dedupe)."""
        args = ingest_args(event, self.contract)  # raises ContractError: never stored, never sent
        now = self.wall()
        with self.cond:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO outbox(event_id, seq, kind, args_json, observed_at_ms, state, enqueued_at, "
                "next_attempt_at) VALUES(?, ?, ?, ?, ?, 'pending', ?, ?)",
                (event.event_id, event.seq, event.kind, json.dumps(args, separators=(",", ":")),
                 event.observed_at_ms, now, now))
            if cur.rowcount == 0:
                self._bump("dedupe_ignored")
                return False
            if captured_mono is not None:
                self.captured_mono[event.event_id] = captured_mono
            overflow = self.pending_count() - self.max_pending
            if overflow > 0:
                rows = self.db.execute("SELECT event_id FROM outbox WHERE state='pending' ORDER BY seq LIMIT ?",
                                       (overflow,)).fetchall()
                for (eid,) in rows:
                    self.db.execute("UPDATE outbox SET state='dropped', last_reason='overflow' WHERE event_id=?",
                                    (eid,))
                    self.captured_mono.pop(eid, None)
                self._bump("overflow_dropped", len(rows))
            self.cond.notify_all()
            return True

    def pending_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM outbox WHERE state IN ('pending', 'in_flight')"
                                   ).fetchone()[0]

    # -- delivery ------------------------------------------------------------------------------
    def _backoff(self, attempts):
        base = self.backoff_s[min(max(attempts - 1, 0), len(self.backoff_s) - 1)]
        return base * (1.0 + random.uniform(-self.jitter, self.jitter))

    def next_due_in(self):
        with self.lock:
            row = self.db.execute("SELECT next_attempt_at FROM outbox WHERE state='pending' ORDER BY seq LIMIT 1"
                                  ).fetchone()
        return None if row is None else max(0.0, row[0] - self.wall())

    def pump_once(self):
        """Attempt the head-of-line event if it is due. Returns its PublishReceipt, or None."""
        with self.lock:
            row = self.db.execute(
                "SELECT event_id, seq, kind, args_json, attempts, ambiguous, enqueued_at, next_attempt_at, "
                "observed_at_ms FROM outbox WHERE state='pending' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                return None
            event_id, seq, kind, args_json, attempts, ambiguous, enqueued_at, next_at, observed_ms = row
            now = self.wall()
            if now - enqueued_at > self.max_age_s:
                self.db.execute("UPDATE outbox SET state='expired', last_reason='max_age' WHERE event_id=?",
                                (event_id,))
                self._bump("expired")
                self.captured_mono.pop(event_id, None)
                return None
            if next_at > now:
                return None
            self.db.execute("UPDATE outbox SET state='in_flight' WHERE event_id=?", (event_id,))
        receipt = self.publisher.publish_args(event_id, seq, kind, json.loads(args_json), attempt=attempts + 1,
                                              prior_ambiguous=bool(ambiguous))
        ack_wall_ms = self.wall() * 1000.0
        with self.cond:
            attempts += 1
            if receipt.delivered:
                cap = self.captured_mono.pop(event_id, None)
                ack_ms = None if cap is None else round((self.clock() - cap) * 1000.0, 1)
                self.db.execute(
                    "UPDATE outbox SET state='delivered', attempts=?, last_status=?, last_reason=?, last_http=?, "
                    "last_error=?, last_attempt_utc=?, ack_latency_ms=?, observed_to_ack_ms=? WHERE event_id=?",
                    (attempts, receipt.status, receipt.reason, receipt.http_status, receipt.error,
                     receipt.attempted_at_utc, ack_ms, round(ack_wall_ms - observed_ms, 1), event_id))
                self.consecutive_failures = 0
            elif receipt.retry_eligible:
                self.db.execute(
                    "UPDATE outbox SET state='pending', attempts=?, ambiguous=?, next_attempt_at=?, last_status=?, "
                    "last_reason=?, last_http=?, last_error=?, last_attempt_utc=? WHERE event_id=?",
                    (attempts, 1 if (ambiguous or receipt.status == "ambiguous") else 0,
                     self.wall() + self._backoff(attempts), receipt.status, receipt.reason, receipt.http_status,
                     receipt.error, receipt.attempted_at_utc, event_id))
                self.consecutive_failures += 1
                self._bump("retries")
            else:
                self.captured_mono.pop(event_id, None)
                self.db.execute(
                    "UPDATE outbox SET state='rejected', attempts=?, last_status=?, last_reason=?, last_http=?, "
                    "last_error=?, last_attempt_utc=? WHERE event_id=?",
                    (attempts, receipt.status, receipt.reason, receipt.http_status,
                     (receipt.server_result or receipt.error)[:300], receipt.attempted_at_utc, event_id))
                self.consecutive_failures = 0 if receipt.http_status else self.consecutive_failures + 1
                self._bump("rejected")
            self.db.execute(
                "INSERT INTO receipts(event_id, seq, kind, status, reason, http_status, server_result, error, attempt, "
                "attempted_at_utc, latency_ms) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, seq, kind, receipt.status, receipt.reason, receipt.http_status, receipt.server_result,
                 receipt.error, receipt.attempt, receipt.attempted_at_utc, receipt.latency_ms))
            self.last_receipt = receipt
            self._prune()
            self.cond.notify_all()
        if self.on_receipt is not None:
            try:
                self.on_receipt(receipt)
            except Exception:
                pass
        return receipt

    def _prune(self):
        self.db.execute("DELETE FROM receipts WHERE id <= (SELECT MAX(id) FROM receipts) - ?", (self.keep_receipts,))
        self.db.execute(
            "DELETE FROM outbox WHERE state IN ('delivered','rejected','expired','dropped') AND event_id NOT IN "
            "(SELECT event_id FROM outbox WHERE state IN ('delivered','rejected','expired','dropped') "
            "ORDER BY seq DESC LIMIT ?)", (self.keep_terminal,))

    # -- worker thread ---------------------------------------------------------------------------
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, name="outbox")
        self.thread.daemon = True
        self.thread.start()
        return self

    def _run(self):
        while self.running:
            try:
                receipt = self.pump_once()
            except Exception as err:  # sqlite/IO trouble must not kill delivery forever
                self._last_worker_error = "%s: %s" % (type(err).__name__, err)
                receipt = None
                time.sleep(0.5)
            if receipt is not None and receipt.delivered:
                continue
            due = self.next_due_in()
            with self.cond:
                if self.running:
                    self.cond.wait(0.5 if due is None else min(0.5, max(0.01, due)))

    def stop(self, flush_s=3.0):
        """Try to deliver what's pending for up to flush_s, then stop. Undelivered rows stay on disk."""
        deadline = time.monotonic() + flush_s
        while self.running and self.pending_count() and time.monotonic() < deadline:
            due = self.next_due_in()
            if due is not None and due > deadline - time.monotonic():
                break  # head is in backoff past the deadline: don't pretend we'll make it
            time.sleep(0.05)
        self.running = False
        with self.cond:
            self.cond.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=5.0)
        left = self.pending_count()
        return left

    def close(self):
        with self.lock:
            self.db.close()

    # -- observability ----------------------------------------------------------------------------
    def link_state(self):
        r = self.last_receipt
        if r is None:
            return "unknown"
        if r.reason == "auth":
            return "auth_rejected"
        if r.delivered or r.http_status:
            return "online" if self.consecutive_failures == 0 else "degraded"
        return "offline" if self.consecutive_failures >= 3 else "degraded"

    def stats(self):
        with self.lock:
            by_state = dict(self.db.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state").fetchall())
            lat = [r[0] for r in self.db.execute(
                "SELECT ack_latency_ms FROM outbox WHERE state='delivered' AND ack_latency_ms IS NOT NULL "
                "ORDER BY seq DESC LIMIT 200").fetchall()]
            counters = dict(self.db.execute("SELECT name, value FROM counters").fetchall())
        lat.sort()
        r = self.last_receipt
        return {
            "link": self.link_state(),
            "pending": by_state.get("pending", 0) + by_state.get("in_flight", 0),
            "delivered": by_state.get("delivered", 0),
            "rejected": by_state.get("rejected", 0),
            "expired": by_state.get("expired", 0),
            "dropped_overflow": by_state.get("dropped", 0),
            "consecutive_failures": self.consecutive_failures,
            "capture_to_ack_ms_p50": lat[len(lat) // 2] if lat else None,
            "capture_to_ack_ms_p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
            "counters": counters,
            "last_receipt": None if r is None else {k: v for k, v in r._asdict().items()},
        }

    def receipts(self, limit=20):
        with self.lock:
            rows = self.db.execute(
                "SELECT event_id, seq, kind, status, reason, http_status, server_result, error, attempt, "
                "attempted_at_utc, latency_ms FROM receipts ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        keys = ("event_id", "seq", "kind", "status", "reason", "http_status", "server_result", "error", "attempt",
                "attempted_at_utc", "latency_ms")
        return [dict(zip(keys, row)) for row in rows]

    def event_state(self, event_id):
        with self.lock:
            row = self.db.execute("SELECT state, attempts, ambiguous, last_status, last_reason FROM outbox "
                                  "WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        return dict(zip(("state", "attempts", "ambiguous", "last_status", "last_reason"), row))


__all__ = ["DurableOutbox", "TERMINAL", "DELIVERED"]
