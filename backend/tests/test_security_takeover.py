"""The native takeover is a read-only derivation of what the SecurityService already holds: only a genuine CRITICAL fact for the live
session takes over the screen, one record keeps one id, reports from the alert window never touch security state, every response line
restates a real record, and the endpoints answer only the native window (its token, no Origin)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
import uuid

import httpx
import pytest

from app.config import PROJECT_ROOT, RuntimeSettings
from app.main import create_app
from app.runtime import LockedInRuntime
from app.security import takeover as tk
from app.security.contracts import IncidentUpdateV1, LockStatus, SecurityEventV1, SecurityKind, SecuritySource
from app.security.modes import SecurityMode
from app.security.session import ResponsePolicy

from .conftest import wait_for
from .test_security_demo import FakeDemoCamera
from .test_security_monitor import STICK
from .test_security_service import CODE, Harness

M = SecurityMode


@pytest.fixture
async def h(rt):
    harness = Harness(rt)
    yield harness
    await rt.security.close()


def now_ms() -> int:
    return int(time.time() * 1000)


def decide(rt, **kw) -> dict:
    return rt.security.takeover.decide(rt.security, **kw)


def camera(kind: str, *, remote: bool = False, session_id: str | None = None, confidence: float | None = 0.91, track: int | None = None) -> SecurityEventV1:
    meta = {"camera": "jetson_cam" if remote else "mac_webcam", "inference": "unreported" if remote else "mac_mediapipe", "armed": True, "session_id": session_id}
    if kind == "person_in_zone":
        meta["zone"] = "desk"
    if track is not None:
        meta["track_id"] = track
    return SecurityEventV1(event_id=uuid.uuid4().hex, device_id="jetson-01" if remote else "cam-test", source=SecuritySource.JETSON_VISION if remote else SecuritySource.MAC_VISION,
                           kind=SecurityKind(kind), observed_at=now_ms(), seq=1, confidence=confidence, metadata=meta)


def usb_event(device_id: str, *, fixture: bool = False, observed_at: int | None = None) -> SecurityEventV1:
    meta = {"fingerprint": "f" * 16, "has_serial": False, "vendor_id": "0x0781", "product_id": "0x5583", "category": "storage",
            "allowlist_status": "unknown", "detection": "iokit_notification", "armed": True}
    return SecurityEventV1(event_id=uuid.uuid4().hex, device_id=device_id, source=SecuritySource.MAC_USB, kind=SecurityKind.USB_DEVICE_ADDED,
                           observed_at=observed_at or now_ms(), seq=1, metadata=meta, is_fixture=fixture)


def incident(rt, **kw) -> IncidentUpdateV1:
    t = now_ms()
    fields = {"incident_id": "inc-1", "session_id": rt.security.session.session_id, "severity": "high", "state": "SUSPECTED_INTRUSION",
              "digital_event_id": "dev-1", "physical_event_id": "cam-1", "evidence_ids": ["dev-1", "cam-1"], "created_at": t, "updated_at": t,
              "rule_id": "correlated_usb_physical"}
    return IncidentUpdateV1(**{**fields, **kw})


async def plugged(h) -> SecurityEventV1:
    """A real (stand-in watcher) unknown USB arrival while armed: CRITICAL on this Mac's own fact."""
    h.usb.plug(STICK)
    assert await wait_for(lambda: h.rt.security.mode == M.CRITICAL)
    return next(e for e in reversed(h.rt.security.evidence) if e.kind == SecurityKind.USB_DEVICE_ADDED)


async def quiet(rt) -> None:
    """Every queued line has finished, so the speaker's history cannot grow underneath a before/after comparison."""
    assert await wait_for(lambda: not rt.speaker.speaking and not getattr(rt.speaker, "_items", ()), timeout=3.0)
    await asyncio.sleep(0.05)


def security_state(rt) -> tuple:
    s = rt.security
    return (s.session is not None, None if s.session is None else s.session.session_id, s.mode.value, json.dumps(s.incident, sort_keys=True, default=str),
            [e.event_id for e in s.evidence], json.dumps(list(s.locks), default=str), len(s.warnings), rt.tool_gate.frozen, len(s.modes.transitions),
            None if s._last_incident_update is None else s._last_incident_update.incident_id, s.modes.state.evidence_ids, len(rt.speaker.history))


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)


