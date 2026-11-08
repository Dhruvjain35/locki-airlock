"""Demo mode: a fresh owner every time, in its own folder; reset and end delete only that folder; rehearsal
fixtures are labelled and refused in a live demo; nothing pretends someone is recognized. No real camera."""

from __future__ import annotations

import asyncio
import json
import stat
from types import SimpleNamespace

import numpy as np
import pytest

from app.security.contracts import SecurityKind, SecuritySource
from app.security.modes import SecurityMode
from app.security.publisher import FixtureTransport, SecurityEventPublisher
from app.security.service import SecurityError
from app.vision.airlock.enrollment import OwnerTemplate, save_template, template_path
from app.vision.airlock.faces import Thresholds

from .conftest import wait_for
from .test_security_service import CODE, Harness


class FakeVision:
    def __init__(self):
        self.enroller = None
        self.enroll_progress = None
        self.on_enrolled = None
        self.faces = None
        self.health = SimpleNamespace(state="live")
        self.stats = SimpleNamespace(fps=30.0)
        self.live_since = 10.0
        self.first_person_mono = 10.4
        self.stream_clients = 0

    def snapshot(self):
        return {"stats": {"detect_ms_p50": 11.0, "detect_ms_p95": 12.0, "capture_to_event_ms_p50": 10.0, "capture_to_event_ms_p95": 12.0}}

    def latest_jpeg(self):
        return None


class FakeDemoCamera:
    instances: list = []

    def __init__(self, settings, *, on_event, session, **_):
        self.on_event, self.session = on_event, session
        self.calls: list = []
        self.templates: list = []
        self.vision = FakeVision()
        self.transport = None
        FakeDemoCamera.instances.append(self)

    running = property(lambda self: "start" in self.calls and [c for c in self.calls if c in ("start", "stop")][-1] == "start")

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def close(self):
        self.calls.append("close")

    def use_template(self, template, source):
        self.templates.append((source, None if template is None else int(template.samples.shape[0])))

    def start_enrollment(self, enroller, on_enrolled):
        self.vision.enroller, self.vision.on_enrolled = enroller, on_enrolled
        self.vision.enroll_progress = enroller.progress(None)

    def stop_enrollment(self):
        self.vision.enroller = None

    def reobserve(self):
        self.calls.append("reobserve")

    def snapshot(self):
        return {"running": self.running}


@pytest.fixture
async def demo(rt, tmp_path, monkeypatch):
    FakeDemoCamera.instances = []
    monkeypatch.setattr("app.security.camera.CameraWatch", FakeDemoCamera)
    h = Harness(rt)
    rt.security._loop = asyncio.get_running_loop()
    from app.security.demo import DemoService

    rt.security.demo = DemoService(rt.security)
    h.demo = rt.security.demo
    yield h
    if h.demo.run is not None:
        await h.demo.end()
    await rt.security.close()


