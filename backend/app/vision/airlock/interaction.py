"""Hands in the owner's calibrated zones: near the keyboard, on the laptop, at the USB ports, on a protected device.

Geometry and time only, with no classifier of "suspicious" movement. A hand is ``near`` a zone when any of
its 21 landmarks falls inside the zone grown by a margin. It is ``in`` the zone when at least two
fingertips fall inside the zone itself. An episode starts with the hand present and ends after it has
been gone for ``gone_s``.

* ``hand_near_*``     near for ``near_s`` (only zones that have a near event: keyboard, USB)
* ``*_interaction``   fingertips in the zone for the zone's dwell, with the tracked hand present in at least
                      70% of frames and no jump across the image. For the keyboard, the fingertips must
                      also move, so a hand resting on the keys is not typing.

A hand at the USB ports is not a device being connected: the Mac's USB watcher is the only authority on
that, and the database correlates the two. Hands hidden by the body or out of view produce nothing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .geometry import Point, ZoneMap, point_in_polygon
from .observations import HandDet, Observation

FINGERTIPS = (4, 8, 12, 16, 20)
NEAR_KIND = {"keyboard_zone": "hand_near_keyboard", "usb_port_zone": "hand_near_usb_zone"}
INTERACTION_KIND = {
    "keyboard_zone": "keyboard_interaction",
    "laptop_zone": "laptop_interaction",
    "usb_port_zone": "usb_zone_interaction",
    "protected_device_zone": "protected_device_interaction",
}


@dataclass
class InteractionConfig:
    min_score: float = 0.5
    margin: float = 0.04  # "near": the zone grown by this fraction of the frame
    near_s: float = 0.4
    dwell_s: dict[str, float] = field(default_factory=lambda: {"keyboard_zone": 1.0, "laptop_zone": 1.0, "usb_port_zone": 0.6, "protected_device_zone": 0.8})
    min_tips: int = 2
    presence_share: float = 0.7  # of frames during the dwell in which the hand was there
    max_jump: float = 0.25  # fingertip centre moving more than this between frames is a different hand or a glitch
    typing_motion: float = 0.004  # std of fingertip positions (fraction of the frame) that counts as moving
    gone_s: float = 1.0
    out_s: float = 0.5  # fingertips out of the zone this long: the dwell starts again


def grow(poly: list[Point], margin: float) -> list[Point]:
    """The polygon pushed out from its centroid by ``margin``: a cheap, convex-enough 'near' region."""
    cx = sum(p[0] for p in poly) / len(poly)
    cy = sum(p[1] for p in poly) / len(poly)
    out = []
    for x, y in poly:
        dx, dy = x - cx, y - cy
        d = (dx * dx + dy * dy) ** 0.5 or 1e-6
        out.append((x + dx / d * margin, y + dy / d * margin))
    return out


@dataclass
class _Episode:
    zone: str
    start: float
    last_seen: float
    in_since: float | None = None
    last_in: float | None = None
    near_since: float | None = None
    frames_since_in: int = 0  # processed frames since the fingertips first went in
    in_frames: int = 0  # of those, frames with the fingertips in
    tips: deque = field(default_factory=lambda: deque(maxlen=90))  # (t, x, y) centre of fingertips inside
    scores: deque = field(default_factory=lambda: deque(maxlen=90))
    near_reported: bool = False
    interaction_reported: bool = False
    last_center: tuple[float, float] | None = None


class InteractionTracker:
    def __init__(self, zones: ZoneMap, cfg: InteractionConfig | None = None):
        self.zones = zones
        self.cfg = cfg or InteractionConfig()
        self.episodes: dict[str, _Episode] = {}

    def reset(self) -> None:
        self.episodes.clear()

    def watched(self) -> list[str]:
        return [z for z in INTERACTION_KIND if self.zones.get(z)]

    def update(self, t: float, hands: list[HandDet]) -> list[Observation]:
        cfg = self.cfg
        out: list[Observation] = []
        hands = [h for h in hands if h.score >= cfg.min_score]
        for zone in self.watched():
            poly = self.zones.get(zone)
            near_poly = grow(poly, cfg.margin)
            near = [h for h in hands if any(point_in_polygon(x, y, near_poly) for x, y in h.points)]
            inside = []
            for h in near:
                tips = [h.points[i] for i in FINGERTIPS if i < len(h.points) and point_in_polygon(*h.points[i], poly)]
                if len(tips) >= cfg.min_tips:
                    inside.append((h, tips))
            ep = self.episodes.get(zone)
            if ep is not None and ep.in_since is not None:
                ep.frames_since_in += 1
                if not inside and t - ep.last_in > cfg.out_s:
                    ep.in_since, ep.last_in, ep.frames_since_in, ep.in_frames, ep.last_center = None, None, 0, 0, None
                    ep.tips.clear()
            if not near:
                if ep is not None and t - ep.last_seen >= cfg.gone_s:
                    del self.episodes[zone]
                continue
            if ep is None:
                ep = self.episodes[zone] = _Episode(zone, t, t, near_since=t)
            ep.last_seen = t
            ep.scores.append(max(h.score for h in near))
            if inside:
                h, tips = max(inside, key=lambda item: len(item[1]))
                cx = float(np.mean([p[0] for p in tips]))
                cy = float(np.mean([p[1] for p in tips]))
                if ep.last_center is not None and ((cx - ep.last_center[0]) ** 2 + (cy - ep.last_center[1]) ** 2) ** 0.5 > cfg.max_jump:
                    ep.in_since, ep.in_frames, ep.frames_since_in = None, 0, 0  # a jump: start the dwell again
                    ep.tips.clear()
                ep.last_center = (cx, cy)
                if ep.in_since is None:
                    ep.in_since, ep.frames_since_in = t, 1
                ep.last_in = t
                ep.in_frames += 1
                ep.tips.append((t, cx, cy))
            out += self._decide(ep, t)
        return out

    def _decide(self, ep: _Episode, t: float) -> list[Observation]:
        cfg = self.cfg
        out: list[Observation] = []
        conf = round(float(np.mean(list(ep.scores))), 3) if ep.scores else None
        kind = NEAR_KIND.get(ep.zone)
        if kind and not ep.near_reported and t - ep.near_since >= cfg.near_s:
            ep.near_reported = True
            out.append(Observation(kind, t, conf, zone=ep.zone, started=ep.near_since, dwell_ms=int((t - ep.near_since) * 1000)))
        if ep.interaction_reported or ep.in_since is None:
            return out
        dwell = t - ep.in_since
        if dwell < cfg.dwell_s.get(ep.zone, 1.0):
            return out
        recent = [p for p in ep.tips if p[0] >= ep.in_since]
        if ep.in_frames / max(1, ep.frames_since_in) < cfg.presence_share:
            return out
        detail = f"{cfg.min_tips}+ fingertips inside for {dwell:.1f} s"
        if ep.zone == "keyboard_zone":
            xs = np.array([p[1] for p in recent])
            ys = np.array([p[2] for p in recent])
            motion = float(np.hypot(xs.std(), ys.std())) if len(recent) >= 3 else 0.0
            if motion < cfg.typing_motion:
                return out  # resting on the keys, not typing
            detail += f", moving (spread {motion:.3f})"
        ep.interaction_reported = True
        out.append(Observation(INTERACTION_KIND[ep.zone], t, conf, zone=ep.zone, started=ep.in_since, dwell_ms=int(dwell * 1000), detail=detail))
        return out

    def snapshot(self) -> dict:
        return {"watching": self.watched(), "active": {z: {"near": True, "in": ep.in_since is not None, "reported": ep.interaction_reported} for z, ep in self.episodes.items()}}


class HandStage:
    """MediaPipe hand landmarks every ``every_n`` frames, into the interaction tracker. Runs whether or not
    a person box is visible: a hand reaching in from the edge of the picture may be all there is."""

    inference = "mac_mediapipe"

    def __init__(self, models_dir, zones: ZoneMap, *, every_n: int = 2, cfg: InteractionConfig | None = None, model=None):
        if model is None:
            from ..hands import HandTracker

            model = HandTracker(models_dir)
        self.model = model
        self.tracker = InteractionTracker(zones, cfg)
        self.every_n = max(1, every_n)
        self._n = 0
        self._last_ts = -1
        self.ms: deque = deque(maxlen=300)
        self.last_hands: list[HandDet] = []

    def reset(self) -> None:
        self.tracker.reset()
        self.last_hands = []

    def process(self, t: float, rgb, ts_ms: int) -> list[Observation]:
        import time as _time

        if not self.tracker.watched():
            return []
        self._n += 1
        if self._n % self.every_n:
            return []
        ts_ms = max(int(ts_ms), self._last_ts + 1)
        self._last_ts = ts_ms
        t0 = _time.perf_counter()
        hands = [HandDet(tuple(tuple(p) for p in h.landmarks), float(h.score)) for h in self.model.detect(rgb, ts_ms)]
        self.ms.append((_time.perf_counter() - t0) * 1000)
        self.last_hands = hands
        return self.tracker.update(t, hands)

    def close(self) -> None:
        try:
            self.model.close()
        except Exception:
            pass

    def snapshot(self) -> dict:
        v = sorted(self.ms)
        return {**self.tracker.snapshot(), "hands_in_last_pass": len(self.last_hands), "every_n_frames": self.every_n,
                "ms_p50": None if not v else round(v[len(v) // 2], 1), "ms_p95": None if not v else round(v[min(len(v) - 1, int(len(v) * 0.95))], 1)}
