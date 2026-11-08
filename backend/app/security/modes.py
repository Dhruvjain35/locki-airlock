"""LOCKI's security mode, decided from what the desk camera and the Mac actually observed.

NORMAL          disarmed, or armed with nobody near the desk
WATCHFUL        armed, and someone near the desk is not matched to the owner (unrecognized, uncertain, or no face yet)
DEFENSIVE       armed, and someone not matched to the owner used the keyboard, the laptop or a protected device on this visit
CRITICAL        armed, and the Mac's own watcher reported a real unknown USB device, or the database reported its correlated incident
OWNER_RETURNED  armed, and the enrolled owner's face matched on this visit. The tone is normal again; the workstation stays
                armed until an authenticated disarm

Deterministic rules over recorded events; no model is asked. Identity belongs to a visit: one stay at the desk, from
person_in_zone to person_left_zone. An owner who looks away is still the owner, and someone who sits down after the
owner left has not been matched. A face match never disarms anything and never removes a USB warning. Once CRITICAL,
only the owner's face matching after the incident (or the hold ending, or the module resolving it) leaves it. A face
that cannot be compared is never assumed to be the owner or an intruder. Nothing here calls anyone a hacker; the
modes describe what was seen, not who someone is or what they meant. SpacetimeDB's module stays the authority on
incidents: CRITICAL only follows its correlated incident or the Mac's own USB fact.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .contracts import SecurityEventV1


class SecurityMode(StrEnum):
    NORMAL = "NORMAL"
    WATCHFUL = "WATCHFUL"
    DEFENSIVE = "DEFENSIVE"
    CRITICAL = "CRITICAL"
    OWNER_RETURNED = "OWNER_RETURNED"


DEFENSIVE_HOLD_S = 300.0  # an interaction keeps this visit DEFENSIVE (while that person stays) for at most this long
CRITICAL_HOLD_S = 60.0  # an unknown USB device keeps the mode CRITICAL this long (the incident stays open in the database)
INTERACTIONS = {"keyboard_interaction": "keyboard", "laptop_interaction": "laptop", "protected_device_interaction": "protected_device"}
IDENTITY = {"owner_face_match": "owner", "unrecognized_face": "unrecognized", "face_uncertain": "uncertain", "no_face_visible": "uncertain"}


@dataclass
class VisualState:
    """What the camera and the Mac say right now. Cheap to read on every turn: no model, no image."""

    armed_status: bool = False
    session_id: str | None = None
    person_present: bool = False
    at_desk: bool = False
    visit: int = 0  # increases each time someone arrives at the desk
    person_track_id: int | None = None
    face_identity_state: str | None = None  # owner | unrecognized | uncertain; None when nobody is present
    identity_confidence: float | None = None  # how settled the vote was, not a probability of identity
    identity_detail: str | None = None
    keyboard_interaction: bool = False
    laptop_interaction: bool = False
    protected_device_interaction: bool = False
    usb_zone_interaction: bool = False
    last_seen_timestamp: int | None = None  # epoch ms of the last person observation
    camera_health: str = "off"  # off | live | obstructed | offline | unavailable
    current_security_mode: SecurityMode = SecurityMode.NORMAL
    previous_mode: SecurityMode | None = None
    mode_since: int = 0  # epoch ms
    mode_reason: str = "disarmed"
    evidence_ids: list[str] = field(default_factory=list)
    updated_at: int = 0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["current_security_mode"] = self.current_security_mode.value
        d["previous_mode"] = None if self.previous_mode is None else self.previous_mode.value
        d["evidence_ids"] = list(self.evidence_ids)
        return d


@dataclass
class _Face:
    identity: str
    confidence: float | None
    detail: str | None
    event_id: str
    visit: int
    at: float


class ModeEngine:
    def __init__(self, on_change: Callable[[SecurityMode, SecurityMode, str, list[str], VisualState], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.on_change = on_change
        self._clock, self._wall = clock, wall
        self.state = VisualState(mode_since=int(wall() * 1000), updated_at=int(wall() * 1000))
        self._faces: dict[int, _Face] = {}  # face track -> its latest identity
        self._visit_owner_at: float | None = None  # when the owner's face matched on this visit
        self._interactions: dict[str, tuple[float, str, bool]] = {}  # name -> (time, event id, done while only the owner was there)
        self._usb_zone: tuple[float, str] | None = None
        self._critical: tuple[float, str, list[str]] | None = None
        self.transitions: list[dict] = []

    # ------------------------------------------------------------------ inputs
    def set_armed(self, armed: bool, session_id: str | None = None) -> None:
        s = self.state
        if armed == s.armed_status and session_id == s.session_id:
            return
        s.armed_status, s.session_id = armed, session_id
        self._clear_people()
        self._critical = None
        if not armed:
            s.camera_health = "off"
        self._evaluate("armed" if armed else "disarmed", [], self._clock())

    def on_camera(self, event: SecurityEventV1) -> None:
        kind, s, now = event.kind.value, self.state, self._clock()  # one clock reading for the whole event
        meta = event.metadata
        if kind == "person_in_zone":
            if not s.at_desk:
                self._new_visit()
            s.person_present = s.at_desk = True
            s.last_seen_timestamp = event.observed_at
        elif kind in ("person_in_view", "person_approaching"):
            s.person_present = True
            s.last_seen_timestamp = event.observed_at
        elif kind == "person_left_zone":
            self._end_visit()  # they may still be in view, but this stay at the desk is over
        elif kind == "zone_clear":
            self._clear_people()
        elif kind in IDENTITY:
            track = meta.get("track_id")
            key = int(track) if isinstance(track, int) else -1
            if key >= 0:
                self._faces.pop(-1, None)  # a real face supersedes "someone, no face"
            self._faces[key] = _Face(IDENTITY[kind], event.confidence, meta.get("detail"), event.event_id, s.visit, now)
            if kind == "owner_face_match":
                self._visit_owner_at = now
            s.person_present = True
            s.last_seen_timestamp = event.observed_at
        elif kind in INTERACTIONS:
            s.person_present = True  # a hand on the keys is a person, even if the body is out of view
            by_owner = self._owner_present() and not self._non_owner_present()
            self._interactions[INTERACTIONS[kind]] = (now, event.event_id, by_owner)
            s.last_seen_timestamp = event.observed_at
        elif kind in ("usb_zone_interaction", "hand_near_usb_zone"):
            s.person_present = True
            if kind == "usb_zone_interaction":
                self._usb_zone = (now, event.event_id)
            s.last_seen_timestamp = event.observed_at
        elif kind in ("camera_obstructed", "camera_offline", "camera_unavailable"):
            s.camera_health = {"camera_obstructed": "obstructed", "camera_offline": "offline", "camera_unavailable": "unavailable"}[kind]
        elif kind == "camera_restored":
            s.camera_health = "live"
        if kind not in ("camera_obstructed", "camera_offline", "camera_unavailable") and s.camera_health in ("off", "unavailable") and s.armed_status:
            s.camera_health = "live"  # it is delivering observations
        self._evaluate(kind, [event.event_id], now)

    def camera_started(self, ok: bool = True) -> None:
        if self.state.armed_status:
            self.state.camera_health = "live" if ok else "unavailable"
            self._touch(self._clock())

    def on_usb(self, event: SecurityEventV1, *, unknown: bool) -> None:
        """A USB fact from the Mac's own watcher. Only an unknown device while armed is CRITICAL, whoever is in view."""
        if not unknown or not self.state.armed_status:
            return
        now = self._clock()
        evidence = [event.event_id] + self._physical_evidence()
        self._critical = (now, "unknown_usb_with_person" if self._non_owner_present() else "unknown_usb", evidence)
        self._evaluate(self._critical[1], evidence, now)

    def on_incident(self, incident_id: str, *, correlated: bool, resolved: bool, evidence: list[str]) -> None:
        """SpacetimeDB's incident. Its correlated USB-and-person case is CRITICAL; resolving it ends that."""
        if not self.state.armed_status:
            return
        now = self._clock()
        if resolved:
            if self._critical is not None and incident_id in self._critical[2]:
                self._critical = None
                self._evaluate("incident_resolved", [incident_id], now)
            return
        if correlated:
            self._critical = (now, "correlated_incident", [incident_id, *evidence])
            self._evaluate("correlated_incident", [incident_id, *evidence], now)

    def tick(self) -> None:
        self._evaluate("time", [], self._clock())

    def interaction_event_id(self) -> str | None:
        """The most recent interaction by someone not matched to the owner: what a DEFENSIVE alert cites."""
        hits = [v for v in self._interactions.values() if not v[2]]
        return max(hits)[1] if hits else None

    # ------------------------------------------------------------------ visits
    def _new_visit(self) -> None:
        self.state.visit += 1
        self._visit_owner_at = None
        self._interactions.clear()
        self._usb_zone = None

    def _end_visit(self) -> None:
        self.state.at_desk = False
        self._visit_owner_at = None
        self._interactions.clear()
        self._usb_zone = None

    def _clear_people(self) -> None:
        s = self.state
        s.person_present = s.at_desk = False
        self._faces.clear()
        self._visit_owner_at = None
        self._interactions.clear()
        self._usb_zone = None

    # ------------------------------------------------------------------ rules
    def _current(self) -> list[_Face]:
        return [f for f in self._faces.values() if f.visit == self.state.visit]

    def _owner_tracks_now(self) -> bool:
        return any(f.identity == "owner" for f in self._current())

    def _owner_present(self) -> bool:
        return self.state.person_present and (self._visit_owner_at is not None or self._owner_tracks_now())

    def _non_owner_present(self) -> bool:
        """Someone whose identity is not established as the owner on this visit, or any face that did not match."""
        if not self.state.person_present:
            return False
        if any(f.identity == "unrecognized" for f in self._current()):
            return True
        return self._visit_owner_at is None and not self._owner_tracks_now()

    def _interacting(self, now: float) -> bool:
        return any(not by_owner and now - t <= DEFENSIVE_HOLD_S for t, _e, by_owner in self._interactions.values())

    def _physical_evidence(self) -> list[str]:
        out = [f.event_id for f in self._current()]
        out += [e for _t, e, by_owner in self._interactions.values() if not by_owner]
        if self._usb_zone is not None:
            out.append(self._usb_zone[1])
        return out

    def _decide(self, now: float) -> SecurityMode:
        if not self.state.armed_status:
            return SecurityMode.NORMAL
        owner = self._owner_present() and not self._non_owner_present()
        if self._critical is not None and now - self._critical[0] <= CRITICAL_HOLD_S:
            # A face that matched before the incident does not mask it; the owner coming back after it does.
            if owner and self._visit_owner_at is not None and self._visit_owner_at > self._critical[0]:
                return SecurityMode.OWNER_RETURNED
            return SecurityMode.CRITICAL
        if owner:
            return SecurityMode.OWNER_RETURNED  # normal tone; the session and any incident stay as they are
        if self._non_owner_present() and self._interacting(now):
            return SecurityMode.DEFENSIVE
        if self._non_owner_present():
            return SecurityMode.WATCHFUL
        return SecurityMode.NORMAL

    def _touch(self, now: float) -> None:
        s = self.state
        keyed = {id(f): k for k, f in self._faces.items()}
        current = self._current()
        unrecognized = [f for f in current if f.identity == "unrecognized"]
        owners = [f for f in current if f.identity == "owner"]
        others = [f for f in current if f.identity != "owner"]

        def show(f: _Face) -> None:
            k = keyed.get(id(f), -1)
            s.person_track_id = None if k < 0 else k
            s.face_identity_state, s.identity_confidence, s.identity_detail = f.identity, f.confidence, f.detail

        if not s.person_present:
            s.person_track_id = s.face_identity_state = s.identity_confidence = s.identity_detail = None
        elif unrecognized:  # the person who matters most for security is shown first
            show(max(unrecognized, key=lambda f: f.at))
        elif owners:
            show(max(owners, key=lambda f: f.at))
        elif self._visit_owner_at is not None:
            s.person_track_id, s.face_identity_state, s.identity_confidence, s.identity_detail = None, "owner", None, "matched earlier on this visit"
        elif others:
            show(max(others, key=lambda f: f.at))
        else:
            s.person_track_id, s.face_identity_state, s.identity_confidence, s.identity_detail = None, "uncertain", None, "no face compared yet"
        for name in ("keyboard", "laptop", "protected_device"):
            hit = self._interactions.get(name)
            setattr(s, f"{name}_interaction", hit is not None and not hit[2] and now - hit[0] <= DEFENSIVE_HOLD_S)
        s.usb_zone_interaction = self._usb_zone is not None and now - self._usb_zone[0] <= DEFENSIVE_HOLD_S
        s.updated_at = int(self._wall() * 1000)

    def _evaluate(self, reason: str, evidence: list[str], now: float) -> None:
        self._touch(now)
        new = self._decide(now)
        s = self.state
        if new == s.current_security_mode:
            return
        prev = s.current_security_mode
        s.previous_mode, s.current_security_mode = prev, new
        s.mode_since, s.mode_reason = int(self._wall() * 1000), reason
        cited = self.interaction_event_id()
        if new == SecurityMode.CRITICAL and self._critical:
            s.evidence_ids = list(self._critical[2])[:20]
        elif new == SecurityMode.DEFENSIVE and cited:
            s.evidence_ids = [cited, *[e for e in self._physical_evidence() if e != cited]][:20]
        else:
            s.evidence_ids = list(evidence or self._physical_evidence())[:20]
        self.transitions.append({"from": prev.value, "to": new.value, "reason": reason, "at": s.mode_since, "evidence_ids": list(s.evidence_ids)})
        del self.transitions[:-50]
        if self.on_change is not None:
            self.on_change(prev, new, reason, list(s.evidence_ids), s)
