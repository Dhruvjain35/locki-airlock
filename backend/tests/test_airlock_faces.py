"""Owner enrollment and the four face states, with scripted faces and embeddings (no camera, no person)."""

from __future__ import annotations

import json
import math
import stat
import threading
import time

import numpy as np
import pytest

from app.security.publisher import FixtureTransport, SecurityEventPublisher
from app.vision.airlock.enrollment import (
    FaceEnrollment,
    OwnerTemplate,
    delete_template,
    load_template,
    save_template,
)
from app.vision.airlock.events import SecurityEventSink
from app.vision.airlock.faces import (
    MATCH,
    NONMATCH,
    UNSURE,
    FaceStage,
    FaceStateTracker,
    QualityConfig,
    Thresholds,
    assess,
    similarity,
    verdict,
)
from app.vision.airlock.geometry import Box
from app.vision.airlock.observations import FaceDet
from app.vision.airlock.pipeline import AirlockVision

from .test_airlock_vision import ScriptedCamera, ScriptedDetector

TH = Thresholds(match=0.5, reject=0.3)


def face(x=0.4, y=0.3, w=0.2, yaw=0.0, score=0.95):
    """A face box with YuNet's five landmarks; ``yaw`` moves the nose off the eye midpoint (in eye distances)."""
    h = w * 1.2
    ey = y + h * 0.4
    rx, lx = x + w * 0.3, x + w * 0.7
    nose = ((rx + lx) / 2 + yaw * (lx - rx), y + h * 0.6)
    lms = ((rx, ey), (lx, ey), nose, (rx + 0.01, y + h * 0.8), (lx - 0.01, y + h * 0.8))
    raw = tuple(v * 640 if i % 2 == 0 else v * 480 for i, v in enumerate([x, y, w, h, *[c for p in lms for c in p]])) + (score,)
    return FaceDet(Box(x, y, w, h), score, lms, raw)


def unit(seed):
    v = np.random.default_rng(seed).normal(size=128).astype(np.float32)
    return v / np.linalg.norm(v)


def feed(tracker, seconds, samples_at, person=True, fps=10, t0=0.0):
    out = []
    for i in range(int(seconds * fps)):
        t = t0 + i / fps
        out += [(round(t, 2), o) for o in tracker.update(t, person, samples_at(i))]
    return out


def jitter(i, base=0.4):
    return face(x=base + 0.004 * math.sin(i), yaw=0.03 * math.sin(i / 2))  # a real person never sits perfectly still


def test_similarity_is_the_mean_of_the_best_three_and_verdicts_have_an_uncertain_band():
    tpl = np.stack([unit(i) for i in range(5)])
    assert similarity(tpl[0], tpl) == pytest.approx((1.0 + np.sort(tpl[1:] @ tpl[0])[-2:].sum()) / 3, abs=1e-5)
    assert verdict(0.6, None, TH) == MATCH and verdict(0.2, None, TH) == NONMATCH and verdict(0.4, None, TH) == UNSURE
    assert verdict(0.9, "blurry", TH) == UNSURE  # a poor sample is never a match


def test_the_owner_is_matched_once_with_the_agreement_as_confidence():
    tr = FaceStateTracker(TH)
    ev = feed(tr, 3, lambda i: [(jitter(i), 0.72, None)])
    assert [o.kind for _, o in ev] == ["owner_face_match"]
    assert ev[0][1].confidence == 1.0 and ev[0][1].detail is None
    assert 0.8 <= ev[0][0] <= 1.2  # about a second: long enough to rule out a still photo, no longer


def test_someone_else_is_unrecognized_not_named():
    ev = feed(FaceStateTracker(TH), 3, lambda i: [(jitter(i), 0.12, None)])
    assert [o.kind for _, o in ev] == ["unrecognized_face"]


def test_a_blurry_or_turned_face_is_uncertain_never_a_match_or_an_intruder():
    ev = feed(FaceStateTracker(TH), 4, lambda i: [(jitter(i), 0.9 if i % 2 else 0.05, "blurry")])
    assert [o.kind for _, o in ev] == ["face_uncertain"]


def test_a_perfectly_still_owner_face_is_uncertain_possibly_a_photo():
    ev = feed(FaceStateTracker(TH), 4, lambda i: [(face(), 0.8, None)])
    kinds = [o.kind for _, o in ev]
    assert "owner_face_match" not in kinds and kinds[-1] == "face_uncertain"
    assert "photo" in ev[-1][1].detail


def test_someone_present_with_no_face_is_reported_once():
    ev = feed(FaceStateTracker(TH), 6, lambda i: [])
    assert [o.kind for _, o in ev] == ["no_face_visible"]
    assert ev[0][0] == pytest.approx(2.5, abs=0.11)