def claims(record: dict) -> list[str]:
    return [s for s in strings(record) if tk.CLAIM.search(s)]


# ------------------------------------------------------------------ what never takes over
async def test_someone_in_view_alone_or_defensive_never_takes_over(h):
    rt = h.rt
    await rt.security.arm(CODE)
    rt.security.on_camera_event(camera("person_in_zone"))
    rt.security.on_camera_event(camera("unrecognized_face", track=1))
    assert rt.security.mode == M.WATCHFUL
    d = decide(rt)
    assert d["armed"] and not d["active"] and d["takeover"] is None and d["blocked"] == ["no_critical_evidence"]
    rt.security.on_camera_event(camera("keyboard_interaction"))
    assert rt.security.mode == M.DEFENSIVE and rt.security.incident is not None  # a local incident is open, and still no takeover
    d = decide(rt)
    assert not d["active"] and d["takeover"] is None and d["blocked"] == ["no_critical_evidence"]


async def test_a_disarmed_session_never_takes_over(h):
    rt = h.rt
    await rt.security.start_monitoring()
    h.usb.plug(STICK)
    await asyncio.sleep(0.05)
    d = decide(rt)
    assert d["armed"] is False and d["session_id"] is None and not d["active"] and d["takeover"] is None and d["blocked"] == ["not_armed"]


@pytest.mark.parametrize("change, reason", [
    ({"session_id": "someone-elses-session"}, "other_session"),
    ({"is_fixture": True}, "fixture"),
    ({"created_at": 1, "updated_at": 1}, "stale"),
    ({"physical_event_id": None}, "not_correlated"),
    ({"severity": "medium"}, "severity_below_high"),
    ({"state": "RESOLVED"}, "incident_not_open"),
])
async def test_incidents_that_must_not_take_over(h, change, reason):
    rt = h.rt
    await rt.security.arm(CODE)
    if "created_at" in change:
        change = {"created_at": now_ms() - tk.TAKEOVER_MAX_AGE_MS - 1000, "updated_at": now_ms() - tk.TAKEOVER_MAX_AGE_MS - 1000}
    rt.security.apply_incident_update(incident(rt, **change))
    d = decide(rt)
    assert not d["active"] and d["takeover"] is None and reason in d["blocked"], d["blocked"]


async def test_another_devices_usb_a_missing_one_and_a_fixture_never_take_over(h):
    rt = h.rt
    await rt.security.arm(CODE)
    other = usb_event("mac-someone-else")
    rt.security.evidence.append(other)
    rt.security.modes.on_usb(other, unknown=True)
    assert rt.security.mode == M.CRITICAL
    d = decide(rt)
    assert not d["active"] and d["takeover"] is None and d["blocked"] == ["other_device"]

    await rt.security.disarm(CODE)
    await rt.security.arm(CODE)
    rt.security.modes.on_usb(usb_event(rt.security.identity.device_id), unknown=True)  # cited, but never in the record
    d = decide(rt)
    assert not d["active"] and d["takeover"] is None and d["blocked"] == ["evidence_missing"]

    await rt.security.disarm(CODE)
    await rt.security.arm(CODE)
    sample = usb_event(rt.security.identity.device_id, fixture=True)
    rt.security.evidence.append(sample)
    rt.security.modes.on_usb(sample, unknown=True)
    d = decide(rt)
    assert not d["active"] and d["takeover"] is None and d["blocked"] == ["fixture"]


