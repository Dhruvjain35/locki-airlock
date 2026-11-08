"""Faces for AIRLOCK: find them, judge whether a sample is good enough, and compare it with the owner.

Only four outcomes ever leave this module: ``owner_face_match``, ``unrecognized_face``, ``face_uncertain`` and
``no_face_visible``. Nothing infers a name, age, gender, emotion or intent. A match is context for the
investigator and the voice; it never unlocks, disarms or authenticates anything.

Detection is OpenCV Zoo's YuNet and the embedding is SFace (128 numbers, compared by cosine similarity),
both through OpenCV's DNN module, so the Jetson can run them too. Embeddings of live faces are compared
and dropped. Only the owner's enrolled template is kept, on this machine (see enrollment.py).

Photographs: an owner match needs the face to move a little over the voting window. A printed photo or
a frozen screen propped in front of the camera stays perfectly still and is reported as uncertain. A
hand-held photo or a video replay is not caught. This is not biometric liveness detection.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .geometry import Box
from .inference import SFACE_MODEL, YUNET_MODEL, require_model
from .observations import FaceDet, Observation

MATCH, NONMATCH, UNSURE = "match", "nonmatch", "unsure"


class YuNetFaceDetector:
    name = "yunet_2023mar"

    def __init__(self, models_dir: Path, score_threshold: float = 0.7, nms: float = 0.3, top_k: int = 20):
        import cv2

        self._det = cv2.FaceDetectorYN.create(str(require_model(models_dir, YUNET_MODEL)), "", (320, 240), score_threshold, nms, top_k)
        self._size: tuple[int, int] | None = None

    def detect(self, bgr: np.ndarray) -> list[FaceDet]:
        h, w = bgr.shape[:2]
        if self._size != (w, h):
            self._det.setInputSize((w, h))
            self._size = (w, h)
        _, faces = self._det.detect(bgr)
        out: list[FaceDet] = []
        if faces is None:
            return out
        for row in faces:
            x, y, fw, fh = (float(v) for v in row[:4])
            lms = tuple((float(row[4 + 2 * i]) / w, float(row[5 + 2 * i]) / h) for i in range(5))
            box = Box(x / w, y / h, fw / w, fh / h)
            out.append(FaceDet(box, float(row[14]), lms, tuple(float(v) for v in row)))
        return out


class SFaceEmbedder:
    name = "sface_2021dec"

    def __init__(self, models_dir: Path):
        import cv2

        self._rec = cv2.FaceRecognizerSF.create(str(require_model(models_dir, SFACE_MODEL)), "")

    def embed(self, bgr: np.ndarray, face: FaceDet) -> tuple[np.ndarray, np.ndarray]:
        """(unit-length 128-d embedding, the 112x112 aligned crop). Neither is stored by this module."""
        aligned = self._rec.alignCrop(bgr, np.asarray(face.raw, np.float32).reshape(1, -1))
        feat = self._rec.feature(aligned).reshape(-1).astype(np.float32)
        return feat / (float(np.linalg.norm(feat)) + 1e-9), aligned


# ---------------------------------------------------------------- sample quality

@dataclass
class QualityConfig:
    min_score: float = 0.8
    min_width_px: int = 60  # SFace works on 112 px crops; smaller faces are upscaled guesses
    max_yaw: float = 0.35  # nose offset from the eye midpoint, in eye distances
    max_roll_deg: float = 25.0
    min_sharpness: float = 25.0  # Laplacian variance of the aligned grey crop
    min_brightness: float = 40.0
    max_brightness: float = 225.0
    edge_margin: float = 0.01  # a face cut by the frame edge is not a full sample


def face_pose(face: FaceDet) -> tuple[float, float]:
    """(yaw, roll in degrees) from YuNet's landmarks: right eye, left eye, nose tip, mouth corners."""
    (rx, ry), (lx, ly), (nx, _ny) = face.landmarks[0], face.landmarks[1], face.landmarks[2]
    eye_dist = math.hypot(lx - rx, ly - ry) or 1e-6
    yaw = (nx - (rx + lx) / 2) / eye_dist
    roll = math.degrees(math.atan2(ly - ry, lx - rx))
    return yaw, roll


