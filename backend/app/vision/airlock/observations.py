"""What one processed frame contains, and what a tracker concluded from several.

Detections live in memory for one frame. An ``Observation`` is what a tracker settled on over several
frames; the pipeline turns it into a SecurityEventV1. Neither ever holds pixels or a face template.
Only the standard library, so the Jetson runs the same code.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import Box


@dataclass(frozen=True)
class PersonDet:
    box: Box
    score: float


@dataclass(frozen=True)
class FaceDet:
    box: Box
    score: float
    landmarks: tuple[tuple[float, float], ...] = ()  # eyes, nose tip, mouth corners (normalized)
    raw: tuple | None = None  # the detector's own row in pixels, for alignment; never leaves this process


@dataclass(frozen=True)
class HandDet:
    points: tuple[tuple[float, float], ...]  # 21 MediaPipe landmarks (normalized)
    score: float


@dataclass(frozen=True)
class FrameStats:
    """Grey-level mean and spread of a small copy of the frame: enough to tell a covered or dark lens."""

    mean: float
    std: float


@dataclass(frozen=True)
class Observation:
    kind: str  # a SecurityKind value
    at: float  # monotonic time of the frame that settled it
    confidence: float | None = None  # a detector score; None for absences and camera states
    zone: str | None = None
    started: float | None = None  # monotonic time of the first frame of the observation
    dwell_ms: int | None = None
    persons_in_view: int | None = None
    detail: str | None = None
    track: int | None = None  # the face or person track this is about (a number, never an identity)
