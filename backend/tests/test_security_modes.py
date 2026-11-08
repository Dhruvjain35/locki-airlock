"""Security modes from observed events: the acceptance flow A-G, the safety rules (no disarm from a face, no
identity assumed at low confidence, no accusation), and the voice and conversation that follow. No camera."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.security.contracts import IncidentState, IncidentUpdateV1, SecurityKind, SecuritySource
from app.security.modes import CRITICAL_HOLD_S, ModeEngine, SecurityMode
from app.security.publisher import FixtureTransport, SecurityEventPublisher
from app.telemetry.events import EventType

from .conftest import wait_for
from .test_security_camera import FakeCamera
from .test_security_monitor import STICK
from .test_security_service import CODE, Harness

M = SecurityMode


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def events(tmp_path):
    pub = SecurityEventPublisher("cam-test", FixtureTransport(), directory=tmp_path)

    def make(kind, track=None, zone=None, source=SecuritySource.MAC_VISION, **meta):
        if kind.startswith("usb_device"):
            usb = {"fingerprint": "f" * 16, "has_serial": False, "vendor_id": "0x05ac", "product_id": "0x12a8", "category": "storage",
                   "allowlist_status": "unknown", "detection": "iokit_notification", "armed": True}
            return pub.build(source, SecurityKind(kind), usb)
        m = {"camera": "mac_webcam", "inference": "mac_mediapipe", "armed": True, **meta}
        if track is not None:
            m["track_id"] = track
        if zone:
            m["zone"] = zone
        return pub.build(source, SecurityKind(kind), m, confidence=0.9)

    yield make
    pub.close()


def engine():
    clock = Clock()
    seen = []
    e = ModeEngine(on_change=lambda prev, new, reason, ev, state: seen.append((prev, new, reason)), clock=clock)
    return e, clock, seen


def test_the_acceptance_flow_at_the_state_level(events):
    e, clock, seen = engine()
    e.set_armed(True, "s1")
    assert e.state.current_security_mode == M.NORMAL and e.state.armed_status  # A: armed, nobody here
    before = len(seen)
    e.on_camera(events("person_in_zone", zone="desk"))  # B: someone arrives
    # The transition happens inside the same call, with no time passing on the engine's clock: no polling, no queue.
    # (Wall-clock speed is measured on real hardware by scripts/airlock_identity_bench.py, not asserted here.)
    assert e.state.current_security_mode == M.WATCHFUL and len(seen) == before + 1 and clock.t == 1000.0
    assert e.state.face_identity_state == "uncertain" and e.state.identity_detail == "no face compared yet"
    e.on_camera(events("unrecognized_face", track=3))
    assert e.state.current_security_mode == M.WATCHFUL and e.state.person_track_id == 3 and e.state.face_identity_state == "unrecognized"
    clock.t += 2
    e.on_camera(events("keyboard_interaction", zone="keyboard_zone"))  # C: typing
    assert e.state.current_security_mode == M.DEFENSIVE and e.state.keyboard_interaction
    usb = events("usb_device_added", source=SecuritySource.MAC_USB)  # D: a real unknown USB device
    e.on_usb(usb, unknown=True)
    assert e.state.current_security_mode == M.CRITICAL and usb.event_id in e.state.evidence_ids and len(e.state.evidence_ids) >= 3
    clock.t += 20
    e.on_camera(events("zone_clear"))  # the visitor leaves; the critical case stays
    assert e.state.current_security_mode == M.CRITICAL and not e.state.person_present
    e.on_camera(events("person_in_zone", zone="desk"))
    e.on_camera(events("owner_face_match", track=4))  # F: the owner returns
    assert e.state.current_security_mode == M.OWNER_RETURNED and e.state.armed_status  # never disarmed by a face
    assert [n for _p, n, _r in seen] == [M.WATCHFUL, M.DEFENSIVE, M.CRITICAL, M.OWNER_RETURNED]
    e.set_armed(False)
    assert e.state.current_security_mode == M.NORMAL and e.state.camera_health == "off"


def test_low_confidence_is_neither_the_owner_nor_an_intruder(events):
    e, clock, _ = engine()
    e.set_armed(True, "s1")
    e.on_camera(events("person_in_zone"))
    e.on_camera(events("face_uncertain", track=1, detail="the face could not be compared reliably"))
    assert e.state.current_security_mode == M.WATCHFUL and e.state.face_identity_state == "uncertain"
    e.on_camera(events("no_face_visible"))
    assert e.state.current_security_mode == M.WATCHFUL  # still not assumed to be anyone
    e.on_camera(events("laptop_interaction", zone="laptop_zone"))
    assert e.state.current_security_mode == M.DEFENSIVE  # someone not matched to the owner is using the laptop while armed


def test_the_owner_with_someone_else_is_not_owner_returned(events):
    e, _, _ = engine()
    e.set_armed(True, "s1")
    e.on_camera(events("owner_face_match", track=1))
    assert e.state.current_security_mode == M.OWNER_RETURNED
    e.on_camera(events("unrecognized_face", track=2))
    assert e.state.current_security_mode == M.WATCHFUL and e.state.person_track_id == 2


def test_typing_the_owner_did_before_anyone_else_appeared_is_not_defensive(events):
    e, clock, _ = engine()
    e.set_armed(True, "s1")
    e.on_camera(events("owner_face_match", track=1))
    e.on_camera(events("keyboard_interaction", zone="keyboard_zone"))
    clock.t += 1
    e.on_camera(events("unrecognized_face", track=2))
    assert e.state.current_security_mode == M.WATCHFUL
    clock.t += 1
    e.on_camera(events("keyboard_interaction", zone="keyboard_zone"))
    assert e.state.current_security_mode == M.DEFENSIVE


def test_critical_holds_then_follows_what_is_there_and_the_database_can_end_it(events):
    e, clock, _ = engine()
    e.set_armed(True, "s1")
    e.on_usb(events("usb_device_added", source=SecuritySource.MAC_USB), unknown=True)
    assert e.state.current_security_mode == M.CRITICAL and e.state.mode_reason == "unknown_usb"
    clock.t += CRITICAL_HOLD_S + 1
    e.tick()
    assert e.state.current_security_mode == M.NORMAL
    e.on_incident("incident:1", correlated=True, resolved=False, evidence=["d1", "p1"])
    assert e.state.current_security_mode == M.CRITICAL and "incident:1" in e.state.evidence_ids
    e.on_incident("incident:1", correlated=False, resolved=True, evidence=[])
    assert e.state.current_security_mode == M.NORMAL
    e.on_usb(events("usb_device_added", source=SecuritySource.MAC_USB), unknown=False)  # a baseline device is not critical
    assert e.state.current_security_mode == M.NORMAL


def test_disarmed_nothing_changes_the_mode(events):
    e, _, seen = engine()
    for kind in ("person_in_zone", "unrecognized_face", "keyboard_interaction"):
        e.on_camera(events(kind))
    e.on_usb(events("usb_device_added", source=SecuritySource.MAC_USB), unknown=True)
    assert e.state.current_security_mode == M.NORMAL and seen == []


# ---------------------------------------------------------------- through the security service, voice and conversation

@pytest.fixture
async def svc(rt, tmp_path, monkeypatch):
    FakeCamera.instances = []
    monkeypatch.setattr("app.security.camera.CameraWatch", FakeCamera)
    rt.settings.airlock_vision = True
    h = Harness(rt)
    rt.security._loop = asyncio.get_running_loop()
    pub = SecurityEventPublisher("cam-test", FixtureTransport(), directory=tmp_path)
    modes = []
    rt.bus.add_listener(lambda ev: modes.append(ev.payload) if ev.event_type == EventType.SECURITY_MODE else None)

    def see(kind, track=None, zone=None):
        meta = {"camera": "mac_webcam", "inference": "mac_mediapipe", "armed": True}
        if track is not None:
            meta["track_id"] = track
        if zone:
            meta["zone"] = zone
        event = pub.build(SecuritySource.MAC_VISION, SecurityKind(kind), meta, confidence=0.9)
        rt.security.on_camera_event(event)
        return event

    h.see, h.modes = see, modes
    yield h
    pub.close()
    await rt.security.close()


async def say(rt, text):
    return await rt.handle_utterance(text, source="text", stt_confidence=1.0)


async def test_the_acceptance_flow_through_voice_and_conversation(svc):
    rt, sec = svc.rt, svc.rt.security
    await sec.arm(CODE)  # A
    svc.see("person_in_zone", zone="desk")  # B
    svc.see("unrecognized_face", track=7)
    assert await svc.said("someone's at your workstation")
    assert sec.mode == M.WATCHFUL and rt.style().tone != "serious"
    svc.see("keyboard_interaction", zone="keyboard_zone")  # C, E: no one asked
    assert await svc.said("Someone is interacting with your computer")
    assert sec.mode == M.DEFENSIVE and rt.style().tone == "serious"
    svc.usb.plug(STICK)  # D
    assert await svc.said("while an unrecognized person was at the desk")
    assert await wait_for(lambda: sec.mode == M.CRITICAL)
    r = await say(rt, "did somebody touch my laptop")
    assert "typed on the keyboard" in r.reply and "not recognized as you" in r.reply and "proof" in r.reply
    svc.see("zone_clear")
    svc.see("person_in_zone", zone="desk")
    svc.see("owner_face_match", track=8)  # F
    assert await svc.said("Welcome back")
    assert sec.mode == M.OWNER_RETURNED and sec.session is not None and rt.style().tone != "serious"
    r = await say(rt, "who was near my workstation")  # G
    assert "visits to the desk" in r.reply and "who was not recognized as you" in r.reply and "who matched you" in r.reply
    r = await say(rt, "is someone at my desk")
    assert r.reply.startswith("Yes.") and "matches you" in r.reply
    spoken = " ".join(svc.spoken()).lower()
    assert "hacker" not in spoken and "intruder" not in spoken and "criminal" not in spoken
    assert [m["to"] for m in svc.modes] == ["WATCHFUL", "DEFENSIVE", "CRITICAL", "OWNER_RETURNED"]
    assert all({"armed_status", "face_identity_state", "current_security_mode", "camera_health"} <= set(m) for m in svc.modes)


async def test_the_owner_returning_before_the_face_wait_hears_no_warning(svc):
    sec = svc.rt.security
    await sec.arm(CODE)
    svc.see("person_in_zone", zone="desk")
    assert sec.mode == M.WATCHFUL  # attentive at once, but quiet for the face wait
    svc.see("owner_face_match", track=1)
    assert await svc.said("Welcome back")
    await asyncio.sleep(0.05)
    sec.tick()
    assert not any("someone's at your workstation" in s for s in svc.spoken())


async def test_the_model_gets_the_observed_summary_and_firm_instructions_when_defensive(svc):
    sec = svc.rt.security
    await sec.arm(CODE)
    svc.see("person_in_zone", zone="desk")
    svc.see("unrecognized_face", track=2)
    svc.see("keyboard_interaction", zone="keyboard_zone")
    block = sec.context_block()
    assert "mode DEFENSIVE" in block and "not recognized as the owner" in block and "keyboard" in block
    assert "never guess who someone is" in block and "one short, firm sentence" in block
    rendered = svc.rt.companion.render(allowed=[], user_text="who are you")
    assert "SECURITY (observed" in rendered


async def test_correlated_incident_from_the_database_is_critical_and_spoken(svc):
    sec = svc.rt.security
    await sec.arm(CODE)
    update = IncidentUpdateV1(incident_id="incident:x", session_id=sec.session.session_id, severity="high", state=IncidentState.SUSPECTED_INTRUSION,
                              evidence_ids=["d1", "p1"], updated_at=int(time.time() * 1000), digital_event_id="d1", physical_event_id="p1",
                              rule_id="correlated_usb_physical", summary="Unknown USB device detected within 10 seconds of a person in the desk zone.")
    sec.apply_incident_update(update)
    assert sec.mode == M.CRITICAL
    assert await svc.said("the desk camera saw someone here")


# ---------------------------------------------------------------- regressions found in review

class Advancing(Clock):
    """A real clock moves between two reads of the same event."""

    def __call__(self):
        self.t += 1e-6
        return self.t


def test_a_hand_on_the_keys_as_the_very_first_observation_is_defensive(events):
    clock = Advancing()
    e = ModeEngine(clock=clock)
    e.set_armed(True, "s1")
    e.on_camera(events("keyboard_interaction", zone="keyboard_zone"))
    assert e.state.current_security_mode == M.DEFENSIVE


def test_an_owner_already_matched_does_not_mask_a_usb_device_but_returning_after_it_does(events):
    e, clock, _ = engine()
    e.set_armed(True, "s1")
    e.on_camera(events("person_in_zone"))
    e.on_camera(events("owner_face_match", track=1))
    assert e.state.current_security_mode == M.OWNER_RETURNED
    clock.t += 1
    e.on_usb(events("usb_device_added", source=SecuritySource.MAC_USB), unknown=True)
    assert e.state.current_security_mode == M.CRITICAL  # a face is never enough to hide a real unknown device
    clock.t += 5
    e.on_camera(events("person_left_zone"))
    e.on_camera(events("person_in_zone"))
    e.on_camera(events("owner_face_match", track=2))
    assert e.state.current_security_mode == M.OWNER_RETURNED


def test_the_owner_looking_away_is_still_the_owner(events):
    e, clock, seen = engine()
    e.set_armed(True, "s1")
    e.on_camera(events("person_in_zone"))
    e.on_camera(events("owner_face_match", track=1))
    e.on_camera(events("face_uncertain", track=1))
    e.on_camera(events("no_face_visible"))
    assert e.state.current_security_mode == M.OWNER_RETURNED and e.state.face_identity_state in ("owner", "uncertain")
    assert [n for _p, n, _r in seen] == [M.WATCHFUL, M.OWNER_RETURNED]  # watchful only until the face matched, then nothing changes


def test_someone_sitting_down_after_the_owner_left_is_not_the_owner(events):
    e, clock, _ = engine()
    e.set_armed(True, "s1")
    e.on_camera(events("person_in_zone"))
    e.on_camera(events("owner_face_match", track=1))
    e.on_camera(events("person_left_zone"))  # the owner leaves the desk; still in view, no zone_clear
    e.on_camera(events("person_in_zone"))  # someone else sits down before any face is compared
    assert e.state.current_security_mode == M.WATCHFUL and e.state.face_identity_state == "uncertain"
    e.on_camera(events("keyboard_interaction", zone="keyboard_zone"))
    assert e.state.current_security_mode == M.DEFENSIVE


def test_a_visitor_who_keeps_typing_stays_defensive_while_at_the_desk(events):
    e, clock, _ = engine()
    e.set_armed(True, "s1")
    e.on_camera(events("person_in_zone"))
    e.on_camera(events("unrecognized_face", track=1))
    e.on_camera(events("keyboard_interaction", zone="keyboard_zone"))
    clock.t += 60
    e.tick()
    assert e.state.current_security_mode == M.DEFENSIVE
    e.on_camera(events("person_left_zone"))
    assert e.state.current_security_mode == M.WATCHFUL  # still in view
    e.on_camera(events("zone_clear"))
    assert e.state.current_security_mode == M.NORMAL


async def test_someone_only_in_view_is_watched_silently_until_they_reach_the_desk(svc, monkeypatch):
    from app.security import service as service_module

    monkeypatch.setattr(service_module, "PRESENCE_FACE_WAIT_S", 0.05)
    sec = svc.rt.security
    await sec.arm(CODE)
    svc.see("person_in_view")
    assert sec.mode == M.WATCHFUL
    await asyncio.sleep(0.1)
    sec.tick()
    assert not any("someone's at your workstation" in s for s in svc.spoken())
    svc.see("person_in_zone", zone="desk")
    await asyncio.sleep(0.1)
    sec.tick()
    assert await svc.said("someone's at your workstation")
    assert sum("someone's at your workstation" in s for s in svc.spoken()) == 1


async def test_typing_while_critical_adds_evidence_without_repeating_the_alert_at_once(svc):
    sec = svc.rt.security
    await sec.arm(CODE)
    svc.see("person_in_zone", zone="desk")
    svc.see("unrecognized_face", track=1)
    svc.see("keyboard_interaction", zone="keyboard_zone")
    assert await svc.said("Someone is interacting with your computer")
    svc.usb.plug(STICK)
    assert await wait_for(lambda: sec.mode == M.CRITICAL)
    later = svc.see("laptop_interaction", zone="laptop_zone")
    assert later.event_id in sec.incident["evidence_ids"]
    assert sum("interacting with your computer" in s for s in svc.spoken()) == 1


async def test_desk_answers_say_when_the_camera_cannot_see(svc):
    sec = svc.rt.security
    await sec.arm(CODE)
    svc.see("person_in_zone", zone="desk")
    svc.see("zone_clear")
    FakeCamera.instances[0].calls.append("stop")  # the camera stopped
    r = sec.describe_desk("now")
    assert "can't see the desk" in r and "off" in r and "No one" not in r


async def test_a_visitor_is_still_named_after_the_owner_matches_on_the_same_visit(svc):
    sec = svc.rt.security
    await sec.arm(CODE)
    svc.see("person_in_zone", zone="desk")
    svc.see("unrecognized_face", track=1)
    svc.see("keyboard_interaction", zone="keyboard_zone")
    svc.see("owner_face_match", track=2)
    touched = sec.describe_desk("touched")
    assert "who was not recognized as you typed on the keyboard" in touched
    who = sec.describe_desk("who")
    assert "not recognized as you" in who and "matched you" in who


async def test_every_security_block_forbids_accusations_and_unrecorded_claims(svc):
    sec = svc.rt.security
    await sec.arm(CODE)
    svc.see("person_in_zone", zone="desk")
    block = sec.context_block()
    assert sec.mode == M.WATCHFUL and "never call anyone a hacker, intruder or criminal" in block and "notified" in block


async def test_with_nobody_enrolled_the_presence_warning_does_not_wait(svc, monkeypatch):
    sec = svc.rt.security
    await sec.arm(CODE)
    FakeCamera.instances[0].vision.faces.template = None
    import time as _time

    t0 = _time.monotonic()
    svc.see("person_in_zone", zone="desk")
    assert await svc.said("someone's at your workstation", within=1.0)
    assert _time.monotonic() - t0 < 0.5  # no face wait: no owner could be about to match


def test_desk_questions_go_to_the_model_when_it_is_up(rt):
    from types import SimpleNamespace

    from app.voice.intents import IntentName

    c = rt.companion
    intent = SimpleNamespace(name=IntentName.SECURITY_DESK, stop_word=False)
    if c.available:
        assert c.route(intent, "is someone at my desk", None, "text") == "converse"
    else:
        assert c.route(intent, "is someone at my desk", None, "text") == "reflex"  # no model: the answer from the record


async def test_one_presence_warning_while_someone_stays_in_view(svc, monkeypatch):
    from app.security import service as service_module

    monkeypatch.setattr(service_module, "WARNING_REPEAT_S", 0.0)  # only the stay-in-view rule is being tested
    sec = svc.rt.security
    await sec.arm(CODE)
    FakeCamera.instances[0].vision.faces.template = None  # nobody enrolled: identity can only be uncertain
    svc.see("person_in_zone", zone="desk")
    assert await svc.said("someone's at your workstation", within=1.0)
    for _ in range(3):  # they shift around: the detector loses and finds them at the desk, but they never leave the view
        svc.see("person_left_zone")
        svc.see("person_in_zone", zone="desk")
    await asyncio.sleep(0.1)
    sec.tick()

    def presence_lines():  # counted from the warnings record: repeats are worded differently by the voice layer
        return sum(1 for w in sec.warnings if w["reason"] == "person_at_desk" and w.get("spoken"))

    assert presence_lines() == 1
    assert sec.modes.state.face_identity_state == "uncertain"  # never "unrecognized" without an enrolled owner
    svc.see("zone_clear")  # everyone left
    svc.see("person_in_zone", zone="desk")
    assert await wait_for(lambda: presence_lines() == 2)


def test_camera_events_name_the_real_capture_path():
    from app.security.contracts import SecurityEventV1
    from app.security.spacetime_adapter import SpacetimeConfig, reducer_call

    ev = SecurityEventV1(event_id="e", device_id="cam-1", source=SecuritySource.MAC_VISION, kind=SecurityKind.PERSON_IN_ZONE, observed_at=1794100000000, seq=1,
                         confidence=0.9, metadata={"camera": "mac_webcam", "inference": "mac_mediapipe", "zone": "desk", "armed": True})
    meta = __import__("json").loads(reducer_call(ev, SpacetimeConfig(url="u", database="d", workstation_id="w", device_id="cam-1"))[1][8])
    assert meta == {"label": "desk", "captureMode": "mac_webcam:mac_mediapipe"}