async def test_a_rehearsal_fixture_is_shown_as_rehearsal_and_is_never_live(rt, monkeypatch):
    monkeypatch.setattr("app.security.camera.CameraWatch", FakeDemoCamera)
    h = Harness(rt)
    rt.security._loop = asyncio.get_running_loop()
    from app.security.demo import DemoService

    demo = rt.security.demo = DemoService(rt.security)
    try:
        await demo.start("rehearsal")
        await rt.security.arm(CODE)
        demo.rehearsal_usb()
        assert rt.security.mode == M.CRITICAL
        d = decide(rt)
        rec = d["takeover"]
        assert not d["active"] and "fixture" in d["blocked"]
        assert rec["live"] is False and rec["rehearsal"] is True and rec["id"] == f"usb:{demo.run.fixtures[-1]['event_id']}"
        assert "Rehearsal" in rec["headline"] and "not a real device" in rec["sentence"]
        assert rec["response"]["audio"]["state"] == "skipped" and rec["response"]["evidence"]["state"] == "local_only"
        # a real device afterwards replaces the sample, and a sample never replaces a real takeover
        await demo.end()
        await rt.security.disarm(CODE)
        await rt.security.arm(CODE)
        real = await plugged(h)
        await demo.start("rehearsal")
        demo.rehearsal_usb()
        d = decide(rt)
        assert d["active"] and d["takeover"]["id"] == f"usb:{real.event_id}" and d["takeover"]["live"] is True
    finally:
        if demo.run is not None:
            await demo.end()
        await rt.security.close()


async def test_a_stale_usb_fact_stops_being_active_but_the_record_stays_until_disarm(h):
    rt = h.rt
    await rt.security.arm(CODE)
    usb = await plugged(h)
    later = usb.observed_at + tk.TAKEOVER_MAX_AGE_MS + 1000
    tracker = rt.security.takeover
    assert decide(rt, now_ms=later)["takeover"] is None, "a fact first read when already stale never makes a record"
    assert decide(rt)["active"]
    d = decide(rt, now_ms=later)
    assert not d["active"] and "stale" in d["blocked"]
    assert d["takeover"]["id"] == f"usb:{usb.event_id}" and d["takeover"]["fresh"] is False and tracker.record is not None


# ------------------------------------------------------------------ what does
async def test_a_genuine_unknown_usb_takes_over(h):
    rt = h.rt
    await rt.security.arm(CODE)
    usb = await plugged(h)
    d = decide(rt)
    rec = d["takeover"]
    assert d["active"] and d["blocked"] == [] and d["armed"] and d["session_id"] == rt.security.session.session_id
    assert rec["id"] == f"usb:{usb.event_id}" and rec["live"] is True and rec["rehearsal"] is False and rec["trigger"] == "unknown_usb"
    assert rec["local_mode"] == "CRITICAL" and rec["local_mode_verified"] is True and rec["fresh"] is True
    assert rec["detected_at_ms"] == usb.observed_at and rec["incident_id"] == rt.security.incident["incident_id"] and rec["incident_id"].startswith("local-")
    assert rec["sentence_source"] == "usb_only" and rec["acknowledged_at_ms"] is None and rec["shown_at_ms"] is None
    kinds = [e["kind"] for e in rec["evidence"]]
    assert kinds == ["usb", "incident", "scope", "sensors"]
    usb_item = rec["evidence"][0]
    assert usb_item["source"] == "mac_usb_watcher" and usb_item["event_id"] == usb.event_id and "0781:5581" in usb_item["detail"]
    assert "SanDisk" not in json.dumps(rec) and "Ultra" not in json.dumps(rec), "a device chooses its own name; it is never shown"
    assert rec["response"]["evidence"]["state"] == "local_only" and rec["response"]["monitoring"]["state"] == "running"


async def test_example_jetson_person_then_unknown_usb(h):
    rt = h.rt
    await rt.security.arm(CODE)
    sid = rt.security.session.session_id
    person = camera("person_in_zone", remote=True, session_id=sid)
    rt.security.apply_remote_camera_event(person, sid, server_received_ms=now_ms())
    await asyncio.sleep(0.3)
    usb = await plugged(h)
    assert rt.security.modes.state.mode_reason == "unknown_usb_with_person"
    assert await wait_for(lambda: any(w.get("audio") for w in rt.security.warnings if w["event_id"] == usb.event_id))
    d = decide(rt)
    rec = d["takeover"]
    assert d["active"] and rec["sentence_source"] == "local_timing" and "after the Jetson reported a person at this desk" in rec["sentence"]
    person_item = rec["evidence"][0]
    assert person_item["kind"] == "person" and person_item["source"] == "remote_camera" and person_item["event_id"] == person.event_id
    assert person_item["detail"] == "Jetson jetson-01 · 91% confidence" and person_item["at_ms"] <= usb.observed_at
    assert rec["response"]["audio"]["state"] == "played"
    print("EXAMPLE_TAKEOVER " + json.dumps(d))  # pytest -s -k example shows one real decision


