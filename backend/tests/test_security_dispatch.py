"""Security dispatch: who hears about an incident, and what each receipt really says. No test reaches a real provider: the phone channel
talks to a stand-in that speaks ntfy's HTTP protocol on 127.0.0.1."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from app.security.contracts import IncidentUpdateV1
from app.security.dispatch import (
    DISPATCH_MAX_AGE_MS,
    MAX_INCIDENTS,
    MAX_PENDING_NOTIFICATIONS,
    Dispatcher,
    NtfyChannel,
    TransientError,
    load_phone_channel,
    recipients_for,
)
from app.security.spacetime_adapter import incident_from_row

from .test_security_spacetime import INCIDENT_COLUMNS, following, remote


class FakeNtfy:
    """Answers each POST with the next (status, body) from `replies` (the last one repeats), and records what was sent."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                sent = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                fake.requests.append({"path": self.path, **sent})
                status, data = fake.replies.pop(0) if len(fake.replies) > 1 else fake.replies[0]
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


TOPIC = "locki-test-topic-0123456789"


def incident(iid="incident:e1", severity="medium", rule="unknown_usb", session="s1", state="SUSPECTED_INTRUSION", **kw):
    now = int(time.time() * 1000)
    return IncidentUpdateV1(incident_id=iid, session_id=session, severity=severity, state=state, rule_id=rule, updated_at=kw.pop("updated_at", now),
                            created_at=kw.pop("created_at", now), summary=kw.pop("summary", "New unapproved USB device detected while armed."), **kw)


def dispatcher(tmp_path, phone=None, **kw) -> Dispatcher:
    d = Dispatcher("dhruv-m4-air", tmp_path / "phone.json", start_worker=False, **kw)
    d.phone = phone
    return d


def test_recipients_follow_the_modules_rules_and_otherwise_its_severity():
    assert recipients_for("soft_observation", "info") == ()  # a person alone: LOCKI warns locally, nobody outside is told
    assert recipients_for("unknown_usb", "medium") == ("owner",) and recipients_for("unapproved_app", "medium") == ("owner",)
    assert recipients_for("correlated_usb_physical", "high") == ("owner", "desk") and recipients_for("owner_panic", "high") == ("owner", "desk")
    assert recipients_for("some_new_rule", "critical") == ("owner", "desk") and recipients_for(None, "medium") == ("owner",) and recipients_for(None, "low") == ()


def test_an_incident_is_dispatched_once_and_again_only_when_its_severity_rises(tmp_path):
    d = dispatcher(tmp_path)
    rec = d.observe(incident(), active_session_id="s1")
    assert [(x.recipient, x.channel, x.status) for x in rec.deliveries] == [("owner", "console", "published")]
    d.observe(incident(), active_session_id="s1")  # the same row again (a reconnect, an unrelated column)
    assert len(rec.deliveries) == 1
    rec = d.observe(incident(severity="high", rule="correlated_usb_physical", summary="Unknown USB device detected within 10 seconds of a person in the desk zone."), active_session_id="s1")
    assert [(x.recipient, x.severity) for x in rec.deliveries] == [("owner", "medium"), ("owner", "high"), ("desk", "high")] and rec.notified_for_severity == "high"
    snap = d.snapshot()
    assert snap["incidents"][0]["incident_id"] == "incident:e1" and snap["phone"]["configured"] is False


def test_fixtures_replays_resolved_and_other_sessions_are_never_sent(tmp_path):
    d = dispatcher(tmp_path)
    assert d.observe(incident(is_fixture=True), active_session_id="s1") is None
    old = int(time.time() * 1000) - DISPATCH_MAX_AGE_MS - 5_000
    assert d.observe(incident("incident:old", updated_at=old), active_session_id="s1").skipped.startswith("older than")
    assert d.observe(incident("incident:other", session="s0"), active_session_id="s1").skipped == "not the armed session"
    assert d.observe(incident("incident:done", state="RESOLVED"), active_session_id="s1").deliveries == []
    assert all(not r["deliveries"] for r in d.snapshot()["incidents"])


def test_an_acknowledgement_in_spacetimedb_marks_what_was_sent(tmp_path):
    d = dispatcher(tmp_path)
    d.observe(incident(severity="high", rule="correlated_usb_physical"), active_session_id="s1")
    rec = d.observe(incident(severity="high", rule="correlated_usb_physical", acknowledged_by="0xdesk"), active_session_id="s1")
    assert rec.acknowledged_by == "0xdesk" and {x.status for x in rec.deliveries} == {"acknowledged"}


