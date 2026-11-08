"""Whether the AIRLOCK camera can see, and the events when that changes.

``live``         frames arrive and show something
``obstructed``   frames arrive but are dark or flat (a hand, tape or a lid over the lens, or a black room)
``offline``      the camera stopped delivering frames after it had worked
``unavailable``  the camera never opened

Only ``live`` lets the presence and interaction trackers conclude anything. The Mac's USB and app
monitoring do not depend on any of this.
"""

from __future__ import annotations

from dataclasses import dataclass

from .observations import FrameStats, Observation

STARTING, LIVE, OBSTRUCTED, OFFLINE, UNAVAILABLE = "starting", "live", "obstructed", "offline", "unavailable"


@dataclass
class CameraHealthConfig:
    dark_mean: float = 14.0  # grey level (0-255) below which a frame shows nothing
    flat_std: float = 5.0  # spread below which a frame is one flat colour
    obstruct_s: float = 1.5
    restore_s: float = 1.0
    unavailable_s: float = 6.0  # never got a frame for this long after starting


class CameraHealth:
    def __init__(self, cfg: CameraHealthConfig | None = None):
        self.cfg = cfg or CameraHealthConfig()
        self.state = STARTING
        self.reason = "starting"
        self._started: float | None = None
        self._ever_live = False
        self._bad_since: float | None = None
        self._good_since: float | None = None
        self._degraded_since: float | None = None
        self.last_stats: FrameStats | None = None

    @property
    def can_see(self) -> bool:
        return self.state == LIVE

    def start(self, t: float) -> None:
        self._started = t

    def blind(self, stats: FrameStats) -> bool:
        return stats.mean < self.cfg.dark_mean or stats.std < self.cfg.flat_std

    def on_frame(self, t: float, stats: FrameStats) -> list[Observation]:
        self.last_stats = stats
        out: list[Observation] = []
        if self.blind(stats):
            self._good_since = None
            self._bad_since = self._bad_since if self._bad_since is not None else t
            if self.state != OBSTRUCTED and t - self._bad_since >= self.cfg.obstruct_s:
                why = "dark" if stats.mean < self.cfg.dark_mean else "one flat colour"
                out += self._to(OBSTRUCTED, t, f"frames arrive but are {why} (grey mean {stats.mean:.0f}, spread {stats.std:.0f}) for {t - self._bad_since:.1f} s")
            return out
        self._bad_since = None
        self._good_since = self._good_since if self._good_since is not None else t
        if self.state == STARTING:
            out += self._to(LIVE, t, "frames arrive and show the scene")
        elif self.state in (OBSTRUCTED, OFFLINE, UNAVAILABLE) and t - self._good_since >= self.cfg.restore_s:
            out += self._to(LIVE, t, "the view is back")
        return out

    def on_source(self, t: float, source_state: str, reason: str) -> list[Observation]:
        """The capture thread's own state: live, opening, reconnecting or offline."""
        if source_state == "live":
            return []  # a frame decides whether the view is usable
        self._good_since = None
        if self._ever_live and self.state not in (OFFLINE,):
            return self._to(OFFLINE, t, reason)
        return []

    def tick(self, t: float) -> list[Observation]:
        if not self._ever_live and self.state == STARTING and self._started is not None and t - self._started >= self.cfg.unavailable_s:
            return self._to(UNAVAILABLE, t, f"no frame from the camera {self.cfg.unavailable_s:g} s after starting")
        return []

    def _to(self, state: str, t: float, reason: str) -> list[Observation]:
        before = self.state
        self.state, self.reason = state, reason
        if state == LIVE:
            self._ever_live = True
            if before == STARTING:
                return []
            down = None if self._degraded_since is None else int(round((t - self._degraded_since) * 1000))
            self._degraded_since = None
            return [Observation("camera_restored", t, None, dwell_ms=down, detail=f"was {before}: {reason}")]
        if self._degraded_since is None:
            self._degraded_since = t
        kind = {OBSTRUCTED: "camera_obstructed", OFFLINE: "camera_offline", UNAVAILABLE: "camera_unavailable"}[state]
        return [Observation(kind, t, None, detail=reason[:240])]

    def snapshot(self) -> dict:
        s = self.last_stats
        return {"state": self.state, "reason": self.reason, "grey_mean": None if s is None else round(s.mean, 1), "grey_spread": None if s is None else round(s.std, 1)}