def assess(face: FaceDet, frame_w: int, aligned: np.ndarray | None, cfg: QualityConfig) -> str | None:
    """None if the sample is usable, otherwise the first reason it is not."""
    import cv2

    b = face.box
    if face.score < cfg.min_score:
        return "low_score"
    if b.w * frame_w < cfg.min_width_px:
        return "small"
    m = cfg.edge_margin
    if b.x < m or b.y < m or b.x + b.w > 1 - m or b.y + b.h > 1 - m:
        return "cut_off"
    yaw, roll = face_pose(face)
    if abs(yaw) > cfg.max_yaw:
        return "turned"
    if abs(roll) > cfg.max_roll_deg and abs(abs(roll) - 180) > cfg.max_roll_deg:
        return "tilted"
    if aligned is not None:
        grey = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        mean = float(grey.mean())
        if mean < cfg.min_brightness:
            return "dark"
        if mean > cfg.max_brightness:
            return "bright"
        if float(cv2.Laplacian(grey, cv2.CV_64F).var()) < cfg.min_sharpness:
            return "blurry"
    return None


# ---------------------------------------------------------------- comparison

@dataclass
class Thresholds:
    match: float = 0.50  # top-3 mean cosine at or above this: matches the owner
    reject: float = 0.30  # at or below this: does not match. Between the two: uncertain
    calibrated: bool = False


def similarity(embedding: np.ndarray, template: np.ndarray, top: int = 3) -> float:
    """Mean of the ``top`` best cosine similarities against the enrolled samples (rows of ``template``)."""
    sims = template @ embedding
    k = min(top, sims.shape[0])
    return float(np.sort(sims)[-k:].mean())


def verdict(score: float, quality_problem: str | None, th: Thresholds) -> str:
    if quality_problem is not None:
        return UNSURE
    if score >= th.match:
        return MATCH
    if score <= th.reject:
        return NONMATCH
    return UNSURE


# ---------------------------------------------------------------- face states over time

@dataclass
class FaceStateConfig:
    window: int = 6  # recent verdicts kept per face track
    settle: int = 4  # agreeing verdicts within window_s that settle owner or unrecognized
    window_s: float = 1.5
    min_settle_s: float = 0.4  # a face is watched at least this long before it is called anything
    uncertain_after_s: float = 1.5  # seen this long (or re-checked this long) without settling: uncertain
    no_face_after_s: float = 2.5  # someone present this long with no face at all
    track_iou: float = 0.3
    track_lost_s: float = 1.0
    revalidate_s: float = 1.0  # a settled face is compared again this often (someone else taking the seat is caught fast)...
    change_area: float = 0.4  # ...or as soon as its box grows or shrinks by this share
    still_s: float = 1.0  # an owner match needs some movement over this long (photo check)
    still_center: float = 0.0015  # std of the face centre, as a fraction of the frame
    still_yaw: float = 0.006


@dataclass
class _Track:
    id: int
    box: Box
    first: float
    last: float
    verdicts: deque = field(default_factory=lambda: deque(maxlen=6))
    scores: deque = field(default_factory=lambda: deque(maxlen=6))
    motion: deque = field(default_factory=lambda: deque(maxlen=90))  # (t, cx, cy, yaw)
    state: str | None = None  # the last state reported for this face
    settled_at: float | None = None
    rechecking: bool = True  # compared on every pass until a state settles, then only to revalidate
    recheck_since: float | None = None
    last_compare: float | None = None
    compare_area: float | None = None
    compares: int = 0
    skipped: int = 0
    last_score: float | None = None  # shown on this machine's preview only; never sent anywhere
    last_problem: str | None = None


def _iou(a: Box, b: Box) -> float:
    x1, y1 = max(a.x, b.x), max(a.y, b.y)
    x2, y2 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