def test_without_an_enrollment_nobody_is_called_unrecognized():
    ev = feed(FaceStateTracker(None), 4, lambda i: [(jitter(i), float("nan"), None)])
    assert [o.kind for _, o in ev] == ["face_uncertain"] and "nobody is enrolled" in ev[0][1].detail


def test_owner_and_a_visitor_are_tracked_separately():
    def both(i):
        return [(jitter(i, 0.1), 0.75, None), (face(x=0.6 + 0.004 * math.sin(i), yaw=0.02 * math.cos(i)), 0.1, None)]

    kinds = sorted(o.kind for _, o in feed(FaceStateTracker(TH), 3, both))
    assert kinds == ["owner_face_match", "unrecognized_face"]


def test_quality_gates():
    q = QualityConfig()
    crop = np.random.default_rng(1).integers(30, 225, (112, 112, 3), dtype=np.uint8)
    assert assess(face(), 640, crop, q) is None
    assert assess(face(w=0.05), 640, crop, q) == "small"
    assert assess(face(x=0.9), 640, crop, q) == "cut_off"
    assert assess(face(yaw=0.6), 640, crop, q) == "turned"
    assert assess(face(score=0.5), 640, crop, q) == "low_score"
    assert assess(face(), 640, np.full((112, 112, 3), 10, np.uint8), q) == "dark"
    assert assess(face(), 640, np.full((112, 112, 3), 128, np.uint8), q) == "blurry"


# ---------------------------------------------------------------- template on disk

def test_the_template_is_owner_only_embeddings_and_delete_removes_it(tmp_path):
    path = tmp_path / "airlock" / "owner_face.json"
    tpl = OwnerTemplate(np.stack([unit(i) for i in range(15)]), {"center": 7, "left": 4, "right": 4}, Thresholds(), 1.0, 1.0)
    save_template(path, tpl)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert set(data) == {"version", "model", "detector", "created_at", "updated_at", "sessions", "poses", "thresholds", "calibration", "samples"}
    assert len(data["samples"]) == 15 and len(data["samples"][0]) == 128
    back = load_template(path)
    assert np.allclose(back.samples, tpl.samples, atol=1e-4) and not back.thresholds.calibrated
    assert delete_template(path) and not path.exists() and load_template(path) is None
    assert delete_template(path) is False


class FakeDetector:
    def __init__(self, plan):
        self.plan, self.n = plan, 0

    def detect(self, bgr):
        self.n += 1
        return self.plan(self.n)


class FakeEmbedder:
    def __init__(self):
        self.n = 0

    def embed(self, bgr, f):
        self.n += 1
        return unit(self.n), np.random.default_rng(self.n).integers(30, 225, (112, 112, 3), dtype=np.uint8)


def test_enrollment_needs_the_owner_alone_from_several_angles():
    yaws = [0.0] * 7 + [-0.2] * 4 + [0.2] * 4
    plan = lambda n: [face(w=0.25, yaw=yaws[(n - 1) % len(yaws)])] if n > 2 else [face(w=0.25), face(x=0.05, w=0.2)]  # noqa: E731
    enrol = FaceEnrollment(FakeDetector(plan), FakeEmbedder())
    t = 0.0
    while not enrol.complete and t < 30:
        p = enrol.offer(np.zeros((480, 640, 3), np.uint8), t)
        t += 0.4
    assert enrol.complete and p["accepted"] == 15 and p["poses"] == {"center": 7, "left": 4, "right": 4}
    assert enrol.rejected.get("more_than_one_face") == 2
    tpl = enrol.template()
    assert tpl.samples.shape == (15, 128)
    calibrated = OwnerTemplate(tpl.samples, tpl.poses, Thresholds(0.55, 0.25, True), 1.0, 1.0, calibration={"owner": 20})
    merged = enrol.template(calibrated)
    assert merged.samples.shape[0] == 30 and merged.sessions == 2
    assert not merged.thresholds.calibrated and merged.calibration is None  # new samples: calibrate again


# ---------------------------------------------------------------- through the pipeline