def template(n=12, seed=1):
    v = np.random.default_rng(seed).normal(size=(n, 128)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return OwnerTemplate(v, {"center": 6, "left": 3, "right": 3}, Thresholds(), 1.0, 1.0)


class DoneEnroller:
    def __init__(self, t):
        self.t = t

    def template(self):
        return self.t

    def progress(self, _):
        return {"accepted": 12, "needed": 12, "complete": True}


async def test_a_demo_starts_with_nobody_enrolled_in_its_own_private_folder(demo):
    snap = await demo.demo.start("live")
    run = demo.demo.run
    assert snap["active"] and snap["mode"] == "live" and "LIVE" in snap["label"]
    assert snap["enrolled"] is False and snap["recognized"] is False and "nobody is enrolled" in snap["template"]
    assert stat.S_IMODE(run.dir.stat().st_mode) == 0o700 and run.dir.parent == demo.demo.root
    cam = FakeDemoCamera.instances[0]
    assert cam.templates[0] == ("demo", None) and cam.running  # the camera compares against nobody, not an older owner
    assert snap["steps"]["start"] == "done" and snap["steps"]["enroll"] == "pending"
    assert await demo.said("nobody is enrolled yet")


async def test_an_older_owner_template_is_never_reused_and_survives_the_demo(demo):
    rt = demo.rt
    prod = template_path(rt.settings.data_dir)
    save_template(prod, template(seed=9))
    before = prod.read_bytes()
    await demo.demo.start("live")
    demo.demo._enrolled(DoneEnroller(template(seed=2)))
    await demo.demo.end()
    assert prod.read_bytes() == before  # the production owner is untouched
    cam = FakeDemoCamera.instances[0]
    assert [src for src, _ in cam.templates] == ["demo", "demo", "production"]


async def test_enrollment_goes_to_the_demo_folder_only_and_recognition_is_timed(demo):
    rt = demo.rt
    await demo.demo.start("live")
    run = demo.demo.run
    if (rt.settings.models_dir / "face_recognition_sface_2021dec.onnx").exists():
        snap = await demo.demo.enroll()
        assert snap["enrolling"] and snap["enrollment"]["needed"] == 12
    demo.demo.run.enroll_started = demo.demo.run.started_mono
    demo.demo._enrolled(DoneEnroller(template()))
    saved = run.dir / "owner_face.json"
    assert saved.exists() and stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert set(json.loads(saved.read_text())) >= {"samples", "model"} and "image" not in saved.read_text()
    assert not template_path(rt.settings.data_dir).exists()  # nothing written as the production owner
    snap = demo.demo.snapshot()
    assert snap["enrolled"] and snap["enrolled_samples"] == 12 and snap["timings"]["enrollment_ms"]["n"] == 1
    assert snap["recognized"] is False  # not until the camera actually matches
    pub = SecurityEventPublisher("cam-test", FixtureTransport(), directory=rt.settings.data_dir)
    match = pub.build(SecuritySource.MAC_VISION, SecurityKind.OWNER_FACE_MATCH, {"camera": "mac_webcam", "inference": "mac_mediapipe", "armed": False, "track_id": 1}, confidence=1.0)
    rt.security.on_camera_event(match)
    snap = demo.demo.snapshot()
    assert snap["recognized"] and snap["steps"]["recognized"] == "done" and snap["timings"]["owner_recognition_after_enrollment_ms"]["n"] == 1
    assert await demo.said("matches this demo's enrollment")
    pub.close()


async def test_end_and_reset_remove_only_the_demo_folder(demo):
    rt = demo.rt
    keep = rt.settings.data_dir / "airlock" / "zones.json"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text('{"zones": {}}')
    await demo.demo.start("rehearsal")
    first = demo.demo.run.dir
    demo.demo._enrolled(DoneEnroller(template()))
    snap = await demo.demo.reset()
    assert not first.exists() and snap["active"] and snap["mode"] == "rehearsal" and snap["enrolled"] is False
    second = demo.demo.run.dir
    out = await demo.demo.end()
    assert not second.exists() and out["ended"] and keep.exists()
    timings = list((rt.settings.data_dir / "airlock_runs").glob("demo-*.json"))
    assert len(timings) == 2  # the measurements are kept; the face template is not
    def no_embedding(node):  # no 128-number vector, and no "samples" field, anywhere in what is kept
        if isinstance(node, dict):
            return "samples" not in node and all(no_embedding(v) for v in node.values())
        if isinstance(node, list):
            return not (len(node) >= 100 and all(isinstance(x, float) for x in node)) and all(no_embedding(v) for v in node)
        return True

    assert all(no_embedding(json.loads(t.read_text())) for t in timings)


async def test_only_a_folder_inside_the_demo_root_can_be_removed(demo, tmp_path):
    victim = tmp_path / "not-a-demo"
    victim.mkdir()
    (victim / "x").write_text("keep me")
    assert demo.demo._remove_demo_folder(victim) == [] and (victim / "x").exists()
    assert demo.demo._remove_demo_folder(demo.demo.root) == []


async def test_rehearsal_fixtures_are_labelled_never_spoken_and_refused_live(demo):
    rt = demo.rt
    await demo.demo.start("live")
    with pytest.raises(SecurityError):
        demo.demo.rehearsal_usb()
    await demo.demo.end()
    await demo.demo.start("rehearsal")
    await rt.security.arm(CODE)
    snap = demo.demo.rehearsal_usb()
    assert snap["fixtures"] and any("FIXTURE" in e["text"] for e in snap["timeline"])
    assert rt.security.mode == SecurityMode.CRITICAL
    await asyncio.sleep(0.1)
    assert not any("Security alert" in s for s in demo.spoken())  # a fixture is never spoken as if it were real


async def test_one_demo_at_a_time_and_never_beside_the_focus_camera(demo):
    rt = demo.rt
    await demo.demo.start("live")
    with pytest.raises(SecurityError):
        await demo.demo.start("live")
    await demo.demo.end()
    rt.vision = SimpleNamespace(camera=SimpleNamespace(source=rt.settings.airlock_vision_source), _running=True)
    with pytest.raises(SecurityError, match="focus camera"):
        await demo.demo.start("live")
    rt.vision = None


async def test_readiness_reports_what_each_part_really_says(demo):
    await demo.demo.start("live")
    r = demo.demo.snapshot()["readiness"]
    assert r["camera"]["ok"] and "live" in r["camera"]["detail"]
    assert r["spacetimedb"]["ok"] is False  # not configured in tests
    assert r["camera_to_database"]["ok"] is False and "local only" in r["camera_to_database"]["detail"]
    assert r["screen_lock"]["detail"].startswith("dry_run")
    assert set(r) >= {"camera", "models", "usb_monitor", "spacetimedb", "speaker", "microphone", "investigator", "zones"}


async def test_arming_and_disarming_during_a_demo_keep_the_preview_running(demo):
    rt = demo.rt
    rt.settings.airlock_vision = True
    await demo.demo.start("live")
    cam = FakeDemoCamera.instances[0]
    await rt.security.arm(CODE)
    await rt.security.disarm(CODE)
    await asyncio.sleep(0.05)
    assert "reobserve" in cam.calls  # armed while already watching: presence is settled again from new frames
    assert cam.running  # the demo still wants the camera
    steps = demo.demo.snapshot()["steps"]
    assert steps["arm"] == "done" and steps["disarm"] == "done"
    await demo.demo.end()
    assert await wait_for(lambda: cam.calls[-1] == "stop")


def test_a_device_is_used_by_one_process_at_a_time(tmp_path):
    import subprocess
    import sys

    from app.security.device_lock import DeviceBusy, camera_lock

    lock = camera_lock(tmp_path)
    lock.acquire()
    probe = ("import sys; sys.path.insert(0, sys.argv[2]); from pathlib import Path; from app.security.device_lock import camera_lock, DeviceBusy\n"
             "try:\n    camera_lock(Path(sys.argv[1])).acquire(); print('acquired')\nexcept DeviceBusy as e:\n    print('busy', e)")
    backend = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    out = subprocess.run([sys.executable, "-c", probe, str(tmp_path), backend], capture_output=True, text=True).stdout
    assert out.startswith("busy") and "AIRLOCK camera" in out and str(__import__("os").getpid()) in out
    lock.release()
    out = subprocess.run([sys.executable, "-c", probe, str(tmp_path), backend], capture_output=True, text=True).stdout
    assert out.strip() == "acquired"
    with pytest.raises(DeviceBusy):
        other = camera_lock(tmp_path)
        lock.acquire()
        other.acquire()
    lock.release()


async def test_recognition_trials_record_outcomes_and_timings_only(demo):
    import time as _time

    from app.telemetry.events import EventSource, EventType

    rt = demo.rt
    await demo.demo.start("live")
    with pytest.raises(SecurityError):
        demo.demo.trial_start("genuine", "v1")  # nothing to compare against yet
    demo.demo._enrolled(DoneEnroller(template()))

    def observe(kind, started_at, dwell_ms, observed_at, confidence=1.0):
        meta = {"camera": "mac_webcam", "inference": "mac_mediapipe", "armed": False, "started_at": started_at, "dwell_ms": dwell_ms, "capture_to_event_ms": 12.0}
        rt.bus.publish(EventType.SECURITY_OBSERVED, EventSource.SECURITY, {"kind": kind, "source": "mac_vision", "metadata": meta, "confidence": confidence, "observed_at": observed_at})

    snap = demo.demo.trial_start("genuine", "v1 <script>")
    t = snap["trials"][-1]
    assert t["participant"] == "v1script" and t["state"] == "running"
    now = int(_time.time() * 1000)
    observe("owner_face_match", now - 5000, 900, now - 4000)  # a face that was there before the trial: ignored
    observe("person_in_zone", now + 100, 300, now + 400)
    observe("owner_face_match", now + 300, 850, now + 1150)
    t = demo.demo.snapshot()["trials"][-1]
    assert t["state"] == "done" and t["outcome"] == "owner" and t["correct"] and t["identity_ms"] == pytest.approx(862)
    assert t["arrival_to_identity_ms"] == pytest.approx(1062)
    demo.demo.trial_start("impostor", "v2")
    observe("face_uncertain", now + 2000, 1500, now + 3500)
    observe("unrecognized_face", now + 2000, 600, now + 2600, confidence=0.83)
    demo.demo.trial_start("impostor", "v3")
    demo.demo.run.trials[-1]["started_mono"] -= 10  # nobody came: the trial times out
    s = demo.demo.snapshot()["trial_summary"]
    assert s["trials"] == 3 and s["genuine"]["matched"] == 1 and s["impostor"]["unrecognized"] == 1 and s["impostor"]["missed"] == 1
    assert s["impostor"]["false_owner_match"] == 0 and s["identity_ms"]["owner"]["n"] == 1
    out = await demo.demo.end()
    saved = json.loads(next((rt.settings.data_dir / "airlock_runs").glob(f"demo-{out['ended']}.json")).read_text())
    assert len(saved["trials"]) == 3 and saved["trial_summary"]["trials"] == 3
