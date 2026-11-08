"""The adapter for Sid's module on maincloud (locki-w91bw). Signatures, statuses and row shapes come from the live server: its schema, its refusal of an
unenrolled identity (530, "Not an enrolled member of this workstation"), and its subscription replies. No test here writes to the team's database."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import websockets

from app.security.contracts import AgentStepV1, LockStatus, SecurityKind, SecuritySource
from app.security.publisher import SecurityEventPublisher
from app.security.session import ResponsePolicy
from app.security.spacetime_adapter import (
    EXPECTED_REDUCERS,
    BaselineReport,
    LockResult,
    RemoteSession,
    SpacetimeConfig,
    SpacetimeSubscriber,
    SpacetimeTransport,
    columns_from_schema,
    contract_problems,
    decode_row,
    incident_from_row,
    load_or_create_token,
    lock_request_from_row,
    reducer_call,
    session_from_row,
)

from .conftest import wait_for
from .test_security_monitor import IDENT, STICK
from .test_security_publisher import HEALTH
from .test_security_service import WARNING, Harness

LIVE = json.loads((Path(__file__).parent / "fixtures" / "locki_w91bw_reducers.json").read_text())["reducers"]
CONFIG = SpacetimeConfig(url="https://db.test", database="locki-w91bw", workstation_id="dhruv-m4-air", device_id="mac-test", token="agent-token")
INCIDENT_COLUMNS = ["id", "workstation_id", "session_id", "severity", "state", "rule_id", "summary", "event_ids_json", "digital_event_id", "physical_event_id", "created_at_ms", "updated_at_ms", "acknowledged_by", "resolved_by", "resolution_note", "action_required", "simulated"]
LOCK_COLUMNS = ["id", "workstation_id", "session_id", "incident_id", "target_device_id", "status", "issued_at_ms", "expires_at_ms", "reason", "owner_policy_hash", "simulated", "result_detail", "updated_at_ms"]
SESSION_COLUMNS = ["id", "workstation_id", "state", "armed_by", "armed_at_ms", "expires_at_ms", "disarmed_at_ms", "baseline_json", "approved_apps_json", "auto_lock_enabled", "owner_policy_hash", "simulated"]


def schema_for(reducers: dict) -> dict:
    return {"reducers": [{"name": n, "params": {"elements": [{"name": {"some": p}, "algebraic_type": {t: []}} for p, t in params]}} for n, params in reducers.items()]}


def event(tmp_path, kind=SecurityKind.USB_DEVICE_ADDED):
    p = SecurityEventPublisher("mac-test", None, directory=tmp_path)
    if kind == SecurityKind.MONITOR_HEALTH:
        return p.build(SecuritySource.MAC_AGENT, kind, HEALTH)
    meta = {"fingerprint": "6f1d2c3b4a5968778695a4b3c2d1e0f0", "has_serial": True, "vendor_id": "0x05ac", "product_id": "0x12a8", "vendor_name": "Apple Inc.", "product_name": "iPhone", "device_class": 0, "interface_classes": [6, 255],
            "category": "phone_or_camera", "location_id": "0x00140000", "allowlist_status": "unknown", "detection": "iokit_notification", "armed": True, "session_id": "dhruv-m4-air-1"}
    return p.build(SecuritySource.MAC_USB, kind, meta)


# ------------------------------------------------------------------ the contract with Sid's module
def test_the_adapter_calls_exactly_the_live_signatures():
    for name, params in EXPECTED_REDUCERS.items():
        assert [list(p) for p in params] == LIVE[name], name
    assert contract_problems(schema_for({n: [tuple(p) for p in LIVE[n]] for n in LIVE})) == []


def test_a_changed_signature_stops_sending_with_the_reason(tmp_path):
    changed = {n: [tuple(p) for p in LIVE[n]] for n in LIVE}
    changed["ingest_device_event"] = changed["ingest_device_event"][:-1]
    problems = contract_problems(schema_for(changed))
    assert problems and "ingest_device_event" in problems[0]
    sent = []
    t = SpacetimeTransport(CONFIG, client=httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json=schema_for(changed)))))
    assert t.check() and t.send(event(tmp_path)).status == "refused" and all(r.method == "GET" for r in sent), "nothing was written"


def test_each_message_goes_to_its_reducer_with_values_of_the_right_type(tmp_path):
    types = {"String": str, "U64": int, "U32": int, "F64": float, "Bool": bool}

    def check(reducer, args):
        params = LIVE[reducer]
        assert len(args) == len(params), reducer
        for (name, kind), value in zip(params, args, strict=True):
            assert isinstance(value, types[kind]) and not (kind != "Bool" and isinstance(value, bool)), (reducer, name, value)

    usb = event(tmp_path)
    reducer, args = reducer_call(usb, CONFIG)
    check(reducer, args)
    assert reducer == "ingest_device_event" and args[:5] == ["dhruv-m4-air", "dhruv-m4-air-1", "mac-test", usb.event_id, "usb_added"] and args[7] == 1.0
    assert json.loads(args[8]) == {"deviceKey": "6f1d2c3b4a5968778695a4b3c2d1e0f0"}, "only the key the live module accepts, as a string"
    assert args[9] is False and args[10] == 1
    reducer, args = reducer_call(event(tmp_path / "h", SecurityKind.MONITOR_HEALTH), CONFIG)
    check(reducer, args)
    assert reducer == "report_health" and args[2:4] == ["usb", "online"], "what the live module accepted for a mac device"
    step = AgentStepV1(run_id="run-1", step=2, tool_name="inspect_usb_event", status="succeeded", evidence_id="e1", duration_ms=3.6, model_id="nemotron-3.5-bolt:e4b (Ollama)", input_type="event_id", args_json='{"event_id": "e1"}')
    reducer, args = reducer_call(step, CONFIG)
    check(reducer, args)
    assert reducer == "record_agent_step" and args[3] == "run-1:2" and args[8] == 4
    assert reducer_call(BaselineReport(observed_at=5, usb={}), CONFIG)[1][4] == "unknown", "the module asks for unknown when there is no active app"
    reducer, args = reducer_call(BaselineReport(observed_at=5, usb={"fp": 2, "other": 1}, active_app_id="com.google.Chrome"), CONFIG)
    check(reducer, args)
    assert reducer == "record_baseline" and json.loads(args[3]) == ["fp", "fp", "other"], "a list of identifiers: the shape the live module accepted"
    big = BaselineReport(observed_at=5, usb={f"fp{i}": 1 for i in range(300)})
    assert len(json.loads(big.baseline_json())) == 256
    reducer, args = reducer_call(LockResult(action_id="a1", status="LOCK_DRY_RUN", detail="dry run"), CONFIG)
    check(reducer, args)


def test_health_the_module_does_not_take_stays_local(tmp_path):
    from app.security.publisher import SecurityEventPublisher

    p = SecurityEventPublisher("mac-test", None, directory=tmp_path)
    publisher_health = p.build(SecuritySource.MAC_AGENT, SecurityKind.MONITOR_HEALTH, {"monitor": "publisher", "status": "started", "method": None, "detail": None})
    assert reducer_call(publisher_health, CONFIG) == ("", [])
    sent = []
    t = transport(lambda r: sent.append(r) or httpx.Response(200))
    assert t.send(publisher_health).status == "local_only" and not [r for r in sent if r.method == "POST"]


def test_no_serial_or_token_goes_into_a_call(tmp_path):
    reducer, args = reducer_call(event(tmp_path), CONFIG)
    text = json.dumps(args)
    assert "agent-token" not in text and "SECRET" not in text


# ------------------------------------------------------------------ statuses
def transport(handler, contract_ok=True):
    def route(request):
        if request.method == "GET":
            return httpx.Response(200, json=schema_for({n: [tuple(p) for p in LIVE[n]] for n in LIVE}))
        return handler(request)

    t = SpacetimeTransport(CONFIG, client=httpx.Client(transport=httpx.MockTransport(route)))
    t.check()
    return t


def test_a_real_success_is_delivered_with_the_agents_own_token(tmp_path):
    seen = []
    t = transport(lambda r: seen.append(r) or httpx.Response(200))
    d = t.send(event(tmp_path))
    assert d.status == "delivered" and t.connected and t.authorized
    assert seen[0].url.path == "/v1/database/locki-w91bw/call/ingest_device_event" and seen[0].headers["authorization"] == "Bearer agent-token"


@pytest.mark.parametrize(
    "code, body, status, authorized",
    [
        (530, "Not an enrolled member of this workstation", "refused", False),  # exactly what the live module said
        (401, "", "refused", False),
        (403, "forbidden", "refused", False),
        (530, "duplicate event id", "refused", None),
        (404, "No such procedure", "refused", None),
        (500, "internal", "failed", None),
        (503, "unavailable", "failed", None),
    ],
)
def test_refusals_authorization_failures_and_server_trouble(tmp_path, code, body, status, authorized):
    t = transport(lambda r: httpx.Response(code, text=body))
    d = t.send(event(tmp_path))
    assert d.status == status and t.authorized == authorized
    if authorized is False:
        assert "not authorized" in d.detail and "enrolled" in (t.last_refusal or "") or code != 530


def test_an_unreachable_server_is_retried_not_refused(tmp_path):
    def down(request):
        raise httpx.ConnectError("no route")

    t = transport(down)
    assert t.send(event(tmp_path)).status == "failed" and not t.connected


def test_a_refused_event_does_not_hold_up_the_next(tmp_path):
    answers = iter([httpx.Response(530, text="Not an enrolled member of this workstation"), httpx.Response(200)])
    t = transport(lambda r: next(answers))
    p = SecurityEventPublisher("mac-test", t, directory=tmp_path)
    p.publish(event(tmp_path / "a"))
    p.publish(event(tmp_path / "b"))
    assert p.flush() and p.refused == 1 and p.last_delivery.status == "delivered"
    p.close()


def test_the_agents_token_stays_in_an_owner_only_file(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **k: (calls.append(url), httpx.Response(200, json={"identity": "abc", "token": "tok"}, request=httpx.Request("POST", url)))[1])
    assert load_or_create_token("https://db.test", tmp_path) == "tok" == load_or_create_token("https://db.test", tmp_path)
    assert len(calls) == 1 and next(tmp_path.glob("spacetime-*.json")).stat().st_mode & 0o077 == 0


# ------------------------------------------------------------------ rows from Sid's views
def row(columns, **values):
    return {c: values.get(c, "" if c not in ("action_required", "simulated", "auto_lock_enabled") else False) for c in columns}


def test_an_incident_row_becomes_the_contract():
    update = incident_from_row(row(INCIDENT_COLUMNS, id="inc-1", workstation_id="dhruv-m4-air", session_id="s1", severity="HIGH", state="suspected_intrusion", event_ids_json='["e1","j1"]', digital_event_id="e1", physical_event_id="j1", action_required=True, updated_at_ms=5, rule_id="usb_plus_person"))
    assert update.severity == "high" and update.state == "SUSPECTED_INTRUSION" and update.evidence_ids == ["e1", "j1"] and update.action_required == "review" and update.physical_event_id == "j1"


def test_a_lock_request_row_with_no_policy_hash_is_never_acted_on():
    with pytest.raises(ValueError):
        lock_request_from_row(row(LOCK_COLUMNS, id="a1", session_id="s1", expires_at_ms=5, reason="x"))


def test_a_session_row_knows_when_it_is_armed_and_reads_this_macs_baseline():
    now = int(time.time() * 1000)
    s = session_from_row(row(SESSION_COLUMNS, id="s1", workstation_id="dhruv-m4-air", state="ARMED", armed_at_ms=now, expires_at_ms=now + 60000, baseline_json='["fp", "fp", "hub"]', approved_apps_json='["com.x"]', auto_lock_enabled=True, owner_policy_hash="hash-1234"))
    assert s.active() and s.baseline() == {"fp": 2, "hub": 1} and s.approved_apps() == ["com.x"] and s.auto_lock_enabled
    assert not session_from_row(row(SESSION_COLUMNS, id="s1", state="DISARMED", expires_at_ms=now + 60000)).active()
    assert not session_from_row(row(SESSION_COLUMNS, id="s1", state="ARMED", expires_at_ms=now - 1)).active()
    assert not session_from_row(row(SESSION_COLUMNS, id="s1", state="ARMED", expires_at_ms=now + 1000, disarmed_at_ms=now)).active()
    assert session_from_row(row(SESSION_COLUMNS, id="s1", state="ARMED", expires_at_ms=now + 1000, baseline_json="not ours")).baseline() is None


def test_both_row_shapes_the_server_sends_decode_the_same():
    names, options = columns_from_schema({"elements": [{"name": {"some": c}, "algebraic_type": {"String": []}} for c in ("id", "state")]})
    assert decode_row('{"id": "s1", "state": "ARMED"}', names, options) == decode_row('["s1", "ARMED"]', names, options) == {"id": "s1", "state": "ARMED"}


async def test_rows_for_another_workstation_or_device_or_a_finished_request_are_ignored():
    acted = []
    sub = SpacetimeSubscriber(CONFIG, on_incident=acted.append, on_lock_request=acted.append, on_session=acted.append)
    await sub._row("my_incidents", row(INCIDENT_COLUMNS, id="i", workstation_id="someone-else", severity="low", state="ARMED"))
    await sub._row("my_lock_requests", row(LOCK_COLUMNS, id="a", workstation_id="dhruv-m4-air", target_device_id="mac-other", session_id="s", expires_at_ms=9, owner_policy_hash="hash-1234"))
    await sub._row("my_lock_requests", row(LOCK_COLUMNS, id="b", workstation_id="dhruv-m4-air", status="confirmed", session_id="s", expires_at_ms=9, owner_policy_hash="hash-1234"))
    await sub._row("my_incidents", row(INCIDENT_COLUMNS, id="i2", workstation_id="dhruv-m4-air", severity="catastrophic", state="ARMED"))
    assert acted == [] and sub.rows_ignored == 3 and sub.rows_rejected == 1


# ------------------------------------------------------------------ reconnects, against a stand-in WebSocket server
async def test_the_subscription_reconnects_and_resubscribes_after_the_server_drops_it(monkeypatch):
    monkeypatch.setattr("app.security.spacetime_adapter.RECONNECT_MIN_S", 0.05)
    connections = []
    now = int(time.time() * 1000)
    lock = json.dumps(["a1", "dhruv-m4-air", "s1", "inc-1", "", "pending", now, now + 30000, "unknown device", "hash-1234", False, "", now])

    async def handler(ws):
        connections.append(1)
        await ws.send(json.dumps({"IdentityToken": {"identity": {"__identity__": "0x1"}, "token": "x"}}))
        subscribe = json.loads(await ws.recv())
        assert subscribe["Subscribe"]["query_strings"] == ["SELECT * FROM my_sessions", "SELECT * FROM my_incidents", "SELECT * FROM my_lock_requests"]
        await ws.send(json.dumps({"InitialSubscription": {"database_update": {"tables": []}, "request_id": 1}}))
        if len(connections) == 1:
            await ws.close()  # the server goes away once
            return
        await ws.send(json.dumps({"TransactionUpdateLight": {"request_id": 0, "update": {"tables": [{"table_name": "my_lock_requests", "updates": [{"deletes": [], "inserts": [lock]}]}]}}}))
        await asyncio.sleep(5)

    got = []
    async with websockets.serve(handler, "127.0.0.1", 0, subprotocols=["v1.json.spacetimedb"]) as server:
        port = server.sockets[0].getsockname()[1]
        config = CONFIG.model_copy(update={"url": f"http://127.0.0.1:{port}"})
        sub = SpacetimeSubscriber(config, on_incident=got.append, on_lock_request=got.append, on_session=got.append)

        async def columns():
            sub._columns = {"my_sessions": (SESSION_COLUMNS, [False] * 12), "my_incidents": (INCIDENT_COLUMNS, [False] * 17), "my_lock_requests": (LOCK_COLUMNS, [False] * 13)}

        sub._load_columns = columns
        sub.start()
        assert await wait_for(lambda: bool(got), timeout=5)
        await sub.stop()
    assert len(connections) == 2 and sub.reconnects >= 1 and got[0].action_id == "a1" and got[0].owner_policy_hash == "hash-1234"


async def test_with_no_server_it_keeps_trying_and_says_why():
    sub = SpacetimeSubscriber(CONFIG.model_copy(update={"url": "http://127.0.0.1:9", "timeout_s": 0.2}), on_incident=print, on_lock_request=print, on_session=print)
    sub.start()
    assert await wait_for(lambda: sub.reconnects >= 1, timeout=3)
    assert not sub.connected and sub.last_error
    await sub.stop()


# ------------------------------------------------------------------ the service, following the database
class Recording:
    name = "spacetimedb"
    connected = True
    authorized = True
    contract: list = []
    last_refusal = None

    def __init__(self):
        self.messages = []

    def send(self, message):
        from app.security.publisher import Delivery

        self.messages.append(message)
        return Delivery("delivered", self.name)


def remote(session_id="s1", state="ARMED", auto_lock=False, baseline=None, policy_hash="hash-1234"):
    now = int(time.time() * 1000)
    return RemoteSession(session_id, state, now, now + 60000, 0, BaselineReport(observed_at=now, usb=baseline or {}).baseline_json(), "[]", auto_lock, policy_hash, False)


async def following(rt):
    h = Harness(rt)
    rt.security.transport = Recording()
    rt.security.subscriber = SpacetimeSubscriber(CONFIG, on_incident=print, on_lock_request=print, on_session=print)  # present, so the database arms
    await rt.security.start_monitoring()
    return h


async def test_the_database_arms_and_disarms_and_locki_says_only_what_changed(rt):
    h = await following(rt)
    with pytest.raises(Exception, match="SpacetimeDB owns arming"):
        await rt.security.arm("orange-7")
    r = await rt.handle_utterance("LOCKI, arm my workstation", source="text", stt_confidence=1.0)
    assert "dashboard" in r.reply and rt.security.session is None
    rt.security.apply_remote_session(remote())
    assert rt.security.session.source == "spacetimedb" and rt.security.session.session_id == "s1" and await h.said("Armed.")
    rt.security.apply_remote_session(remote())  # the same row again: nothing new
    assert sum("Armed." in s for s in h.spoken()) == 1
    rt.security.apply_remote_session(remote(state="DISARMED"))
    assert rt.security.session is None and await h.said("Disarmed.")


async def test_a_baseline_goes_to_the_database_and_the_session_uses_the_one_it_was_armed_with(rt):
    h = await following(rt)
    assert rt.security.publisher.flush()
    baselines = [m for m in rt.security.transport.messages if isinstance(m, BaselineReport)]
    assert baselines and baselines[-1].usb == rt.security.monitor.fingerprints()
    rt.security.apply_remote_session(remote(baseline=rt.security.monitor.fingerprints()))
    h.usb.plug(STICK)
    assert await h.said(WARNING) and rt.security.publisher.flush()
    usb = [m for m in rt.security.transport.messages if getattr(m, "kind", None) == SecurityKind.USB_DEVICE_ADDED]
    assert usb and usb[-1].metadata["session_id"] == "s1" and usb[-1].metadata["allowlist_status"] == "unknown"


async def test_a_lock_request_from_the_database_runs_every_check_and_reports_back(rt):
    h = await following(rt)
    rt.security.apply_remote_session(remote(auto_lock=True))
    request = lock_request_from_row(row(LOCK_COLUMNS, id="a1", workstation_id="dhruv-m4-air", session_id="s1", expires_at_ms=int(time.time() * 1000) + 30000, reason="unknown device", owner_policy_hash="hash-1234"))
    result = await rt.security.handle_lock_request(request)
    assert result.status == LockStatus.LOCK_DRY_RUN and h.ran == []
    rt.security.publisher.flush()
    sent = [m for m in rt.security.transport.messages if isinstance(m, LockResult)]
    assert [m.status for m in sent] == ["LOCK_REQUESTED", "LOCK_DRY_RUN"] and all(m.action_id == "a1" for m in sent)
    wrong = await rt.security.handle_lock_request(request.model_copy(update={"action_id": "a2", "owner_policy_hash": "someone-elses"}))
    assert wrong.status == LockStatus.LOCK_REFUSED and "policy" in wrong.detail


async def test_with_the_database_connected_the_mac_does_not_lock_on_its_own(rt):
    h = await following(rt)
    rt.security.apply_remote_session(remote(auto_lock=True))
    h.usb.plug(STICK)
    assert await h.said(WARNING)
    await asyncio.sleep(0.05)
    assert not rt.security.locks


async def test_an_incident_the_mac_could_not_see_by_itself_warns_once(rt):
    from app.security.contracts import IncidentUpdateV1

    h = await following(rt)
    rt.security.apply_remote_session(remote())
    update = IncidentUpdateV1(incident_id="inc-1", session_id="s1", severity="high", state="SUSPECTED_INTRUSION", evidence_ids=["e1", "j1"], digital_event_id="e1", physical_event_id="j1")
    rt.security.apply_incident_update(update)
    assert await h.said("the desk camera saw someone here")
    rt.security.apply_incident_update(update)
    assert sum("desk camera" in s for s in h.spoken()) == 1
    assert rt.security.incident["source"] == "spacetimedb" and rt.security.incident["authoritative"] is True


async def test_an_incident_for_a_device_already_warned_about_is_not_warned_again(rt):
    from app.security.contracts import IncidentUpdateV1

    h = await following(rt)
    rt.security.apply_remote_session(remote())
    h.usb.plug(STICK)
    assert await h.said(WARNING)
    event_id = rt.security.warnings[-1]["event_id"]
    rt.security.apply_incident_update(IncidentUpdateV1(incident_id="inc-2", session_id="s1", severity="medium", state="SUSPECTED_INTRUSION", evidence_ids=[event_id], digital_event_id=event_id))
    await asyncio.sleep(0.05)
    assert sum(WARNING in s for s in h.spoken()) == 1 and rt.security.incident["incident_id"] == "inc-2"


async def test_a_simulated_incident_is_shown_and_never_spoken(rt):
    from app.security.contracts import IncidentUpdateV1

    h = await following(rt)
    rt.security.apply_remote_session(remote())
    rt.security.apply_incident_update(IncidentUpdateV1(incident_id="inc-3", session_id="s1", severity="high", state="SUSPECTED_INTRUSION", evidence_ids=["x"], is_fixture=True))
    await asyncio.sleep(0.05)
    assert not any(WARNING in s for s in h.spoken()) and rt.security.incident is None


INCIDENT_LINE = "the security system has opened an incident"


def live_incident(updated_at_ms, incident_id="incident:4d3d6e546b8d4af7a0bf233b3a05c725", physical=""):
    """A my_incidents row exactly as maincloud sent it to the enrolled Mac on 2026-11-07 (state "open"), with only the times and session changed."""
    return [incident_id, "dhruv-m4-air", "s1", "medium", "open", "unknown_usb", "New unapproved USB device detected while armed.", json.dumps(["4d3d6e546b8d4af7a0bf233b3a05c725"] + ([physical] if physical else [])),
            "4d3d6e546b8d4af7a0bf233b3a05c725", physical, updated_at_ms, updated_at_ms, "", "", "", True, False]


def incident_message(rows, initial=False):
    tables = [{"table_name": "my_incidents", "updates": [{"deletes": [], "inserts": [json.dumps(r) for r in rows]}]}]
    return json.dumps({"InitialSubscription": {"database_update": {"tables": tables}, "request_id": 1}} if initial else {"TransactionUpdateLight": {"request_id": 0, "update": {"tables": tables}}})


async def subscribed(rt):
    h = await following(rt)
    rt.security.apply_remote_session(remote())
    sub = SpacetimeSubscriber(CONFIG, on_incident=rt.security.apply_incident_update, on_lock_request=print, on_session=rt.security.apply_remote_session)
    sub._columns = {"my_incidents": (INCIDENT_COLUMNS, [False] * 17)}
    return h, sub


async def test_a_fresh_server_incident_reaches_the_voice_once_even_after_a_reconnect(rt):
    h, sub = await subscribed(rt)
    now = int(time.time() * 1000)
    await sub._message(incident_message([live_incident(now)]))
    assert await h.said(INCIDENT_LINE)
    record = rt.security.warnings[-1]
    assert record["reason"] == "incident" and record["severity"] == "medium" and record["server_updated_at_ms"] == now and record["received_at_ms"] >= now
    assert rt.security.incident["state"] == "SUSPECTED_INTRUSION" and rt.security.incident["summary"].startswith("New unapproved USB device")
    await sub._message(incident_message([live_incident(now)], initial=True))  # a reconnect re-delivers every row
    await asyncio.sleep(0.1)
    assert sum(INCIDENT_LINE in s for s in h.spoken()) == 1 and sub.rows_rejected == 0


async def test_an_old_incident_in_the_live_session_is_shown_and_never_spoken(rt):
    from app.security.service import INCIDENT_VOICE_MAX_AGE_MS

    h, sub = await subscribed(rt)
    old = int(time.time() * 1000) - INCIDENT_VOICE_MAX_AGE_MS - 5_000
    await sub._message(incident_message([live_incident(old)], initial=True))  # e.g. the backend restarted mid-session
    await asyncio.sleep(0.1)
    assert not any(INCIDENT_LINE in s for s in h.spoken()) and rt.security.incident["incident_id"].startswith("incident:")
    await sub._message(incident_message([live_incident(old)]))
    await asyncio.sleep(0.1)
    assert not any("Security alert" in s for s in h.spoken())


async def test_a_server_incident_with_camera_evidence_says_what_the_camera_saw(rt):
    h, sub = await subscribed(rt)
    await sub._message(incident_message([live_incident(int(time.time() * 1000), incident_id="incident:corr-1", physical="jetson-obs-1")]))
    assert await h.said("the desk camera saw someone here")
    assert rt.security.warnings[-1]["reason"] == "correlated" and "jetson-obs-1" in rt.security.warnings[-1]["evidence_ids"]


async def test_an_incident_for_a_disarmed_session_is_never_spoken(rt):
    h = await following(rt)
    sub = SpacetimeSubscriber(CONFIG, on_incident=rt.security.apply_incident_update, on_lock_request=print, on_session=print)
    sub._columns = {"my_incidents": (INCIDENT_COLUMNS, [False] * 17)}
    await sub._message(incident_message([live_incident(int(time.time() * 1000))], initial=True))
    await asyncio.sleep(0.1)
    assert not any("Security alert" in s for s in h.spoken()) and rt.security.incident is None


def test_the_service_never_reads_the_owners_cli_login():
    source = (Path(__file__).resolve().parents[1] / "app" / "security" / "spacetime_adapter.py").read_text() + (Path(__file__).resolve().parents[1] / "app" / "security" / "service.py").read_text()
    assert "cli.toml" not in source and ".config/spacetime" not in source
    assert IDENT.device_id == "mac-test" and ResponsePolicy().auto_lock is False


async def test_a_heartbeat_reports_each_monitors_real_state_every_few_seconds(rt):
    from app.security.spacetime_adapter import HealthBeat

    h = await following(rt)
    rt.security.heartbeat()
    rt.security.heartbeat()  # too soon: nothing more
    assert rt.security.publisher.flush()
    beats = [m for m in rt.security.transport.messages if isinstance(m, HealthBeat)]
    assert [(b.component, b.status) for b in beats] == [("usb", "online"), ("apps", "online")]
    assert reducer_call(beats[0], CONFIG) == ("report_health", ["dhruv-m4-air", "mac-test", "usb", "online", "iokit_notification"])
    rt.security.monitor.errors["usb"] = "IOKit stopped answering"
    rt.security._last_beat = 0.0
    rt.security.heartbeat()
    assert rt.security.publisher.flush()
    assert [(b.component, b.status) for b in rt.security.transport.messages if isinstance(b, HealthBeat)][-2:] == [("usb", "degraded"), ("apps", "online")]
    log = "".join(p.read_text() for p in rt.security.dir.glob("events-*.jsonl"))
    assert "healthbeat" not in log, "heartbeats stay out of the local log"
    assert h is not None


async def test_the_last_change_of_a_burst_always_reaches_the_database(rt, monkeypatch):
    """From a real run: a second hub that came back inside the gap after a report was never reported, so the owner's arm was refused."""
    from app.security import service as service_module
    from app.security.mac_usb import UsbDevice

    monkeypatch.setattr(service_module, "BASELINE_MIN_GAP_S", 0.2)
    h = await following(rt)
    assert rt.security.publisher.flush()
    second = UsbDevice(41, 0x05e3, 0x0608, product_name="USB2.0 Hub", location_id=0x120000)
    h.usb.plug(second)  # within the gap after the start-up report
    assert await wait_for(lambda: rt.security.publisher.flush(0.1) and any(isinstance(m, BaselineReport) and sum(m.usb.values()) == 2 for m in rt.security.transport.messages), timeout=3)


