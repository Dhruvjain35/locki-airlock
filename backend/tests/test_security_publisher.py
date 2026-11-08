"""Publishing: numbering across restarts, order under failure, an honest record of what was delivered, and never holding up the local response."""

from __future__ import annotations

import json
import threading
import time

import pytest
from pydantic import ValidationError

from app.security.contracts import SecurityKind, SecuritySource
from app.security.publisher import (
    Deduper,
    Delivery,
    FixtureTransport,
    LocalOnlyTransport,
    SecurityEventPublisher,
)

HEALTH = {"monitor": "usb", "status": "started", "method": "iokit_notification", "detail": None}


def publisher(tmp_path, transport=None):
    return SecurityEventPublisher("mac-test", transport or FixtureTransport(), directory=tmp_path, is_fixture=True)


def event(p):
    return p.build(SecuritySource.MAC_AGENT, SecurityKind.MONITOR_HEALTH, HEALTH)


def test_sequence_numbers_keep_rising_across_a_restart(tmp_path):
    p = publisher(tmp_path)
    assert [event(p).seq for _ in range(3)] == [1, 2, 3]
    p2 = publisher(tmp_path)
    assert event(p2).seq == 4


def test_each_event_gets_its_own_id_and_a_real_time(tmp_path):
    p = publisher(tmp_path)
    a, b = event(p), event(p)
    assert a.event_id != b.event_id and abs(a.observed_at - time.time() * 1000) < 5000 and a.is_fixture


def test_an_invalid_event_raises_before_anything_is_sent(tmp_path):
    p = publisher(tmp_path)
    with pytest.raises(ValidationError):
        p.build(SecuritySource.MAC_USB, SecurityKind.USB_DEVICE_ADDED, {"fingerprint": "x"})
    assert p.transport.events == []


def test_events_are_delivered_in_order_and_retried_after_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("app.security.publisher.RETRY_BASE_S", 0.01)
    t = FixtureTransport()
    t.fail_next = 2
    p = publisher(tmp_path, t)
    events = [event(p) for _ in range(3)]
    for e in events:
        p.publish(e)
    assert p.flush()
    assert [e.seq for e in t.events] == [e.seq for e in events] and p.failures == 2
    p.close()


def test_an_exception_in_the_transport_is_a_failed_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr("app.security.publisher.RETRY_BASE_S", 0.01)

    class Broken:
        name = "broken"
        calls = 0

        def send(self, e):
            Broken.calls += 1
            if Broken.calls == 1:
                raise ConnectionError("database is down")
            return Delivery("delivered", self.name)

    p = publisher(tmp_path, Broken())
    p.publish(event(p))
    assert p.flush() and p.failures == 1 and "database is down" in p.last_error


def test_publish_returns_at_once_even_if_the_backend_hangs(tmp_path):
    release = threading.Event()

    class Hangs:
        name = "hangs"

        def send(self, e):
            release.wait(5)
            return Delivery("delivered", self.name)

    p = publisher(tmp_path, Hangs())
    t0 = time.monotonic()
    p.publish(event(p))
    p.publish(event(p))
    assert time.monotonic() - t0 < 0.5, "the local response must not wait for a database"
    release.set()
    assert p.flush()


def test_with_no_backend_the_record_says_nothing_left_the_mac(tmp_path):
    p = publisher(tmp_path, LocalOnlyTransport())
    p.publish(event(p))
    assert p.flush()
    assert p.snapshot()["last_delivery"]["status"] == "local_only"
    lines = [json.loads(line) for path in tmp_path.glob("events-*.jsonl") for line in path.read_text().splitlines()]
    assert not any(line.get("delivery", {}).get("status") == "delivered" for line in lines)
    p.close()


def test_a_fixture_is_never_recorded_as_delivered(tmp_path):
    p = publisher(tmp_path)
    p.publish(event(p))
    assert p.flush() and p.last_delivery.status == "fixture"
    p.close()


def test_the_local_log_says_what_it_is(tmp_path):
    p = publisher(tmp_path)
    p.publish(event(p))
    p.flush()
    head = json.loads(next(tmp_path.glob("events-*.jsonl")).read_text().splitlines()[0])
    assert head["log"] == "locki-security-events" and "Not a NeMo Relay trace" in head["note"] and "not SpacetimeDB" in head["note"]
    p.close()


def test_the_queue_is_bounded_and_counts_what_it_drops(tmp_path, monkeypatch):
    monkeypatch.setattr("app.security.publisher.MAX_PENDING", 3)
    release = threading.Event()

    class Hangs:
        name = "hangs"

        def send(self, e):
            release.wait(5)
            return Delivery("delivered", self.name)

    p = publisher(tmp_path, Hangs())
    for _ in range(6):
        p.publish(event(p))
    assert p.snapshot()["pending"] <= 3 and p.dropped >= 2
    release.set()
    p.flush()


def test_the_deduper_forgets_after_the_window():
    now = [0.0]
    d = Deduper(2.0, clock=lambda: now[0])
    assert not d.repeated("a") and d.repeated("a") and not d.repeated("b")
    now[0] = 2.5
    assert not d.repeated("a") and d.suppressed == 1