def test_face_states_leave_the_pipeline_as_contract_events_without_any_template(tmp_path):
    base = unit(100)
    owner = np.stack([(base + np.random.default_rng(i).normal(scale=0.03, size=128)).astype(np.float32) for i in range(10)])
    owner /= np.linalg.norm(owner, axis=1, keepdims=True)
    tpl = OwnerTemplate(owner, {"center": 10}, TH, 1.0, 1.0)

    class OwnerEmbedder:
        def embed(self, bgr, f):
            noise = np.random.default_rng(int(f.box.x * 1e6)).normal(scale=0.02, size=128).astype(np.float32)
            v = base + noise
            return v / np.linalg.norm(v), np.random.default_rng(3).integers(30, 225, (112, 112, 3), dtype=np.uint8)

    k = [0]

    def faces_plan(n):
        k[0] += 1
        return [jitter(k[0])]

    stage = FaceStage(tmp_path, tpl, every_n=1, detector=FakeDetector(faces_plan), embedder=OwnerEmbedder())
    publisher = SecurityEventPublisher("cam-test", FixtureTransport(), directory=tmp_path)
    seen, done = [], threading.Event()

    def on_event(event, ev):
        seen.append(event)
        if event.kind.value == "owner_face_match":
            done.set()

    vision = AirlockVision(tmp_path, SecurityEventSink(publisher, armed=lambda: (True, "s1"), on_event=on_event),
                           detector_factory=lambda: ScriptedDetector(lambda n: []), faces=stage)
    frames = [np.random.default_rng(i).integers(0, 255, (480, 640, 3), dtype=np.uint8) for i in range(40)]  # full size: faces are judged in pixels
    vision.start(frame_source=ScriptedCamera(frames))
    assert done.wait(10), vision.snapshot()
    vision.close()
    publisher.close()
    match = next(e for e in seen if e.kind.value == "owner_face_match")
    assert match.source.value == "mac_vision" and 0.5 <= match.confidence <= 1.0
    blob = json.dumps([e.model_dump(mode="json") for e in seen])
    assert "samples" not in blob and "embedding" not in blob
    # the large face in the desk zone also told the presence tracker someone is at the desk
    assert any(e.kind.value == "person_in_zone" for e in seen)
    assert vision.snapshot()["faces"]["enrolled"] is True


def test_the_pipeline_drops_live_embeddings(tmp_path):
    stage = FaceStage(tmp_path, OwnerTemplate(np.stack([unit(1)]), {}, TH, 1.0, 1.0), every_n=1,
                      detector=FakeDetector(lambda n: [jitter(n)]), embedder=FakeEmbedder())
    stage.process(time.monotonic(), np.zeros((480, 640, 3), np.uint8), True)
    assert not any(isinstance(v, np.ndarray) and v.shape == (128,) for v in vars(stage).values())


def test_a_settled_face_is_not_compared_again_until_it_is_due_or_changes(tmp_path):
    base = unit(7)
    owner = np.stack([base] * 5)
    calls = []

    class Counting:
        def embed(self, bgr, f):
            calls.append(f.box.w)
            return base, np.random.default_rng(len(calls)).integers(30, 225, (112, 112, 3), dtype=np.uint8)

    k = [0]

    def plan(n):
        k[0] += 1
        return [jitter(k[0]) if k[0] < 40 else face(x=0.4, w=0.3)]  # at pass 40 the face suddenly looks much bigger

    stage = FaceStage(tmp_path, OwnerTemplate(owner, {}, TH, 1.0, 1.0), every_n=1, detector=FakeDetector(plan), embedder=Counting())
    frame = np.zeros((480, 640, 3), np.uint8)
    events = []
    for i in range(45):  # 15 passes a second for 3 s
        events += [o.kind for o in stage.process(i / 15, frame, True)]
    assert events.count("owner_face_match") == 1
    settled_after = stage.tracker.tracks[0].compares
    assert settled_after < 30  # compared while settling, then only to revalidate (every 2 s) and when the face changed
    assert stage.tracker.tracks[0].skipped > 15
    snap = stage.snapshot()
    assert snap["identity_ms"]["owner_face_match"]["n"] == 1 and 800 <= snap["identity_ms"]["owner_face_match"]["p50"] <= 1200


def test_someone_else_taking_the_seat_is_caught_when_revalidation_disagrees():
    tr = FaceStateTracker(TH)
    events = []
    for i in range(80):  # 10 passes a second: the owner for 3 s, then a different face in the same place
        t = i / 10
        f = jitter(i)
        track = tr.match(f.box, t)
        score = (0.75 if i < 30 else 0.1) if tr.needs_compare(track, t) else None
        events += [(round(t, 1), o.kind) for o in tr.record(t, track, f, score, None)]
        events += [(round(t, 1), o.kind) for o in tr.finish(t, True, True)]
    kinds = [k for _, k in events]
    assert kinds[:2] == ["owner_face_match", "unrecognized_face"]
    changed = next(t for t, k in events if k == "unrecognized_face")
    assert changed <= 3.0 + 2.0 + 1.0  # within one revalidation period plus a short re-check