def test_events_go_out_in_the_shape_the_module_accepts_and_the_rest_stays_local(tmp_path):
    p = SecurityEventPublisher("mac-test", None, directory=tmp_path)
    app = p.build(SecuritySource.MAC_APP, SecurityKind.APP_LAUNCHED, {
        "name": "Calculator", "bundle_id": "com.apple.calculator", "executable_path": "/System/Applications/Calculator.app/Contents/MacOS/Calculator", "pid": 9, "launched_at": 1,
        "signature": {"status": "signed", "identifier": None, "team_id": None, "authority": None}, "watchlist_match": True, "watchlist_category": "demo_test_app", "detection": "nsworkspace_notification", "armed": True, "session_id": "s1"})
    reducer, args = reducer_call(app, CONFIG)
    assert args[4] == "app_launched" and json.loads(args[8]) == {"appId": "com.apple.calculator"}
    removed = event(tmp_path / "r", SecurityKind.USB_DEVICE_REMOVED)
    assert reducer_call(removed, CONFIG)[1][4] == "usb_removed"
    gate = p.build(SecuritySource.MAC_GATE, SecurityKind.PROTECTED_TOOL_DECISION, {"run_id": "r", "tool_name": "x", "decision": "denied", "reason": None, "callback_ran": False})
    assert reducer_call(gate, CONFIG) == ("", []), "no kind for it in the module yet: it stays local"
    for value in json.loads(reducer_call(event(tmp_path / "u"), CONFIG)[1][8]).values():
        assert isinstance(value, str)