class FaceStateTracker:
    """Per-face tracks with cached recognition: a new face is compared on every pass until its state
    settles (about 0.4-1 s), then only every ``revalidate_s`` or when it changes, and on any disagreement
    it is compared on every pass again. One event per change of what can be said about a face."""

    def __init__(self, thresholds: Thresholds | None, cfg: FaceStateConfig | None = None):
        self.thresholds = thresholds  # None: nobody enrolled, so no face can be called unrecognized
        self.cfg = cfg or FaceStateConfig()
        self.tracks: list[_Track] = []
        self._next_id = 1
        self._person_since: float | None = None
        self._last_face: float | None = None
        self._no_face_reported = False
        self.identity_ms: dict[str, deque] = {k: deque(maxlen=100) for k in ("owner_face_match", "unrecognized_face", "face_uncertain")}

    def reset(self) -> None:
        self.tracks.clear()
        self._person_since = None
        self._last_face = None
        self._no_face_reported = False

    # -- the three steps of a pass: match each face to a track, record what was compared, then finish
    def match(self, box: Box, t: float) -> _Track:
        best = max(self.tracks, key=lambda tr: _iou(tr.box, box), default=None)
        if best is not None and _iou(best.box, box) >= self.cfg.track_iou:
            best.box, best.last = box, t
            return best
        tr = _Track(self._next_id, box, t, t, deque(maxlen=self.cfg.window), deque(maxlen=self.cfg.window), recheck_since=t)
        self._next_id += 1
        self.tracks.append(tr)
        return tr

    def needs_compare(self, tr: _Track, t: float) -> bool:
        if tr.rechecking or tr.last_compare is None or t - tr.last_compare >= self.cfg.revalidate_s:
            return True
        return tr.compare_area is not None and abs(tr.box.area / max(tr.compare_area, 1e-9) - 1.0) > self.cfg.change_area

    def record(self, t: float, tr: _Track, face: FaceDet, score: float | None, problem: str | None) -> list[Observation]:
        """``score`` None: not compared this pass (the cached state stands); nan: nobody enrolled."""
        yaw, _ = face_pose(face)
        cx, cy = face.box.center
        tr.motion.append((t, cx, cy, yaw))
        if score is None:
            tr.skipped += 1
            return []
        tr.last_compare, tr.compare_area = t, face.box.area
        tr.compares += 1
        tr.last_score, tr.last_problem = (None if math.isnan(score) else score), problem
        v = UNSURE if self.thresholds is None or math.isnan(score) else verdict(score, problem, self.thresholds)
        tr.verdicts.append((t, v))
        if not math.isnan(score):
            tr.scores.append(score)
        if not tr.rechecking and tr.state is not None and v != {"owner_face_match": MATCH, "unrecognized_face": NONMATCH}.get(tr.state, UNSURE):
            tr.rechecking, tr.recheck_since = True, t  # it disagrees with what was settled: compare every pass again
        return self._settle(tr, t)

    def finish(self, t: float, person_present: bool, any_face: bool) -> list[Observation]:
        cfg = self.cfg
        out: list[Observation] = []
        if not person_present and not any_face:
            self.reset()
            return out
        if self._person_since is None:
            self._person_since = t
        if any_face:
            self._last_face = t
            self._no_face_reported = False
        self.tracks = [tr for tr in self.tracks if t - tr.last <= cfg.track_lost_s]
        quiet_since = self._last_face if self._last_face is not None else self._person_since
        if person_present and not self._no_face_reported and t - quiet_since >= cfg.no_face_after_s:
            self._no_face_reported = True
            out.append(Observation("no_face_visible", t, None, started=quiet_since, dwell_ms=int((t - quiet_since) * 1000),
                                   detail="someone is in view but no face has been visible"))
        return out

    def update(self, t: float, person_present: bool, samples: list[tuple[FaceDet, float | None, str | None]]) -> list[Observation]:
        """All three steps at once, for callers that compare every face themselves."""
        out: list[Observation] = []
        for face, score, problem in samples:
            out += self.record(t, self.match(face.box, t), face, score, problem)
        return out + self.finish(t, person_present, bool(samples))

    def _still(self, tr: _Track, t: float) -> bool | None:
        """True: the face has not moved at all (a photo, or a frozen screen). None: not watched long enough to say."""
        pts = [m for m in tr.motion if t - m[0] <= self.cfg.still_s]
        if len(pts) < 5 or t - pts[0][0] < self.cfg.still_s * 0.8:
            return None
        a = np.array([(m[1], m[2], m[3]) for m in pts])
        return float(np.hypot(a[:, 0].std(), a[:, 1].std())) < self.cfg.still_center and float(a[:, 2].std()) < self.cfg.still_yaw

    def _settle(self, tr: _Track, t: float) -> list[Observation]:
        cfg = self.cfg
        recent = [v for (vt, v) in tr.verdicts if t - vt <= cfg.window_s]
        new = None
        detail = None
        if t - tr.first < cfg.min_settle_s:
            return []
        if recent.count(MATCH) >= cfg.settle:
            still = self._still(tr, t)
            if still is None:
                return []  # an owner match waits until the face has been watched long enough for the photo check
            if still:
                new, detail = "face_uncertain", "matches the enrolled owner but did not move at all (possibly a photo)"
            else:
                new = "owner_face_match"
        elif recent.count(NONMATCH) >= cfg.settle:
            new = "unrecognized_face"
        elif tr.rechecking and t - (tr.recheck_since or tr.first) >= cfg.uncertain_after_s and tr.state != "face_uncertain":
            new = "face_uncertain"
            detail = "nobody is enrolled" if self.thresholds is None else "the face could not be compared reliably"
        if new is None:
            return []
        if new in ("owner_face_match", "unrecognized_face") or (new == "face_uncertain" and detail and "photo" in detail):
            tr.rechecking = False  # settled: from now on only revalidate
        else:
            tr.recheck_since = t  # uncertain: keep comparing, and wait another window before saying it again
        if new == tr.state:
            return []
        if tr.state is None:
            self.identity_ms[new].append((t - tr.first) * 1000)
        tr.state, tr.settled_at = new, t
        conf = None  # the share of recent frames that agree: how settled the vote was, not a probability of identity
        if new == "owner_face_match":
            conf = round(recent.count(MATCH) / len(recent), 3)
        elif new == "unrecognized_face":
            conf = round(recent.count(NONMATCH) / len(recent), 3)
        return [Observation(new, t, conf, started=tr.first, dwell_ms=int((t - tr.first) * 1000), detail=detail, track=tr.id)]

    def snapshot(self) -> dict:
        return {"tracks": [{"id": tr.id, "state": tr.state, "compares": tr.compares, "cached_passes": tr.skipped, "verdicts": [v for _, v in tr.verdicts]} for tr in self.tracks],
                "enrolled": self.thresholds is not None}