def test_the_phone_receipt_is_the_providers_and_a_transient_failure_is_retried(tmp_path):
    fake = FakeNtfy([(500, {"error": "busy"}), (200, {"id": "msg-42", "event": "message"})])
    try:
        d = dispatcher(tmp_path, phone=NtfyChannel(TOPIC, fake.base), retry_delays=(0.01, 0.01))
        rec = d.observe(incident(severity="high", rule="correlated_usb_physical", summary="Unknown USB device detected within 10 seconds of a person in the desk zone."), active_session_id="s1")
        phone = next(x for x in rec.deliveries if x.channel == "phone")
        assert phone.status == "queued"
        d.deliver(rec.incident_id, phone)
        assert phone.status == "accepted" and phone.attempts == 2 and phone.provider_message_id == "msg-42" and phone.accepted_at_ms
        sent = fake.requests[-1]
        assert sent["path"] == "/" and sent["topic"] == TOPIC and sent["priority"] == 4 and sent["title"] == "LOCKI High incident · dhruv-m4-air"
        assert "within 10 seconds of a person" in sent["message"] and "incident:e1" in sent["message"]
        snap = d.snapshot()
        assert TOPIC not in json.dumps(snap, ensure_ascii=False) and snap["phone"]["destination"] == "ntfy …" + TOPIC[-4:]  # the topic is the secret: never shown
    finally:
        fake.stop()


def test_a_refused_phone_notification_fails_at_once_and_an_unreachable_one_after_bounded_retries(tmp_path):
    fake = FakeNtfy([(400, {"error": "bad topic"})])
    try:
        d = dispatcher(tmp_path, phone=NtfyChannel(TOPIC, fake.base), retry_delays=(0.01, 0.01))
        rec = d.observe(incident(), active_session_id="s1")
        phone = rec.deliveries[-1]
        d.deliver(rec.incident_id, phone)
        assert phone.status == "failed" and phone.attempts == 1 and phone.error == "HTTP 400" and len(fake.requests) == 1
    finally:
        fake.stop()
    d = dispatcher(tmp_path, phone=NtfyChannel(TOPIC, "http://127.0.0.1:9"), retry_delays=(0.01, 0.01))
    rec = d.observe(incident("incident:e2"), active_session_id="s1")
    d.deliver(rec.incident_id, rec.deliveries[-1])
    assert rec.deliveries[-1].status == "failed" and rec.deliveries[-1].attempts == 3


def test_the_phone_channel_must_be_readable_by_the_owner_only(tmp_path):
    path = tmp_path / "phone.json"
    path.write_text(json.dumps({"provider": "ntfy", "topic": TOPIC}))
    os.chmod(path, 0o644)
    channel, error = load_phone_channel(path)
    assert channel is None and "readable by other users" in error
    os.chmod(path, 0o600)
    channel, error = load_phone_channel(path)
    assert channel is not None and error is None and channel.describe()["destination"].endswith(TOPIC[-4:])
    with pytest.raises(ValueError):
        NtfyChannel(TOPIC, "http://ntfy.example.com")  # a remote server must use https


def test_the_incident_row_carries_when_it_opened_and_who_acknowledged_it():
    row = dict(zip(INCIDENT_COLUMNS, ["incident:e9", "dhruv-m4-air", "s1", "high", "acknowledged", "correlated_usb_physical", "Unknown USB device detected within 10 seconds of a person in the desk zone.",
                                       '["e9","j9"]', "e9", "j9", 1794122002000, 1794122010000, "0xc200desk", "", "", True, False], strict=True))
    update = incident_from_row(row)
    assert update.created_at == 1794122002000 and update.acknowledged_by == "0xc200desk" and update.incident_id == "incident:e9"


async def test_a_provider_that_is_down_never_delays_the_spoken_warning(rt, tmp_path):
    h = await following(rt)
    rt.security.apply_remote_session(remote())
    rt.security.dispatch = Dispatcher("dhruv-m4-air", tmp_path / "phone.json", retry_delays=(5.0,))
    rt.security.dispatch.phone = NtfyChannel(TOPIC, "http://127.0.0.1:9")
    t0 = time.monotonic()
    rt.security.apply_incident_update(IncidentUpdateV1(incident_id="incident:e5", session_id="s1", severity="high", state="SUSPECTED_INTRUSION", evidence_ids=["e5", "j5"],
                                                       digital_event_id="e5", physical_event_id="j5", rule_id="correlated_usb_physical", updated_at=int(time.time() * 1000)))
    assert time.monotonic() - t0 < 0.5  # dispatch queued the phone message on its own thread
    assert await h.said("the desk camera saw someone here")
    rec = rt.security.dispatch.incidents["incident:e5"]
    assert {(x.recipient, x.channel) for x in rec.deliveries} == {("owner", "console"), ("desk", "console"), ("owner", "phone")}
    rt.security.dispatch.close()


def test_a_phone_channel_set_up_while_running_is_picked_up_without_a_restart(tmp_path):
    d = Dispatcher("dhruv-m4-air", tmp_path / "phone.json", start_worker=False)
    assert d.snapshot()["phone"]["configured"] is False
    path = tmp_path / "phone.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"provider": "ntfy", "topic": TOPIC}, f)
    assert d.snapshot()["phone"]["configured"] is True
    rec = d.observe(incident("incident:e7"), active_session_id="s1")
    assert [x.channel for x in rec.deliveries] == ["console", "phone"]