def test_the_first_real_incident_row_from_the_module_maps_onto_the_contract():
    """Exactly as Sid's module wrote it for the iPhone at 16:47 (incident:4d3d6e54...)."""
    update = incident_from_row(row(INCIDENT_COLUMNS, id="incident:4d3d6e546b8d4af7a0bf233b3a05c725", workstation_id="dhruv-m4-air", session_id="dhruv-m4-air-1794091173", severity="medium", state="open", rule_id="unknown_usb",
                                   summary="New unapproved USB device detected while armed.", event_ids_json='["4d3d6e546b8d4af7a0bf233b3a05c725"]', action_required=True))
    assert update.state == "SUSPECTED_INTRUSION" and update.severity == "medium" and update.evidence_ids == ["4d3d6e546b8d4af7a0bf233b3a05c725"] and update.action_required == "review" and update.rule_id == "unknown_usb"


def test_camera_events_go_to_the_module_under_the_prd_names_with_only_the_zone():
    import json as _json

    from app.security.contracts import SecurityEventV1, SecurityKind, SecuritySource
    from app.security.spacetime_adapter import SpacetimeConfig, reducer_call

    config = SpacetimeConfig(url="http://x", database="d", workstation_id="ws", device_id="cam-1")

    def cam(kind, **meta):
        return SecurityEventV1(event_id="e1", device_id="cam-1", source=SecuritySource.MAC_VISION, kind=SecurityKind(kind), observed_at=1794100000000, seq=3, confidence=0.8,
                               metadata={"camera": "mac_webcam", "inference": "mac_mediapipe", "armed": True, "session_id": "s1", **meta})

    name, args = reducer_call(cam("person_in_zone", zone="desk", dwell_ms=300), config)
    assert name == "ingest_device_event" and args[4] == "person_in_zone" and args[1] == "s1" and args[2] == "cam-1"
    assert _json.loads(args[8]) == {"label": "desk", "captureMode": "mac_webcam:mac_mediapipe"}  # Sid's keys only; dwell and the rest stay on the Mac
    assert reducer_call(cam("person_left_zone", zone="desk"), config)[1][4] == "person_cleared"
    assert reducer_call(cam("camera_obstructed", detail="dark"), config)[1][4] == "camera_offline"
    assert _json.loads(reducer_call(cam("camera_obstructed", detail="dark"), config)[1][8])["detail"] == "dark"
    assert reducer_call(cam("camera_restored"), config)[1][4] == "camera_online"
    for local_only in ("owner_face_match", "unrecognized_face", "usb_zone_interaction", "keyboard_interaction", "hand_near_keyboard"):
        assert reducer_call(cam(local_only), config) == ("", []), local_only
    renamed = SpacetimeConfig(url="http://x", database="d", workstation_id="ws", device_id="cam-1", camera_kinds={"usb_zone_interaction": "hand_near_usb"})
    assert reducer_call(cam("usb_zone_interaction", zone="usb_port_zone"), renamed)[1][4] == "hand_near_usb"
    assert reducer_call(cam("person_in_zone"), renamed) == ("", [])  # an override replaces the whole map


