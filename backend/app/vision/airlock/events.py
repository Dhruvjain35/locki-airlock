"""VisionEvent -> SecurityEventV1, through the same security publisher the Mac's USB monitor uses.

The camera has its own device id (``cam-...``, its own sequence and log) because Sid's module ties what
an event may say to the device that sends it. ``publisher.build`` validates every event against the
contract, so a camera event that names a person, carries a picture or a face template is a bug that
raises here, before anything is sent.
"""

from __future__ import annotations

from collections.abc import Callable

from ...security.contracts import SecurityEventV1, SecurityKind, SecuritySource
from .pipeline import VisionEvent


class SecurityEventSink:
    def __init__(
        self,
        publisher,
        *,
        source: SecuritySource = SecuritySource.MAC_VISION,
        armed: Callable[[], tuple[bool, str | None]] = lambda: (False, None),
        on_event: Callable[[SecurityEventV1, VisionEvent], None] | None = None,
    ):
        self.publisher = publisher
        self.source = source
        self.armed = armed
        self.on_event = on_event

    def __call__(self, ev: VisionEvent) -> SecurityEventV1:
        armed, session_id = self.armed()
        meta = {**ev.metadata, "armed": bool(armed)}
        if session_id:
            meta["session_id"] = session_id
        event = self.publisher.build(self.source, SecurityKind(ev.kind), meta, observed_at_ms=ev.observed_at_ms, confidence=ev.confidence)
        self.publisher.publish(event)
        if self.on_event is not None:
            self.on_event(event, ev)
        return event