async def test_an_authoritative_correlated_incident_takes_over_and_says_whether_this_mac_verified_it(h):
    rt = h.rt
    await rt.security.arm(CODE)
    update = incident(rt, incident_id="inc-c", summary="An unknown USB device connected 3.1 s after a person sat at the desk.")
    rt.security._last_incident_update = update  # SpacetimeDB's row with no local CRITICAL behind it (rule C)
    assert rt.security.mode == M.NORMAL
    d = decide(rt)
    rec = d["takeover"]
    assert d["active"] and rec["id"] == "incident:inc-c" and rec["trigger"] == "correlated_incident" and rec["incident_id"] == "inc-c"
    assert rec["local_mode"] == "NORMAL" and rec["local_mode_verified"] is False
    assert rec["sentence_source"] == "spacetimedb_summary" and rec["sentence"] == update.summary
    assert rec["response"]["evidence"] == {"state": "recorded", "detail": "Recorded in SpacetimeDB (2 evidence ids)"}

    await rt.security.disarm(CODE)
    await rt.security.arm(CODE)
    rt.security.apply_incident_update(incident(rt, incident_id="inc-b"))  # the ModeEngine follows it to CRITICAL (rule B)
    assert rt.security.mode == M.CRITICAL and rt.security.modes.state.mode_reason == "correlated_incident"
    rec = decide(rt)["takeover"]
    assert rec["id"] == "incident:inc-b" and rec["local_mode"] == "CRITICAL" and rec["local_mode_verified"] is True


async def test_one_id_across_the_local_usb_to_authoritative_incident_upgrade(h):
    rt = h.rt
    await rt.security.arm(CODE)
    usb = await plugged(h)
    first = decide(rt)["takeover"]
    acked = rt.security.takeover.report(first["id"], "dismissed", at_ms=now_ms())["acknowledged_at_ms"]
    rt.security.apply_incident_update(incident(rt, incident_id="inc-u", digital_event_id=usb.event_id, physical_event_id="cam-9", evidence_ids=[usb.event_id, "cam-9"]))
    d = decide(rt)
    rec = d["takeover"]
    assert d["active"] and rec["id"] == first["id"] == f"usb:{usb.event_id}"
    assert rec["trigger"] == "correlated_incident" and rec["incident_id"] == "inc-u" and rec["local_mode_verified"] is True
    assert acked is not None and rec["acknowledged_at_ms"] == acked
    assert rec["detected_at_ms"] == usb.observed_at
    inc = next(e for e in rec["evidence"] if e["kind"] == "incident")
    assert inc["source"] == "spacetimedb" and inc["event_id"] == "incident:inc-u" and inc["severity"] == "high" and "HIGH (server)" in inc["detail"]
    assert rec["response"]["evidence"]["state"] == "recorded"


async def test_a_reconnect_redelivering_the_incident_keeps_the_acknowledgement_and_the_id(h):
    rt = h.rt
    await rt.security.arm(CODE)
    update = incident(rt, incident_id="inc-r")
    rt.security.apply_incident_update(update)
    rec = decide(rt)["takeover"]
    acked = rt.security.takeover.report(rec["id"], "dismissed", at_ms=now_ms())["acknowledged_at_ms"]
    rt.security.apply_incident_update(IncidentUpdateV1(**update.model_dump()))  # the subscriber reconnects and re-delivers every row
    again = decide(rt)["takeover"]
    assert again["id"] == rec["id"] == "incident:inc-r" and again["acknowledged_at_ms"] == acked
    assert again["detected_at_ms"] == rec["detected_at_ms"]