# ------------------------------------------------------------------ a remote camera (jetson-01) through my_device_events
DEVICE_EVENT_COLUMNS = ["id", "workstation_id", "session_id", "device_id", "source", "kind", "observed_at_ms", "received_at_ms", "seq", "confidence", "metadata_json", "simulated", "schema_version", "classification"]
CAMERA_CONFIG = CONFIG.model_copy(update={"remote_cameras": ("jetson-01",)})


def jetson_row(kind="person_in_zone", observed_at_ms=None, session_id="s1", device_id="jetson-01", source="jetson", simulated=False, event_id=None, received_at_ms=None):
    """A my_device_events row in the column order maincloud sends (read from the live view on 2026-11-08), with jetson-01's metadata keys.
    observed_at_ms is the camera's clock; received_at_ms is the server's."""
    now = int(time.time() * 1000)
    at = observed_at_ms if observed_at_ms is not None else now
    got = received_at_ms if received_at_ms is not None else now
    meta = {"label": "desk", "captureMode": "jetson_usb_camera", "detail": "state=streaming fps=15.47"}
    return [event_id or f"jetson-01:{kind}:{at}", "dhruv-m4-air", session_id, device_id, source, kind, at, got, 7, 0.82, json.dumps(meta), simulated, 1, "soft_observation"]