def _pct(values, q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(len(v) * q))], 1)


class FaceStage:
    """Faces every ``every_n`` frames: detect on a half-size copy, compare a face only when its track needs it
    (new, unsettled, due for revalidation, or changed), and settle the four states. Live embeddings are dropped
    as soon as they are compared."""

    def __init__(self, models_dir: Path, template=None, *, every_n: int = 2, max_faces: int = 3, detect_scale: float = 0.5,
                 quality: QualityConfig | None = None, state: FaceStateConfig | None = None, detector=None, embedder=None):
        self.detector = detector or YuNetFaceDetector(models_dir)
        self.template = template
        self.embedder = embedder if embedder is not None else (SFaceEmbedder(models_dir) if template is not None else None)
        self.tracker = FaceStateTracker(template.thresholds if template is not None else None, state)
        self.quality = quality or QualityConfig()
        self.every_n, self.max_faces, self.detect_scale = max(1, every_n), max_faces, detect_scale
        self._models_dir = models_dir
        self._swap = __import__("threading").Lock()  # the template can change (enrollment) while frames are processed
        self.last_pairs: list = []  # (face, track) from the last pass, for the preview
        self._n = 0
        self.ms: deque = deque(maxlen=300)
        self.detect_ms: deque = deque(maxlen=300)
        self.compare_ms: deque = deque(maxlen=300)
        self.last_faces: list[FaceDet] = []
        self._desk_face_t: float | None = None

    def reset(self) -> None:
        with self._swap:
            self.tracker.reset()
            self.last_faces = []
            self.last_pairs = []
            self._desk_face_t = None

    def set_template(self, template) -> None:
        """Use this owner template from the next pass (None: nobody enrolled). Every face is judged afresh."""
        with self._swap:
            if template is not None and self.embedder is None:
                self.embedder = SFaceEmbedder(self._models_dir)
            self.template = template
            self.tracker = FaceStateTracker(template.thresholds if template is not None else None, self.tracker.cfg)
            self.last_pairs = []

    def display(self) -> list[tuple]:
        """(face, state, score, problem, track id) for the faces of the last pass, for the local preview."""
        return [(f, tr.state, tr.last_score, tr.last_problem, tr.id) for f, tr in list(self.last_pairs)]

    def _detect(self, bgr: np.ndarray) -> list[FaceDet]:
        """YuNet on a smaller copy when the frame is large; boxes and landmarks scaled back for alignment."""
        import cv2

        h, w = bgr.shape[:2]
        k = self.detect_scale
        if k >= 1.0 or w < 480 or not isinstance(self.detector, YuNetFaceDetector):
            return self.detector.detect(bgr)
        small = cv2.resize(bgr, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        out = []
        for f in self.detector.detect(small):
            raw = tuple(v / k for v in f.raw[:14]) + (f.raw[14],)
            out.append(FaceDet(f.box, f.score, f.landmarks, raw))  # box and landmarks are fractions: unchanged
        return out

    def process(self, t: float, bgr: np.ndarray, someone: bool) -> list[Observation]:
        self._n += 1
        if self._n % self.every_n:
            return []
        with self._swap:
            return self._process(t, bgr, someone)

    def _process(self, t: float, bgr: np.ndarray, someone: bool) -> list[Observation]:
        import time as _time

        t0 = _time.perf_counter()
        faces = sorted(self._detect(bgr), key=lambda f: f.box.area, reverse=True)[: self.max_faces]
        t1 = _time.perf_counter()
        self.detect_ms.append((t1 - t0) * 1000)
        out: list[Observation] = []
        pairs = []
        for face in faces:
            tr = self.tracker.match(face.box, t)
            pairs.append((face, tr))
            score, problem = None, None
            if self.embedder is not None and self.template is not None:
                if self.tracker.needs_compare(tr, t):
                    problem = assess(face, bgr.shape[1], None, self.quality)  # the free geometric checks first
                    if problem is not None:
                        score = float("nan")  # too small, cut off, turned or tilted: not worth embedding, and unsure
                    else:
                        c0 = _time.perf_counter()
                        emb, aligned = self.embedder.embed(bgr, face)
                        problem = assess(face, bgr.shape[1], aligned, self.quality)
                        score = similarity(emb, self.template.samples)
                        del emb, aligned
                        self.compare_ms.append((_time.perf_counter() - c0) * 1000)
            elif tr.rechecking:
                problem, score = assess(face, bgr.shape[1], None, self.quality), float("nan")
            out += self.tracker.record(t, tr, face, score, problem)
        self.last_faces = faces
        self.last_pairs = pairs
        out += self.tracker.finish(t, someone or bool(faces), bool(faces))
        self.ms.append((_time.perf_counter() - t0) * 1000)
        return out

    def close_face_at_desk(self, t: float, zones) -> bool:
        """A large face inside the desk zone counts as someone at the desk for the presence tracker,
        for close-ups the person detector misses. Held for 0.4 s because faces run every few frames."""
        from .geometry import box_fraction_in

        desk = zones.get("desk") or []
        if any(f.box.w >= 0.15 and box_fraction_in(f.box, desk) >= 0.5 for f in self.last_faces):
            self._desk_face_t = t
        return self._desk_face_t is not None and t - self._desk_face_t <= 0.4

    def snapshot(self) -> dict:
        tr = self.tracker
        return {**tr.snapshot(), "faces_in_last_pass": len(self.last_faces), "ms_p50": _pct(self.ms, 0.5), "ms_p95": _pct(self.ms, 0.95),
                "detect_ms_p50": _pct(self.detect_ms, 0.5), "detect_ms_p95": _pct(self.detect_ms, 0.95),
                "compare_ms_p50": _pct(self.compare_ms, 0.5), "compare_ms_p95": _pct(self.compare_ms, 0.95), "compares": len(self.compare_ms),
                "identity_ms": {k: {"n": len(v), "p50": _pct(v, 0.5), "p95": _pct(v, 0.95)} for k, v in tr.identity_ms.items()},
                "every_n_frames": self.every_n, "calibrated": bool(self.template is not None and self.template.thresholds.calibrated)}