async def test_disarm_clears_the_record_and_the_handoff(h):
    rt = h.rt
    await rt.security.arm(CODE)
    await plugged(h)
    rec = decide(rt)["takeover"]
    out = rt.security.takeover.report(rec["id"], "handoff", view="evidence", at_ms=now_ms())
    handoff = decide(rt)["handoff"]
    assert handoff == out["handoff"] and handoff["view"] == "evidence" and handoff["takeover_id"] == rec["id"] and len(handoff["nonce"]) == 16
    assert handoff["incident_id"] == rec["incident_id"]
    await rt.security.disarm(CODE)
    d = decide(rt)
    assert d["takeover"] is None and d["handoff"] is None and d["blocked"] == ["not_armed"]
    await rt.security.arm(CODE)
    d = decide(rt)
    assert d["takeover"] is None and d["handoff"] is None, "a new session starts with nothing from the last one"


async def test_reports_from_the_window_never_change_security_state(h):
    rt = h.rt
    await rt.security.arm(CODE, ResponsePolicy(auto_lock=True, freeze_protected_tools=True))
    await plugged(h)
    assert await wait_for(lambda: all(w.get("audio") for w in rt.security.warnings if w.get("spoken")))
    assert await wait_for(lambda: any(x["status"] == "LOCK_DRY_RUN" for x in rt.security.locks))
    await quiet(rt)
    rec = decide(rt)["takeover"]
    before = security_state(rt)
    tracker = rt.security.takeover
    tracker.report(rec["id"], "shown", at_ms=rec["detected_at_ms"] + 180, latency_ms=180)
    tracker.report(rec["id"], "dismissed", at_ms=now_ms())
    for view in ("explain", "evidence", "stop"):
        tracker.report(rec["id"], "handoff", view=view, at_ms=now_ms())
    with pytest.raises(LookupError):
        tracker.report("usb:not-this-one", "dismissed")
    await asyncio.sleep(0.1)
    assert security_state(rt) == before
    after = decide(rt)["takeover"]
    assert after["shown_at_ms"] == rec["detected_at_ms"] + 180 and after["shown_latency_ms"] == 180 and after["acknowledged_at_ms"] is not None
    assert rt.security.session is not None and rt.security.mode == M.CRITICAL


# ------------------------------------------------------------------ response lines, from real records only
async def test_audio_comes_from_the_warning_record(h):
    rt = h.rt
    await rt.security.arm(CODE)
    usb = await plugged(h)
    assert await wait_for(lambda: any(w.get("audio") for w in rt.security.warnings if w["event_id"] == usb.event_id))
    record = next(w for w in rt.security.warnings if w["event_id"] == usb.event_id)
    audio = lambda: decide(rt)["takeover"]["response"]["audio"]  # noqa: E731
    assert audio()["state"] == "played" and record["played"] is True
    rt._os_volume = {"volume": 40, "muted": True, "checked_at": time.time()}
    assert audio()["state"] == "muted"
    rt._os_volume = None
    record.update(played=False, audio={"outcome": "device error", "source": "text_only", "source_label": "Text only", "detail": "the speaker was unplugged"})
    assert audio() == {"state": "failed", "detail": "No audio played (device error, Text only): the speaker was unplugged"}
    record.update(audio=None)
    assert audio()["state"] == "pending"
    record.update(spoken=False, skipped="the same warning was just spoken")
    assert audio()["state"] == "skipped"
    rt.security.warnings.clear()
    assert audio()["state"] == "unknown"


async def test_a_dry_run_lock_is_a_dry_run(h):
    rt = h.rt
    await rt.security.arm(CODE, ResponsePolicy(auto_lock=True))
    await plugged(h)
    assert await wait_for(lambda: any(x["status"] == "LOCK_DRY_RUN" for x in rt.security.locks))
    lock = decide(rt)["takeover"]["response"]["lock"]
    assert lock == {"state": "dry_run", "detail": "Dry run: the screen was not locked"} and h.ran == []


async def test_no_opt_in_means_not_locked(h):
    rt = h.rt
    await rt.security.arm(CODE)
    await plugged(h)
    lock = decide(rt)["takeover"]["response"]["lock"]
    assert lock["state"] == "not_locked" and "automatic locking is off" in lock["detail"]


async def test_a_confirmed_lock_is_only_confirmed_when_the_os_says_so(rt):
    h = Harness(rt, mode="real")
    try:
        await rt.security.arm(CODE, ResponsePolicy(auto_lock=True))
        await plugged(h)
        assert await wait_for(lambda: any(x["status"] == "LOCK_EXECUTED" for x in rt.security.locks), timeout=3)
        assert await wait_for(lambda: any(x["status"] == "LOCK_CONFIRMED" for x in rt.security.locks), timeout=3)
        lock = decide(rt)["takeover"]["response"]["lock"]
        assert lock == {"state": "confirmed", "detail": "macOS reports the screen as locked"} and h.ran == [1]
    finally:
        await rt.security.close()