def device_event_message(rows, initial=False):
    tables = [{"table_name": "my_device_events", "updates": [{"deletes": [], "inserts": [json.dumps(r) for r in rows]}]}]
    return json.dumps({"InitialSubscription": {"database_update": {"tables": tables}, "request_id": 1}} if initial else {"TransactionUpdateLight": {"request_id": 0, "update": {"tables": tables}}})


def test_a_listed_remote_camera_row_becomes_a_local_observation_and_everything_else_is_ignored():
    from app.security.spacetime_adapter import remote_camera_event

    allowed = ("jetson-01",)
    row = dict(zip(DEVICE_EVENT_COLUMNS, jetson_row(received_at_ms=1794120000040)))
    event, session, server_ms = remote_camera_event(row, "mac-test", allowed)
    assert event.kind == SecurityKind.PERSON_IN_ZONE and event.source == SecuritySource.JETSON_VISION and event.device_id == "jetson-01" and session == "s1" and server_ms == 1794120000040
    assert event.metadata["inference"] == "unreported" and event.metadata["zone"] == "desk" and event.metadata["camera"] == "jetson_usb_camera" and not event.is_fixture
    cleared, _, _ = remote_camera_event(dict(zip(DEVICE_EVENT_COLUMNS, jetson_row("person_cleared"))), "mac-test", allowed)
    assert cleared.kind == SecurityKind.PERSON_LEFT_ZONE
    for bad in (jetson_row(device_id="mac-test"), jetson_row(device_id="mac-dev-cam-01"), jetson_row(simulated=True), jetson_row(source="fixture"),
                jetson_row(source="mac"), jetson_row("usb_added"), jetson_row("heartbeat")):
        assert remote_camera_event(dict(zip(DEVICE_EVENT_COLUMNS, bad)), "mac-test", allowed) is None
    assert remote_camera_event(row, "mac-test", ()) is None  # nobody listed: no remote camera drives this Mac


