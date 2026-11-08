"""The desk camera inside the security service: on only while armed, contextual warnings from what it saw,
never a missing USB warning, and nothing spoken about the camera while disarmed. No real camera."""

from __future__ import annotations

import asyncio

import pytest

from app.security import service as service_module
from app.security.contracts import SecurityKind, SecuritySource
from app.security.publisher import FixtureTransport, SecurityEventPublisher

from .conftest import wait_for
from .test_security_monitor import STICK
from .test_security_service import CODE, WARNING, Harness

PRESENCE = "someone's at your workstation"


class FakeCamera:
    instances: list = []

    enrolled = True  # an owner template is loaded, so presence waits for the owner's face

    def __init__(self, settings, *, on_event, session, **_):
        from types import SimpleNamespace

        self.on_event, self.session = on_event, session
        self.calls: list[str] = []
        self.fail = False
        self.vision = SimpleNamespace(faces=SimpleNamespace(template=object() if FakeCamera.enrolled else None))
        FakeCamera.instances.append(self)

    def start(self):
        self.calls.append("start")
        if self.fail:
            raise RuntimeError("could not open camera 0")

    def stop(self):
        self.calls.append("stop")

    def close(self):
        self.calls.append("close")

    running = property(lambda self: self.calls[-1:] == ["start"])

    def snapshot(self):
        return {"running": self.running}


@pytest.fixture
async def cam(rt, tmp_path, monkeypatch):
    FakeCamera.instances = []
    monkeypatch.setattr("app.security.camera.CameraWatch", FakeCamera)
    rt.settings.airlock_vision = True
    h = Harness(rt)
    rt.security._loop = asyncio.get_running_loop()
    pub = SecurityEventPublisher("cam-test", FixtureTransport(), directory=tmp_path)

    def see(kind, zone=None, confidence=0.9):
        meta = {"camera": "mac_webcam", "inference": "mac_mediapipe", "armed": rt.security.session is not None}
        if zone:
            meta["zone"] = zone
        event = pub.build(SecuritySource.MAC_VISION, SecurityKind(kind), meta, confidence=confidence)
        rt.security.on_camera_event(event)
        return event

    h.see = see
    yield h
    pub.close()
    await rt.security.close()


async def test_arming_turns_the_camera_on_and_says_so_and_disarming_turns_it_off(cam):
    rt = cam.rt
    await rt.security.arm(CODE)
    assert await cam.said("watching the desk")
    assert await wait_for(lambda: FakeCamera.instances and FakeCamera.instances[0].calls == ["start"])
    await rt.security.disarm(CODE)
    assert await wait_for(lambda: FakeCamera.instances[0].calls == ["start", "stop"])


async def test_the_owner_returning_is_welcomed_and_still_armed(cam):
    rt = cam.rt
    await rt.security.arm(CODE)
    cam.see("person_in_zone", "desk")
    cam.see("owner_face_match", confidence=0.88)
    assert await cam.said("Welcome back")
    await asyncio.sleep(0.2)
    rt.security.tick()
    assert not any(PRESENCE in s for s in cam.spoken()) and rt.security.session is not None


async def test_someone_unrecognized_arriving_at_the_desk_is_warned_about(cam):
    await cam.rt.security.arm(CODE)
    cam.see("person_in_zone", "desk")
    cam.see("unrecognized_face", confidence=0.75)
    assert await cam.said(PRESENCE)
    assert cam.rt.security.incident is None  # presence alone is not an incident


async def test_with_no_face_in_time_the_presence_warning_still_comes(cam, monkeypatch):
    monkeypatch.setattr(service_module, "PRESENCE_FACE_WAIT_S", 0.1)
    await cam.rt.security.arm(CODE)
    cam.see("person_in_zone", "desk")
    await asyncio.sleep(0.15)
    cam.rt.security.tick()
    assert await cam.said(PRESENCE)


async def test_typing_by_someone_not_matched_is_an_alert_and_by_the_owner_is_not(cam):
    rt = cam.rt
    await rt.security.arm(CODE)
    cam.see("owner_face_match")
    cam.see("keyboard_interaction", "keyboard_zone")
    await asyncio.sleep(0.1)
    assert not any("interacting" in s for s in cam.spoken())
    cam.see("unrecognized_face")
    event = cam.see("keyboard_interaction", "keyboard_zone")
    assert await cam.said("Someone is interacting with your computer")
    assert rt.security.incident["reason"] == "keyboard_interaction" and event.event_id in rt.security.incident["evidence_ids"]


