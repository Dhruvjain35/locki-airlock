"""Sends security events on, in order, without ever holding up the local response. Validation, numbering and the local log happen
at once; delivery runs on its own thread, so a slow or missing database can never delay a warning.

The transport is injected. "delivered" means a real backend accepted the event. A transport with no backend reports "local_only" and
a test fixture reports "fixture", so nothing here can look like a database acknowledgement that did not happen.

The file written here is LOCKI's own application log. It is not a NeMo Relay trace and not SpacetimeDB.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .contracts import AgentStepV1, SecurityEventV1, SecurityKind, SecuritySource

log = logging.getLogger(__name__)

LOG_NOTE = "LOCKI application log. Not a NeMo Relay trace and not SpacetimeDB."
MAX_PENDING = 1000
RETRY_BASE_S = 0.5
RETRY_MAX_S = 15.0


@dataclass(frozen=True)
class Delivery:
    status: Literal["delivered", "refused", "local_only", "fixture", "failed"]  # refused: the backend answered no, so trying again cannot help
    transport: str
    detail: str | None = None


Message = Any  # SecurityEventV1, AgentStepV1, or the SpacetimeDB adapter's BaselineReport and LockResult


class Transport(Protocol):
    name: str

    def send(self, message: Message) -> Delivery: ...


class LocalOnlyTransport:
    """No backend connected: the event stays in the local log."""

    name = "local_only"

    def send(self, message: Message) -> Delivery:
        return Delivery("local_only", self.name, "no backend connected")


class FixtureTransport:
    """For tests and development against sample data. Keeps what it was given and never claims more than that."""

    name = "fixture"

    def __init__(self) -> None:
        self.events: list[SecurityEventV1] = []
        self.steps: list[AgentStepV1] = []
        self.fail_next = 0

    def send(self, message: Message) -> Delivery:
        if self.fail_next > 0:
            self.fail_next -= 1
            return Delivery("failed", self.name, "fixture told to fail")
        (self.events if isinstance(message, SecurityEventV1) else self.steps).append(message)
        return Delivery("fixture", self.name)


class Deduper:
    """The same observation repeated within a short window counts once. The OS can report a device twice as it settles."""

    def __init__(self, window_s: float = 2.0, clock=time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._seen: dict[str, float] = {}
        self.suppressed = 0

    def repeated(self, key: str) -> bool:
        now = self._clock()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self.window_s}
        if key in self._seen:
            self.suppressed += 1
            return True
        self._seen[key] = now
        return False


class SecurityEventPublisher:
    def __init__(self, device_id: str, transport: Transport, *, directory: Path, is_fixture: bool = False, clock=time.time):
        self.device_id = device_id
        self.transport = transport
        self.directory = directory
        self.is_fixture = is_fixture
        self._clock = clock
        self.directory.mkdir(parents=True, exist_ok=True)
        self._seq_path = self.directory / f"seq-{device_id}"
        self._seq = self._load_seq()
        self._lock = threading.Lock()
        self._pending: deque[Message] = deque()
        self._wake = threading.Event()
        self._closing = False
        self._thread: threading.Thread | None = None
        self.published = 0
        self.dropped = 0
        self.failures = 0
        self.refused = 0
        self.last_delivery: Delivery | None = None
        self.last_error: str | None = None
        self._log_path: Path | None = None

    def _load_seq(self) -> int:
        try:
            return int(self._seq_path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _save_seq(self) -> None:
        tmp = self._seq_path.with_suffix(".tmp")
        tmp.write_text(str(self._seq))
        os.replace(tmp, self._seq_path)

    def _write_log(self, record: dict) -> None:
        path = self.directory / f"events-{time.strftime('%Y%m%d')}.jsonl"
        try:
            is_new = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                if is_new:
                    f.write(json.dumps({"log": "locki-security-events", "note": LOG_NOTE}) + "\n")
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._log_path = path
        except OSError as exc:
            self.last_error = f"could not write the local log: {exc}"[:200]
            log.warning(self.last_error)

    def build(self, source: SecuritySource, kind: SecurityKind, metadata: dict[str, Any], *, observed_at_ms: int | None = None, confidence: float | None = None) -> SecurityEventV1:
        """Validates against the contract. A malformed event is a bug and raises here, before anything is sent."""
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._save_seq()
        return SecurityEventV1(
            event_id=uuid.uuid4().hex,
            device_id=self.device_id,
            source=source,
            kind=kind,
            observed_at=observed_at_ms if observed_at_ms is not None else int(self._clock() * 1000),
            seq=seq,
            confidence=confidence,
            metadata=metadata,
            is_fixture=self.is_fixture,
        )

    def publish(self, event: Message, *, record: bool = True) -> None:
        """``record=False`` for frequent heartbeats: sent like anything else, kept out of the local log so it stays readable."""
        kind = "event" if isinstance(event, SecurityEventV1) else "step" if isinstance(event, AgentStepV1) else type(event).__name__.lower()
        if record:
            self._write_log({kind: event.model_dump(mode="json")})
        self.published += 1
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                self._pending.popleft()
                self.dropped += 1
            self._pending.append(event)
        self._ensure_sender()
        self._wake.set()

    def _ensure_sender(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._send_loop, daemon=True, name="security-publisher")
            self._thread.start()

    def _send_loop(self) -> None:
        delay = RETRY_BASE_S
        while True:
            with self._lock:
                event = self._pending[0] if self._pending else None
            if event is None:
                if self._closing:
                    return
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                delivery = self.transport.send(event)
            except Exception as exc:
                delivery = Delivery("failed", getattr(self.transport, "name", "?"), f"{type(exc).__name__}: {exc}"[:200])
            self.last_delivery = delivery
            sent_id = {"event_id": event.event_id, "seq": event.seq} if isinstance(event, SecurityEventV1) else {"run_id": event.run_id, "step": event.step} if isinstance(event, AgentStepV1) else {"message": type(event).__name__}
            if type(event).__name__ != "HealthBeat" or delivery.status != "delivered":
                self._write_log({"delivery": {**sent_id, "status": delivery.status, "transport": delivery.transport, "detail": delivery.detail}})
            if delivery.status == "failed":
                self.failures += 1
                self.last_error = delivery.detail
                if self._closing:
                    return
                time.sleep(delay)  # in order: nothing later overtakes an undelivered event
                delay = min(delay * 2, RETRY_MAX_S)
                continue
            if delivery.status == "refused":
                self.refused += 1
                self.last_error = delivery.detail
            delay = RETRY_BASE_S
            with self._lock:
                if self._pending and self._pending[0] is event:
                    self._pending.popleft()

    def flush(self, timeout_s: float = 5.0) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            with self._lock:
                if not self._pending:
                    return True
            self._wake.set()
            time.sleep(0.01)
        return False

    def close(self, timeout_s: float = 2.0) -> None:
        self.flush(timeout_s)
        self._closing = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def snapshot(self) -> dict:
        with self._lock:
            pending = len(self._pending)
        d = self.last_delivery
        return {
            "device_id": self.device_id, "transport": getattr(self.transport, "name", "?"), "published": self.published, "pending": pending, "dropped": self.dropped, "failures": self.failures, "refused": self.refused,
            "last_delivery": None if d is None else {"status": d.status, "transport": d.transport, "detail": d.detail}, "last_error": self.last_error,
            "log_path": None if self._log_path is None else self._log_path.name, "log_note": LOG_NOTE, "is_fixture": self.is_fixture,
        }