def test_the_mac_follows_device_events_only_for_listed_cameras():
    assert "my_device_events" not in SpacetimeSubscriber(CAMERA_CONFIG, on_incident=print, on_lock_request=print, on_session=print)._views()
    assert "my_device_events" not in SpacetimeSubscriber(CONFIG, on_incident=print, on_lock_request=print, on_session=print, on_camera_event=print)._views()
    assert "my_device_events" in SpacetimeSubscriber(CAMERA_CONFIG, on_incident=print, on_lock_request=print, on_session=print, on_camera_event=print)._views()


async def camera_subscribed(rt):
    h = await following(rt)
    rt.security.apply_remote_session(remote())
    sub = SpacetimeSubscriber(CAMERA_CONFIG, on_incident=print, on_lock_request=print, on_session=print, on_camera_event=rt.security.apply_remote_camera_event)
    sub._columns = {"my_device_events": (DEVICE_EVENT_COLUMNS, [False] * len(DEVICE_EVENT_COLUMNS))}
    return h, sub


async def test_a_real_jetson_person_while_armed_makes_locki_watchful_and_warn_once(rt):
    h, sub = await camera_subscribed(rt)
    row = jetson_row()
    await sub._message(device_event_message([row]))
    assert rt.security.mode.value == "WATCHFUL" and rt.security.visual_state()["at_desk"]
    await wait_for(lambda: any(w["reason"] == "person_at_desk" for w in rt.security.warnings), timeout=4.0)
    warning = next(w for w in rt.security.warnings if w["reason"] == "person_at_desk")
    timing = warning["remote_timing"]
    assert warning["device_id"] == "jetson-01" and timing["server_to_m4_ms"] is not None and timing["camera_to_server_ms"] is not None and timing["acted_on"]
    await sub._message(device_event_message([row], initial=True))  # a reconnect re-delivers the same row
    await asyncio.sleep(0.1)
    assert sum(w["reason"] == "person_at_desk" for w in rt.security.warnings) == 1 and sub.camera_rows == 2 and sub.rows_rejected == 0


