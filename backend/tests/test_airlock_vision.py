"""AIRLOCK camera pipeline: presence and camera health over scripted detections, the limiter, and events
that the security contract accepts. No camera needed; the one model test skips without the model."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app.security.contracts import SecurityEventV1
from app.security.publisher import FixtureTransport, SecurityEventPublisher
from app.vision.airlock.camera_health import CameraHealth
from app.vision.airlock.events import SecurityEventSink
from app.vision.airlock.geometry import Box, ZoneMap, box_fraction_in, clean_polygon
from app.vision.airlock.observations import FrameStats, PersonDet
from app.vision.airlock.pipeline import AirlockVision, EventLimiter
from app.vision.airlock.presence import AT_DESK, EMPTY, PresenceTracker

MODELS = Path(__file__).resolve().parents[1] / "models"
FPS = 10
SEATED = PersonDet(Box(0.25, 0.15, 0.5, 0.85), 0.9)  # tall box in the middle: someone in the chair
FAR = PersonDet(Box(0.70, 0.30, 0.10, 0.30), 0.8)  # short box: someone across the room


def run(tracker, frames, t0=0.0):
    """frames: list of detection lists, one per 1/FPS s. Returns [(t, kind, obs)]."""
    out = []
    for i, persons in enumerate(frames):
        t = t0 + i / FPS
        out += [(round(t, 2), o.kind, o) for o in tracker.update(t, persons)]
    return out


def kinds(events):
    return [k for _, k, _ in events]


def test_walking_past_far_away_is_in_view_never_at_the_desk():
    tr = PresenceTracker(ZoneMap())
    ev = run(tr, [[FAR]] * 15 + [[]] * 35)
    assert kinds(ev) == ["person_in_view", "zone_clear"]
    assert tr.state == EMPTY


def test_sitting_down_is_settled_over_frames_with_a_real_dwell_and_score():
    tr = PresenceTracker(ZoneMap())
    ev = run(tr, [[]] * 3 + [[SEATED]] * 10)
    assert kinds(ev) == ["person_in_zone"]
    t, _, obs = ev[0]
    assert obs.zone == "desk" and obs.confidence == pytest.approx(0.9) and obs.dwell_ms >= 250 and obs.started < t
    assert tr.state == AT_DESK


def test_one_frame_false_positive_is_not_a_person():
    tr = PresenceTracker(ZoneMap())
    assert kinds(run(tr, [[], [SEATED], [], [], [], [], []])) == []


def test_a_detector_dropping_frames_does_not_make_anyone_leave():
    tr = PresenceTracker(ZoneMap())
    flicker = [[SEATED], [SEATED], []] * 20  # one frame in three missed, for 6 s
    assert kinds(run(tr, flicker)) == ["person_in_zone"]


def test_leaving_the_desk_then_the_room():
    tr = PresenceTracker(ZoneMap())
    ev = run(tr, [[SEATED]] * 20 + [[]] * 40)
    assert kinds(ev) == ["person_in_zone", "person_left_zone", "zone_clear"]
    left = ev[1][2]
    assert left.dwell_ms == pytest.approx(1900, abs=150) and left.confidence is None
    assert ev[1][0] - 1.9 == pytest.approx(1.5, abs=0.15)  # leave_s after the last sighting


def test_approaching_is_reported_once_as_the_box_grows_toward_the_desk():
    tr = PresenceTracker(ZoneMap())
    frames = [[PersonDet(Box(0.75 - 0.01 * i, 0.3, 0.08 + 0.01 * i, 0.25 + 0.01 * i), 0.8)] for i in range(15)]
    ev = run(tr, frames)
    assert kinds(ev) == ["person_in_view", "person_approaching"]


def test_low_scores_are_ignored():
    tr = PresenceTracker(ZoneMap())
    assert kinds(run(tr, [[PersonDet(SEATED.box, 0.2)]] * 20)) == []


def test_zones_are_checked_not_repaired():
    with pytest.raises(ValueError):
        clean_polygon([(0, 0), (1, 0)])
    with pytest.raises(ValueError):
        clean_polygon([(0, 0), (1.5, 0), (1, 1)])
    with pytest.raises(ValueError):
        ZoneMap.from_dict({"zones": {"bathroom": [(0, 0), (1, 0), (1, 1)]}})
    assert box_fraction_in(Box(0, 0, 0.5, 0.5), [(0, 0), (1, 0), (1, 1), (0, 1)]) == 1.0
    assert box_fraction_in(Box(0, 0, 0.5, 0.5), [(0.5, 0.5), (1, 0.5), (1, 1), (0.5, 1)]) == 0.0


# ---------------------------------------------------------------- camera health

DARK, FLAT, SCENE = FrameStats(3.0, 1.0), FrameStats(120.0, 2.0), FrameStats(110.0, 40.0)


def feed(health, stats, t0, seconds):
    out = []
    for i in range(int(seconds * FPS)):
        out += [o.kind for o in health.on_frame(t0 + i / FPS, stats)]
    return out


def test_a_covered_lens_is_obstructed_then_restored_and_a_dark_moment_is_not():
    h = CameraHealth()
    h.start(0.0)
    assert feed(h, SCENE, 0.0, 1.0) == [] and h.can_see
    assert feed(h, DARK, 1.0, 0.5) == [] and h.can_see  # shorter than obstruct_s
    assert feed(h, DARK, 1.5, 2.0) == ["camera_obstructed"] and not h.can_see
    assert feed(h, SCENE, 3.5, 2.0) == ["camera_restored"] and h.can_see
    assert feed(h, FLAT, 5.5, 2.0) == ["camera_obstructed"]


def test_a_camera_that_stops_is_offline_and_one_that_never_opens_is_unavailable():
    h = CameraHealth()
    h.start(0.0)
    feed(h, SCENE, 0.0, 0.5)
    assert [o.kind for o in h.on_source(1.0, "reconnecting", "camera 0 stopped delivering frames")] == ["camera_offline"]
    assert feed(h, SCENE, 3.0, 1.5) == ["camera_restored"]
    never = CameraHealth()
    never.start(0.0)
    assert never.on_source(1.0, "opening", "could not open camera 0") == []
    assert [o.kind for o in never.tick(7.0)] == ["camera_unavailable"]


# ---------------------------------------------------------------- limiter

def test_soft_observations_are_rate_limited_and_counted():
    t = [0.0]
    lim = EventLimiter(clock=lambda: t[0])
    assert lim.allow("person_in_view")
    t[0] = 5.0
    assert not lim.allow("person_in_view")
    t[0] = 10.5
    assert lim.allow("person_in_view")
    assert lim.suppressed["person_in_view"] == 1
    burst = [lim.allow("person_in_zone") for _ in range(20)]
    assert sum(burst) <= lim.burst


# ---------------------------------------------------------------- the whole pipeline, no camera

class ScriptedCamera:
    """Hands frames to the pipeline like CameraSource: newest only, ids increasing."""

    def __init__(self, frames):
        self.frames = frames
        self.on_state_change = lambda s, r: None
        self._i = 0

    def start(self):
        self.on_state_change("live", "scripted")

    def stop(self):
        pass

    def read_latest(self, after_id, timeout=0.5):
        if self._i >= len(self.frames):
            time.sleep(0.01)
            return None
        self._i += 1
        time.sleep(1 / FPS)
        return self._i, self.frames[self._i - 1], time.monotonic()


class ScriptedDetector:
    name, inference = "scripted", "mac_mediapipe"

    def __init__(self, plan):
        self.plan = plan
        self.calls = 0

    def detect(self, rgb, ts_ms):
        self.calls += 1
        return self.plan(self.calls)

    def close(self):
        pass


def scene(value=110):
    rng = np.random.default_rng(value)
    return rng.integers(0, 255, (48, 64, 3), dtype=np.uint8)


def test_real_frames_through_the_pipeline_become_contract_events_from_the_camera_device(tmp_path):
    transport = FixtureTransport()
    publisher = SecurityEventPublisher("cam-test", transport, directory=tmp_path)
    seen = []
    done = threading.Event()

    def on_event(event, ev):
        seen.append(event)
        if event.kind == "person_left_zone":
            done.set()

    frames = [scene() for _ in range(40)]
    det = ScriptedDetector(lambda n: [SEATED] if n <= 12 else [])
    vision = AirlockVision(tmp_path, SecurityEventSink(publisher, armed=lambda: (True, "session-1"), on_event=on_event), detector_factory=lambda: det)
    vision.start(frame_source=ScriptedCamera(frames))
    assert done.wait(10), vision.snapshot()
    vision.close()
    publisher.flush(2.0)
    publisher.close()

    kinds_seen = [e.kind.value for e in seen]
    assert kinds_seen[:2] == ["person_in_zone", "person_left_zone"]
    for e in seen:
        assert isinstance(e, SecurityEventV1) and e.source.value == "mac_vision" and e.device_id == "cam-test"
        assert e.metadata["armed"] is True and e.metadata["session_id"] == "session-1"
        assert e.metadata["camera"] == "mac_webcam" and e.metadata["capture_to_event_ms"] >= 0
        for key in e.metadata:  # frame_rate only describes frames; nothing here can carry a face, a picture or a name
            assert not {"face", "image", "video", "name", "embedding", "template", "jpeg"} & set(key.split("_")), key
            assert key == "frame_rate" or "frame" not in key, key
    entered, left = seen[0], seen[1]
    assert entered.observed_at <= left.observed_at and entered.seq < left.seq
    assert entered.metadata["started_at"] <= entered.observed_at
    snap = vision.snapshot()
    assert snap["stats"]["frames_processed"] >= 12 and snap["stats"]["detect_ms_p50"] is not None


def test_a_detector_that_crashes_reports_the_camera_offline_and_stops_cleanly(tmp_path):
    seen = []

    def boom(n):
        raise RuntimeError("model exploded")

    vision = AirlockVision(tmp_path, lambda ev: seen.append(ev.kind), detector_factory=lambda: ScriptedDetector(boom))
    vision.start(frame_source=ScriptedCamera([scene()] * 5))
    deadline = time.monotonic() + 5
    while vision.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not vision.running and "model exploded" in vision.error
    assert seen == ["camera_offline"]
    vision.close()


def test_an_obstructed_camera_concludes_nothing_about_people(tmp_path):
    seen = []
    dark = np.zeros((48, 64, 3), np.uint8)
    det = ScriptedDetector(lambda n: [])
    vision = AirlockVision(tmp_path, lambda ev: seen.append(ev.kind), detector_factory=lambda: det)
    vision.start(frame_source=ScriptedCamera([dark] * 45))
    deadline = time.monotonic() + 8
    while "camera_obstructed" not in seen and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    vision.close()
    assert "camera_obstructed" in seen and "zone_clear" not in seen
    assert det.calls == 0  # nothing is run on frames that show nothing


@pytest.mark.skipif(not (MODELS / "efficientdet_lite0.tflite").exists(), reason="run scripts/download_models.py --airlock-vision")
def test_the_real_person_detector_finds_nobody_in_an_empty_frame():
    from app.vision.airlock.inference import MediaPipePersonDetector

    det = MediaPipePersonDetector(MODELS)
    try:
        assert det.detect(np.zeros((480, 640, 3), np.uint8), 1) == []
        assert det.detect(np.full((480, 640, 3), 128, np.uint8), 1) == []  # same timestamp is bumped, not rejected
    finally:
        det.close()