async def test_tools_frozen_only_when_the_owner_chose_it(h):
    rt = h.rt
    await rt.security.arm(CODE, ResponsePolicy(freeze_protected_tools=True))
    await plugged(h)
    assert await wait_for(lambda: rt.tool_gate.frozen is not None)
    assert decide(rt)["takeover"]["response"]["tools"]["state"] == "frozen"
    await rt.security.disarm(CODE)
    await rt.security.arm(CODE)
    await asyncio.sleep(0.05)
    h.usb.plug(STICK.__class__(7, 0x0781, 0x5590, serial="OTHER", interface_classes=(8,), location_id=0x140000))
    assert await wait_for(lambda: rt.security.mode == M.CRITICAL)
    tools = decide(rt)["takeover"]["response"]["tools"]
    assert tools["state"] == "not_configured" and rt.tool_gate.frozen is None


async def test_nemotron_line_from_investigations_and_the_probe(h, monkeypatch):
    rt = h.rt
    probed = []
    reachable = {"yes": True}

    def probe(host, port, timeout=tk.PROBE_TIMEOUT_S):
        probed.append((host, port, timeout))
        return reachable["yes"]

    monkeypatch.setattr(tk, "_tcp_probe", probe)
    await rt.security.arm(CODE)
    await plugged(h)
    tracker = rt.security.takeover
    nemotron = lambda: decide(rt)["takeover"]["response"]["nemotron"]  # noqa: E731
    assert nemotron()["state"] == "available" and probed[-1] == ("127.0.0.1", 9, tk.PROBE_TIMEOUT_S)
    n = len(probed)
    nemotron()
    assert len(probed) == n, "the probe is cached"
    tracker._probe, reachable["yes"] = None, False
    assert nemotron()["state"] == "unavailable"
    pending = asyncio.get_running_loop().create_future()
    rt.security._investigating = pending
    assert nemotron()["state"] == "investigating"
    pending.set_result(None)
    rec_incident = decide(rt)["takeover"]["incident_id"]
    rt.security.investigations.append({"incident_id": rec_incident, "status": "completed", "model_id": "nemotron-test", "total_ms": 1234.0})
    assert nemotron() == {"state": "answered", "detail": "Answered by nemotron-test in 1234 ms"}
    rt.security.investigations.append({"incident_id": rec_incident, "status": "incomplete", "model_id": "nemotron-test"})
    assert nemotron()["state"] == "failed"


async def test_nothing_claims_containment_unless_the_lock_is_confirmed(h):
    rt = h.rt
    await rt.security.arm(CODE, ResponsePolicy(auto_lock=True, freeze_protected_tools=True))
    usb = await plugged(h)
    assert await wait_for(lambda: any(x["status"] == "LOCK_DRY_RUN" for x in rt.security.locks))
    rec = decide(rt)["takeover"]
    assert rec["response"]["lock"]["state"] != "confirmed" and claims(rec) == []
    rt.security.apply_incident_update(incident(rt, incident_id="inc-w", digital_event_id=usb.event_id, rule_id="blocked_and_contained",
                                               summary="The device was blocked and the workstation isolated."))
    rec = decide(rt)["takeover"]
    assert rec["trigger"] == "correlated_incident" and claims(rec) == [], claims(rec)
    assert rec["sentence_source"] != "spacetimedb_summary"
    rt.security.locks.append({"at": time.time(), "action_id": "x-1", "status": LockStatus.LOCK_FAILED.value, "dry_run": False,
                              "session_id": rt.security.session.session_id, "detail": "the keystroke was blocked by the OS"})
    rec = decide(rt)["takeover"]
    assert rec["response"]["lock"] == {"state": "failed", "detail": "The lock failed"} and claims(rec) == []