async def test_a_camera_clock_minutes_behind_still_warns_because_age_is_the_servers(rt):
    h, sub = await camera_subscribed(rt)
    await sub._message(device_event_message([jetson_row(observed_at_ms=int(time.time() * 1000) - 95_000)]))  # Jetson clock 95 s behind, received just now
    assert rt.security.remote_camera["acted_on"] and rt.security.mode.value == "WATCHFUL"


async def test_another_enrolled_camera_never_drives_this_desk(rt):
    h, sub = await camera_subscribed(rt)
    await sub._message(device_event_message([jetson_row(device_id="mac-dev-cam-01")]))
    await asyncio.sleep(1.8)
    assert rt.security.remote_camera is None and rt.security.mode.value == "NORMAL" and sub.rows_ignored == 1


async def test_old_or_other_session_or_disarmed_camera_rows_are_never_spoken(rt):
    from app.security.service import REMOTE_CAMERA_MAX_AGE_MS

    h, sub = await camera_subscribed(rt)
    old = int(time.time() * 1000) - REMOTE_CAMERA_MAX_AGE_MS - 1_000
    await sub._message(device_event_message([jetson_row(received_at_ms=old, event_id="replayed")], initial=True))  # the server got it long ago: a replay
    assert rt.security.remote_camera["why_not"] == "stale"
    await sub._message(device_event_message([jetson_row(session_id="an-older-session", event_id="other-session")]))
    assert rt.security.remote_camera["why_not"] == "not the armed session"
    rt.security.apply_remote_session(remote(state="DISARMED"))
    await sub._message(device_event_message([jetson_row(event_id="after-disarm")]))
    await asyncio.sleep(1.8)
    assert rt.security.mode.value == "NORMAL" and not any(w["reason"] == "person_at_desk" for w in rt.security.warnings)


async def test_a_bug_in_the_camera_handler_is_not_counted_as_a_bad_row():
    def broken(*_):
        raise RuntimeError("handler bug")

    sub = SpacetimeSubscriber(CAMERA_CONFIG, on_incident=print, on_lock_request=print, on_session=print, on_camera_event=broken)
    sub._columns = {"my_device_events": (DEVICE_EVENT_COLUMNS, [False] * len(DEVICE_EVENT_COLUMNS))}
    await sub._message(device_event_message([jetson_row()]))
    assert sub.rows_rejected == 0 and sub.camera_rows == 1 and "camera handler failed" in sub.last_error

