"""Voluntary owner enrollment: the owner starts it, the camera collects face embeddings from several
angles, and only those numbers are kept, on this machine, in a file only this user can read.

The template (data/airlock/owner_face.json, mode 0600) holds SFace embeddings: 128 numbers per sample,
no picture. It never goes to SpacetimeDB, the dashboard, the logs or the investigator. ``delete_template``
overwrites the file and removes it. Thresholds start at a conservative default and are set from the
owner's own held-out samples and consenting visitors by ``scripts/airlock_face.py calibrate``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .faces import QualityConfig, SFaceEmbedder, Thresholds, YuNetFaceDetector, assess, face_pose

TEMPLATE_VERSION = 1
MAX_SAMPLES = 60
POSES = ("center", "left", "right")


def template_path(data_dir: Path) -> Path:
    return Path(data_dir) / "airlock" / "owner_face.json"


@dataclass
class OwnerTemplate:
    samples: np.ndarray  # (n, 128) unit vectors
    poses: dict[str, int]
    thresholds: Thresholds
    created_at: float
    updated_at: float
    sessions: int = 1
    calibration: dict | None = None
    model: str = SFaceEmbedder.name
    detector: str = YuNetFaceDetector.name

    def to_dict(self) -> dict:
        return {
            "version": TEMPLATE_VERSION,
            "model": self.model,
            "detector": self.detector,
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
            "sessions": self.sessions,
            "poses": self.poses,
            "thresholds": {"match": self.thresholds.match, "reject": self.thresholds.reject, "calibrated": self.thresholds.calibrated},
            "calibration": self.calibration,
            "samples": [[round(float(v), 5) for v in row] for row in self.samples],
        }

    @classmethod
    def from_dict(cls, data: dict) -> OwnerTemplate:
        if data.get("version") != TEMPLATE_VERSION or data.get("model") != SFaceEmbedder.name:
            raise ValueError("owner template is from another version or model; enroll again")
        samples = np.asarray(data["samples"], np.float32)
        if samples.ndim != 2 or samples.shape[1] != 128 or not 1 <= samples.shape[0] <= MAX_SAMPLES:
            raise ValueError("owner template is malformed")
        samples /= np.linalg.norm(samples, axis=1, keepdims=True) + 1e-9
        th = data.get("thresholds") or {}
        return cls(samples, dict(data.get("poses") or {}), Thresholds(float(th.get("match", 0.5)), float(th.get("reject", 0.3)), bool(th.get("calibrated"))),
                   float(data.get("created_at", 0)), float(data.get("updated_at", 0)), int(data.get("sessions", 1)), data.get("calibration"))


def load_template(path: Path) -> OwnerTemplate | None:
    try:
        return OwnerTemplate.from_dict(json.loads(Path(path).read_text()))
    except FileNotFoundError:
        return None


def save_template(path: Path, template: OwnerTemplate) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(template.to_dict(), f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def delete_template(path: Path) -> bool:
    """Overwrite then remove. True if there was a template. (On an SSD, overwriting is best effort.)"""
    path = Path(path)
    if not path.exists():
        return False
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.write(b"\0" * size)
        f.flush()
        os.fsync(f.fileno())
    path.unlink()
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    return True


@dataclass
class EnrollmentConfig:
    samples: int = 15
    per_pose: dict[str, int] = field(default_factory=lambda: {"center": 7, "left": 4, "right": 4})
    min_gap_s: float = 0.35  # between accepted samples, so they are not near-copies
    min_change: float = 0.02  # an accepted sample must differ a little from the previous one (cosine)
    side_yaw: float = 0.12  # beyond this the sample counts as turned left or right
    quality: QualityConfig = field(default_factory=lambda: QualityConfig(min_score=0.85, min_width_px=80))


class FaceEnrollment:
    """Feed frames to ``offer``; read ``progress``; when ``complete``, ``template()`` builds the result."""

    def __init__(self, detector: YuNetFaceDetector, embedder: SFaceEmbedder, cfg: EnrollmentConfig | None = None, clock=time.monotonic):
        self.detector, self.embedder = detector, embedder
        self.cfg = cfg or EnrollmentConfig()
        self._clock = clock
        self.accepted: list[np.ndarray] = []
        self.poses = {p: 0 for p in POSES}
        self.rejected: dict[str, int] = {}
        self._last_t: float | None = None

    def pose_of(self, yaw: float) -> str:
        if yaw <= -self.cfg.side_yaw:
            return "left"
        if yaw >= self.cfg.side_yaw:
            return "right"
        return "center"

    @property
    def complete(self) -> bool:
        return all(self.poses[p] >= n for p, n in self.cfg.per_pose.items())

    def hint(self) -> str:
        if self.complete:
            return "done"
        for pose, need in self.cfg.per_pose.items():
            if self.poses[pose] < need:
                return {"center": "look at the camera", "left": "turn your head slightly to one side", "right": "turn your head slightly to the other side"}[pose]
        return "hold still"

    def _reject(self, why: str) -> dict:
        self.rejected[why] = self.rejected.get(why, 0) + 1
        return self.progress(why)

    def offer(self, bgr: np.ndarray, t: float | None = None) -> dict:
        t = self._clock() if t is None else t
        if self.complete:
            return self.progress(None)
        if self._last_t is not None and t - self._last_t < self.cfg.min_gap_s:
            return self.progress(None)
        faces = self.detector.detect(bgr)
        if not faces:
            return self._reject("no_face")
        if len(faces) > 1:
            return self._reject("more_than_one_face")  # enrollment is for the owner alone
        face = faces[0]
        emb, aligned = self.embedder.embed(bgr, face)
        problem = assess(face, bgr.shape[1], aligned, self.cfg.quality)
        if problem == "turned":
            problem = None if abs(face_pose(face)[0]) <= 0.45 else "turned_too_far"
        if problem is not None:
            return self._reject(problem)
        pose = self.pose_of(face_pose(face)[0])
        if self.poses[pose] >= self.cfg.per_pose.get(pose, 0):
            return self._reject(f"enough_{pose}")
        if self.accepted and float(self.accepted[-1] @ emb) > 1 - self.cfg.min_change:
            return self._reject("same_as_last")
        self.accepted.append(emb)
        self.poses[pose] += 1
        self._last_t = t
        return self.progress(None)

    def progress(self, last_problem: str | None) -> dict:
        return {"accepted": len(self.accepted), "needed": sum(self.cfg.per_pose.values()), "poses": dict(self.poses),
                "hint": self.hint(), "last_problem": last_problem, "rejected": dict(self.rejected), "complete": self.complete}

    def template(self, existing: OwnerTemplate | None = None) -> OwnerTemplate:
        if not self.accepted:
            raise ValueError("no samples accepted")
        now = time.time()
        new = np.stack(self.accepted).astype(np.float32)
        if existing is None:
            return OwnerTemplate(new, dict(self.poses), Thresholds(), now, now)
        merged = np.concatenate([existing.samples, new])[-MAX_SAMPLES:]
        poses = {p: existing.poses.get(p, 0) + self.poses.get(p, 0) for p in POSES}
        # Adding samples changes the score distribution, so earlier calibration no longer applies.
        return OwnerTemplate(merged, poses, Thresholds(existing.thresholds.match, existing.thresholds.reject, False),
                             existing.created_at, now, existing.sessions + 1, None)