# ------------------------------------------------------------------ the token and the endpoints
def test_the_token_never_lands_in_the_real_home_folder_from_a_test_or_script(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCKI_ALERT_TOKEN_FILE", raising=False)
    assert tk.token_path(tmp_path) == tmp_path / "security" / "alert.token"
    assert tk.token_path(PROJECT_ROOT / "data") == tk.Path.home() / ".locki" / "run" / "alert.token"
    monkeypatch.setenv("LOCKI_ALERT_TOKEN_FILE", str(tmp_path / "x" / "t"))
    assert tk.token_path(PROJECT_ROOT / "data") == tmp_path / "x" / "t"


async def test_the_endpoints_answer_only_the_native_window(settings, tts, monkeypatch, tmp_path):
    token_file = tmp_path / "run" / "alert.token"
    monkeypatch.setenv("LOCKI_ALERT_TOKEN_FILE", str(token_file))
    runtime = LockedInRuntime(settings, runtime_settings=RuntimeSettings(), persist_settings=False, tts_engine=tts, enable_vision=False, enable_voice=False)
    h = Harness(runtime)
    app = create_app(settings=settings, runtime=runtime)
    async with app.router.lifespan_context(app):
        token = token_file.read_text()
        assert len(token) == 64 and int(token, 16) >= 0
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600 and stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
        good = {"X-Locki-Alert-Token": token, "X-Locki-Alert-Pid": str(os.getpid()), "X-Locki-Alert-Version": "1"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8077") as c:
            assert (await c.get("/api/security/takeover")).status_code == 401
            assert (await c.get("/api/security/takeover", headers={"X-Locki-Alert-Token": "0" * 64})).status_code == 401
            assert (await c.get("/api/security/takeover", headers={**good, "origin": "http://127.0.0.1:5174"})).status_code == 403
            assert (await c.get("/api/security/takeover", headers={**good, "origin": "https://evil.example"})).status_code == 403
            r = await c.get("/api/security/takeover", headers=good)
            assert r.status_code == 200 and r.json()["armed"] is False and r.json()["takeover"] is None
            assert r.json()["native"] == {"connected": True, "last_poll_ms": r.json()["native"]["last_poll_ms"], "pid": os.getpid(), "version": "1"}

            report = {"takeover_id": "usb:nothing", "event": "dismissed", "at_ms": now_ms()}
            assert (await c.post("/api/security/takeover/report", json=report)).status_code == 401
            assert (await c.post("/api/security/takeover/report", json=report, headers={**good, "origin": "http://127.0.0.1:5174"})).status_code == 403
            assert (await c.post("/api/security/takeover/report", json=report, headers={**good, "origin": "https://evil.example"})).status_code == 403
            assert (await c.post("/api/security/takeover/report", json=report, headers=good)).status_code == 404

            await runtime.security.arm(CODE)
            await plugged(h)
            d = (await c.get("/api/security/takeover", headers=good)).json()
            assert d["active"] and d["takeover"]["live"]
            tid = d["takeover"]["id"]
            await quiet(runtime)
            before = security_state(runtime)
            assert (await c.post("/api/security/takeover/report", json={"takeover_id": tid, "event": "handoff"}, headers=good)).status_code == 422
            shown = await c.post("/api/security/takeover/report", json={"takeover_id": tid, "event": "shown", "at_ms": now_ms(), "latency_ms": 210, "visible": True}, headers=good)
            assert shown.status_code == 200 and shown.json()["shown_at_ms"]
            ack = await c.post("/api/security/takeover/report", json={"takeover_id": tid, "event": "dismissed", "at_ms": now_ms()}, headers=good)
            assert ack.status_code == 200 and ack.json()["acknowledged_at_ms"]
            hand = await c.post("/api/security/takeover/report", json={"takeover_id": tid, "event": "handoff", "view": "explain", "at_ms": now_ms()}, headers=good)
            assert hand.status_code == 200 and hand.json()["handoff"]["view"] == "explain"
            assert security_state(runtime) == before

            state = await c.get("/api/security")
            public = state.json()["takeover"]
            assert public["takeover"]["id"] == tid and public["takeover"]["acknowledged_at_ms"] and public["handoff"]["view"] == "explain"
            assert token not in state.text and token not in (await c.get("/api/state")).text
        await runtime.security.close()