@pytest.mark.parametrize("server", [
    "http://localhost.attacker.example", "http://127.0.0.1.attacker.example",
    "http://localhost@attacker.example", "https://user:secret@example.com",
    "https://example.com?token=secret", "https://example.com#secret",
    "http://127.0.0.1:70000", "https://example.com\n", "https://",
])
def test_phone_server_rejects_spoofed_loopback_and_secret_url_parts(server):
    with pytest.raises(ValueError):
        NtfyChannel(TOPIC, server)


@pytest.mark.parametrize("server", ["http://127.0.0.1:9999", "http://localhost:9999", "http://[::1]:9999", "https://ntfy.example.com"])
def test_phone_server_accepts_exact_loopback_or_https(server):
    assert NtfyChannel(TOPIC, server).server == server


@pytest.mark.parametrize("reply", [[], None, 42, {"id": []}, {"id": {}}, {"id": " "}, {"id": "x" * 257}])
def test_malformed_provider_receipt_is_a_retryable_channel_failure(monkeypatch, reply):
    def post(*args, **kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return httpx.Response(200, json=reply)
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(TransientError, match="receipt"):
        NtfyChannel(TOPIC).send("test", "test", "default")


@pytest.mark.parametrize("value", [[], None, 42])
def test_nonobject_phone_config_is_reported_without_crashing(tmp_path, value):
    path = tmp_path / "phone.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    channel, error = load_phone_channel(path)
    assert channel is None and "not a valid phone channel" in error


def test_config_removal_before_delivery_fails_explicitly(tmp_path):
    path = tmp_path / "phone.json"
    path.write_text(json.dumps({"provider": "ntfy", "topic": TOPIC}))
    path.chmod(0o600)
    d = Dispatcher("ws", path, start_worker=False)
    rec = d.observe(incident(), active_session_id="s1")
    path.unlink()
    d.deliver(rec.incident_id, rec.deliveries[-1])
    assert rec.deliveries[-1].status == "failed"
    assert rec.deliveries[-1].error == "phone channel is unavailable"


def test_delivery_retry_keeps_one_channel_when_config_changes(tmp_path):
    calls = []
    d = dispatcher(tmp_path, retry_delays=(0,))
    def send(*args):
        calls.append(args)
        if len(calls) == 1:
            d.phone = None  # a concurrent config reload must not crash/change this delivery
            raise TransientError("retry")
        return "receipt"
    d.phone = SimpleNamespace(send=send)
    rec = d.observe(incident(), active_session_id="s1")
    d.deliver(rec.incident_id, rec.deliveries[-1])
    assert len(calls) == 2 and rec.deliveries[-1].status == "accepted"


def test_unexpected_channel_failure_does_not_kill_worker(tmp_path):
    calls, completed = [], threading.Event()
    def send(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("secret must not be exposed")
        completed.set()
        return "receipt"
    d = dispatcher(tmp_path, phone=SimpleNamespace(send=send))
    first = d.observe(incident("first"), active_session_id="s1").deliveries[-1]
    second = d.observe(incident("second"), active_session_id="s1").deliveries[-1]
    worker = threading.Thread(target=d._run)
    worker.start()
    try:
        assert completed.wait(2)
    finally:
        d.close()
        worker.join(2)
    assert not worker.is_alive() and first.status == "failed" and second.status == "accepted"
    assert first.error == "channel failure (RuntimeError)"


@pytest.mark.parametrize("session, active", [("s1", None), (None, "s1"), (None, None)])
def test_missing_session_scope_never_dispatches(tmp_path, session, active):
    d = dispatcher(tmp_path, phone=SimpleNamespace(send=lambda *args: "receipt"))
    rec = d.observe(incident(session=session), active_session_id=active)
    assert rec.skipped == "not the armed session" and not rec.deliveries and d._queue.empty()


def test_queue_pressure_is_bounded_and_eviction_keeps_exact_queued_message(tmp_path):
    sent = []
    d = dispatcher(tmp_path, phone=SimpleNamespace(send=lambda *args: sent.append(args) or "receipt"))
    first = d.observe(incident("first", summary="Exact first incident."), active_session_id="s1")
    for i in range(MAX_PENDING_NOTIFICATIONS):
        last = d.observe(incident(f"incident-{i}"), active_session_id="s1")
    assert d._queue.qsize() == MAX_PENDING_NOTIFICATIONS
    assert len(d.incidents) == MAX_INCIDENTS and "first" not in d.incidents
    assert last.deliveries[-1].status == "failed" and "queue is full" in last.deliveries[-1].error
    iid, delivery, message = d._queue.get_nowait()
    d.deliver(iid, delivery, message=message)
    assert first.deliveries[-1].status == "accepted"
    assert "Exact first incident." in sent[0][1]
    assert "Incident first." in sent[0][1]