async def test_a_usb_device_with_an_unrecognized_person_at_the_ports_names_what_was_seen(cam):
    rt = cam.rt
    await rt.security.arm(CODE)
    seen = [cam.see("person_in_zone", "desk"), cam.see("unrecognized_face"), cam.see("usb_zone_interaction", "usb_port_zone")]
    cam.usb.plug(STICK)
    assert await cam.said("while an unrecognized person was at the desk")
    record = next(w for w in rt.security.warnings if w["reason"] == "unknown_usb")
    assert set(record["camera_context"]) == {e.event_id for e in seen}


async def test_the_owner_at_the_desk_never_removes_a_usb_warning(cam):
    await cam.rt.security.arm(CODE)
    cam.see("person_in_zone", "desk")
    cam.see("owner_face_match")
    cam.usb.plug(STICK)
    assert await cam.said(WARNING)
    assert "a new USB device was just connected" in next(s for s in cam.spoken() if WARNING in s)


async def test_someone_whose_face_could_not_be_checked_is_just_someone(cam):
    await cam.rt.security.arm(CODE)
    cam.see("person_in_zone", "desk")
    cam.see("face_uncertain")
    cam.usb.plug(STICK)
    assert await cam.said("while someone was at the desk")
    assert not any("unrecognized person" in s for s in cam.spoken())


async def test_a_blocked_camera_is_said_once_and_usb_still_warns(cam):
    await cam.rt.security.arm(CODE)
    cam.see("camera_obstructed")
    cam.see("camera_obstructed")
    assert await cam.said("can't see the desk")
    assert sum("can't see the desk" in s for s in cam.spoken()) == 1
    cam.usb.plug(STICK)
    assert await cam.said(WARNING)


async def test_while_disarmed_the_camera_is_recorded_not_spoken(cam):
    cam.see("person_in_zone", "desk")
    cam.see("unrecognized_face")
    cam.see("keyboard_interaction", "keyboard_zone")
    await asyncio.sleep(0.1)
    assert not cam.spoken() and len(cam.rt.security.physical) == 3 and not cam.rt.security.evidence


async def test_a_camera_that_fails_to_start_leaves_usb_monitoring_working(cam, monkeypatch):
    orig = FakeCamera.start

    def broken(self):
        self.fail = True
        return orig(self)

    monkeypatch.setattr(FakeCamera, "start", broken)
    await cam.rt.security.arm(CODE)
    assert await cam.said("can't see the desk")
    cam.usb.plug(STICK)
    assert await cam.said(WARNING)


async def test_the_investigator_can_read_a_camera_observation_with_its_limits(cam):
    from app.security.tools import AirlockTools, RunContext

    rt = cam.rt
    await rt.security.arm(CODE)
    event = cam.see("usb_zone_interaction", "usb_port_zone")
    tools = AirlockTools(rt.security, rt.tool_gate)
    out = await tools.call("inspect_camera_observation", {"event_id": event.event_id}, RunContext("r"))
    assert out.status == "ok" and out.data["metadata"]["zone"] == "usb_port_zone" and "not a device being connected" in out.data["limits"]
    cam.usb.plug(STICK)
    assert await wait_for(lambda: any(e.kind == SecurityKind.USB_DEVICE_ADDED for e in rt.security.evidence))
    usb = next(e for e in rt.security.evidence if e.kind == SecurityKind.USB_DEVICE_ADDED)
    refused = await tools.call("inspect_camera_observation", {"event_id": usb.event_id}, RunContext("r"))
    assert refused.status == "error" and "not a camera observation" in refused.error
    health = (await tools.call("get_device_health", {}, RunContext("r"))).data
    assert health["desk_camera"]["status"] == "usb_zone_interaction"


async def test_the_camera_reports_its_own_health_only_while_running_and_publishing(cam):
    from types import SimpleNamespace

    from app.security.publisher import FixtureTransport, SecurityEventPublisher
    from app.security.spacetime_adapter import HealthBeat

    rt = cam.rt
    await rt.security.arm(CODE)
    assert await wait_for(lambda: FakeCamera.instances and FakeCamera.instances[0].calls == ["start"])
    fake = FakeCamera.instances[0]
    transport = FixtureTransport()
    fake.publisher = SecurityEventPublisher("cam-test", transport, directory=rt.settings.data_dir)
    fake.transport = transport
    fake.vision = SimpleNamespace(health=SimpleNamespace(state="obstructed"))
    rt.security.heartbeat()
    fake.publisher.flush()
    sent = [m for m in transport.steps if isinstance(m, HealthBeat)]  # the fixture keeps everything that is not an event here
    assert sent and sent[0].component == "camera" and sent[0].status == "degraded"
    fake.transport = None  # local only: nothing to report to
    rt.security._last_camera_beat = 0.0
    rt.security.heartbeat()
    fake.publisher.flush()
    assert len([m for m in transport.steps if isinstance(m, HealthBeat)]) == 1
    fake.publisher.close()
