"""Who is in view and who is at the desk, settled over several frames.

States: ``unknown`` (just started, or the camera could not see), ``empty``, ``in_view`` (someone visible
but not at the desk) and ``at_desk``. A person counts as at the desk when enough of their box lies in the
desk zone and the box is tall enough to be close to the camera, so someone walking past behind the seat
stays ``in_view``. Being at the desk is not an alert by itself: it is one observation that the database
correlates with what the Mac reports.

Debounce: entering needs ``confirm_hits`` of the last ``confirm_window`` frames spanning ``confirm_min_s``;
leaving needs ``leave_s`` without a hit, so a detector that drops a frame does not make anyone leave and
come back. While the camera cannot see, nothing is concluded at all: an obstructed lens is not an empty room.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .geometry import ZoneMap, box_fraction_in
from .observations import Observation, PersonDet

UNKNOWN, EMPTY, IN_VIEW, AT_DESK = "unknown", "empty", "in_view", "at_desk"


@dataclass
class PresenceConfig:
    min_score: float = 0.45  # a person box below this is ignored
    desk_overlap: float = 0.3  # share of the person's box inside the desk zone
    confirm_hits: int = 3
    confirm_window: int = 5
    confirm_min_s: float = 0.25
    leave_s: float = 1.5
    clear_s: float = 3.0
    approach_window_s: float = 1.0
    approach_growth: float = 1.25  # box area ratio over the window that counts as coming closer
    approach_overlap_gain: float = 0.15  # or this much more of the box inside the desk zone


class PresenceTracker:
    def __init__(self, zones: ZoneMap, cfg: PresenceConfig | None = None):
        self.zones = zones
        self.cfg = cfg or PresenceConfig()
        self.state = UNKNOWN
        self._reset(None)

    def _reset(self, t: float | None) -> None:
        n = self.cfg.confirm_window
        self._since = t  # when this tracker last started looking
        self._view_hits: deque = deque(maxlen=n)
        self._desk_hits: deque = deque(maxlen=n)
        self._desk_scores: deque = deque(maxlen=n)
        self._view_scores: deque = deque(maxlen=n)
        self._last_view: float | None = None
        self._last_desk: float | None = None
        self._view_since: float | None = None
        self._desk_since: float | None = None
        self._entered: float | None = None
        self._track: deque = deque()
        self._approached = False
        self.persons_in_view = 0

    def suspend(self) -> None:
        """The camera cannot see: forget what was in view, conclude nothing."""
        if self.state != UNKNOWN:
            self.state = UNKNOWN
        self._reset(None)

    def at_desk(self, p: PersonDet) -> bool:
        desk = self.zones.get("desk")
        return bool(desk) and p.box.h >= self.zones.desk_min_height and box_fraction_in(p.box, desk) >= self.cfg.desk_overlap

    def update(self, t: float, persons: list[PersonDet], desk_evidence: bool = False) -> list[Observation]:
        """``desk_evidence``: a face or hand seen inside the desk zone, for close-ups the person box misses."""
        cfg = self.cfg
        if self._since is None:
            self._since = t
        people = [p for p in persons if p.score >= cfg.min_score]
        desk_people = [p for p in people if self.at_desk(p)]
        in_view = bool(people) or desk_evidence
        at_desk = bool(desk_people) or desk_evidence
        self.persons_in_view = len(people)
        self._view_hits.append(in_view)
        self._desk_hits.append(at_desk)
        if in_view:
            if self._last_view is None or t - self._last_view > cfg.leave_s:
                self._view_since = t
            self._last_view = t
            self._view_scores.append(max((p.score for p in people), default=cfg.min_score))
        if at_desk:
            if self._last_desk is None or t - self._last_desk > cfg.leave_s:
                self._desk_since = t
            self._last_desk = t
            self._desk_scores.append(max((p.score for p in desk_people), default=cfg.min_score))

        out: list[Observation] = []
        if self.state != AT_DESK and self._settled(self._desk_hits, self._desk_since, t):
            self.state = AT_DESK
            self._entered = self._desk_since
            out.append(Observation(
                "person_in_zone", t, _mean(self._desk_scores), zone="desk", started=self._desk_since,
                dwell_ms=_ms(t - self._desk_since), persons_in_view=len(people),
            ))
        elif self.state == AT_DESK and t - self._last_desk >= cfg.leave_s:
            self.state = IN_VIEW
            self._approached = True  # they just left the desk; walking away is not an approach
            self._track.clear()
            out.append(Observation(
                "person_left_zone", t, None, zone="desk", started=self._entered,
                dwell_ms=_ms(self._last_desk - self._entered), persons_in_view=len(people),
            ))
            self._entered = None

        if self.state in (UNKNOWN, EMPTY) and not at_desk and self._settled(self._view_hits, self._view_since, t):
            self.state = IN_VIEW
            self._approached = False
            self._track.clear()
            out.append(Observation("person_in_view", t, _mean(self._view_scores), started=self._view_since, persons_in_view=len(people)))

        if self.state == IN_VIEW and people and not desk_people:
            if not self._approached and self._approaching(t, people):
                self._approached = True
                out.append(Observation("person_approaching", t, _mean(self._view_scores), zone="desk", started=self._view_since, persons_in_view=len(people)))

        quiet_since = self._last_view if self._last_view is not None else self._since
        if self.state in (UNKNOWN, IN_VIEW) and t - quiet_since >= cfg.clear_s:
            self.state = EMPTY
            self._approached = False
            self._track.clear()
            out.append(Observation("zone_clear", t, None, persons_in_view=0, detail=f"no person in view for {cfg.clear_s:g} s"))
        return out

    def _settled(self, hits: deque, since: float | None, t: float) -> bool:
        return since is not None and sum(hits) >= self.cfg.confirm_hits and t - since >= self.cfg.confirm_min_s

    def _approaching(self, t: float, people: list[PersonDet]) -> bool:
        desk = self.zones.get("desk") or []
        best = max(people, key=lambda p: (box_fraction_in(p.box, desk), p.box.area))
        self._track.append((t, best.box.area, box_fraction_in(best.box, desk)))
        while self._track and t - self._track[0][0] > self.cfg.approach_window_s:
            self._track.popleft()
        if len(self._track) < 3 or t - self._track[0][0] < self.cfg.approach_window_s / 2:
            return False
        _, area0, overlap0 = self._track[0]
        _, area1, overlap1 = self._track[-1]
        return (area0 > 0 and area1 / area0 >= self.cfg.approach_growth) or overlap1 - overlap0 >= self.cfg.approach_overlap_gain

    def snapshot(self) -> dict:
        return {"state": self.state, "persons_in_view": self.persons_in_view}


def _mean(values) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))
