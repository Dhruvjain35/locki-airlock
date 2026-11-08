"""Per-frame observations -> a small number of truthful, timestamped events.

Rules (PRD E2):
  * Evidence guard: a kind is emitted only if the provider HAS the capability it requires
    (contract.KIND_CAPABILITY). A motion provider can never produce person or face events, and a
    provider can't produce runtime kinds such as camera_unavailable.
  * Presence (person_in_zone / person_left_zone): enter after `enter_frames` consecutive frames;
    leave after `exit_s` with no observation. One event per sustained crossing, never per frame.
  * motion_in_zone: enter-only (background subtraction absorbs a still person, so "motion stopped"
    is not evidence the zone is clear) with a per-zone cooldown.
  * Instant kinds (face / interaction): `instant_frames` consecutive frames, then a cooldown.
  * Camera loss resets state WITHOUT emitting any leave/clear event.
  * Metadata is reduced to the contract allowlist; privacy-forbidden keys are stripped and counted.
  * Global rate limit on new episodes; leave events for already-emitted enters always pass.

Times: enter.observed_at = first frame of the confirmed streak. leave.observed_at = last frame the
person was seen (confirmed exit_s later). captured_mono = the frame that confirmed the decision.

Python 3.6 compatible on purpose.
"""
import math
from typing import Any, Dict, NamedTuple, Optional

from .contract import KIND_CAPABILITY, is_forbidden_key

PRESENCE_EXIT = {"person_in_zone": "person_left_zone"}
ENTER_ONLY = frozenset(("motion_in_zone",))
INSTANT = frozenset(("owner_face_match", "unrecognized_face", "face_uncertain", "keyboard_interaction",
                     "usb_zone_interaction"))
OBSERVATION_LABEL = {"person_in_zone": "presence", "person_left_zone": "absence", "motion_in_zone": "motion",
                     "owner_face_match": "face", "unrecognized_face": "face", "face_uncertain": "face",
                     "keyboard_interaction": "interaction", "usb_zone_interaction": "interaction"}
PROVIDER_METADATA_KEYS = frozenset(("bbox", "zone_overlap"))  # what a provider may pass through


class PendingEvent(NamedTuple):
    kind: str
    confidence: Optional[float]
    zone_id: str
    observed_at_ms: int
    captured_mono: float
    metadata: Dict[str, Any]


class _Streak(object):
    __slots__ = ("count", "first_ms", "first_mono", "last_ms", "last_mono", "confs", "occupied", "md",
                 "cooldown_until", "emitted")

    def __init__(self):
        self.count = 0
        self.first_ms = self.first_mono = self.last_ms = self.last_mono = None
        self.confs = []
        self.occupied = False
        self.md = {}
        self.cooldown_until = -1e18
        self.emitted = False   # an enter event went out for the current episode

    def hit(self, ms, mono, conf, md):
        if self.count == 0:
            self.first_ms, self.first_mono, self.confs = ms, mono, []
        self.count += 1
        self.last_ms, self.last_mono = ms, mono
        self.confs.append(conf)
        self.md = md

    def confidence(self):
        vals = [c for c in self.confs if c is not None]
        if not vals or len(vals) != len(self.confs):
            return None
        return round(sum(vals) / len(vals), 4)


class EventNormalizer(object):
    def __init__(self, provider, zone_ids, enter_frames=3, exit_s=2.0, instant_frames=2, cooldown_s=10.0,
                 max_episodes_per_min=30):
        self.provider = provider
        self.capabilities = frozenset(provider.capabilities)
        self.zone_ids = frozenset(zone_ids)
        self.enter_frames = int(enter_frames)
        self.exit_s = float(exit_s)
        self.instant_frames = int(instant_frames)
        self.cooldown_s = float(cooldown_s)
        self.max_episodes_per_min = int(max_episodes_per_min)
        self.streaks = {}
        self.episode_times = []
        self.counters = {"observations": 0, "capability_violations": 0, "unknown_zone": 0, "bad_confidence": 0,
                         "unknown_kind": 0, "metadata_stripped": 0, "privacy_stripped": 0, "rate_limited": 0,
                         "cooldown_suppressed": 0, "camera_resets": 0, "events": 0}

    # -- guards -------------------------------------------------------------------------------
    def _admit(self, obs):
        need = KIND_CAPABILITY.get(obs.kind)
        if need is None or (obs.kind not in PRESENCE_EXIT and obs.kind not in ENTER_ONLY
                            and obs.kind not in INSTANT):
            self.counters["unknown_kind"] += 1
            return False
        if need not in self.capabilities:
            self.counters["capability_violations"] += 1
            return False
        if obs.zone_id not in self.zone_ids:
            self.counters["unknown_zone"] += 1
            return False
        c = obs.confidence
        if c is not None and (isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c)
                              or not 0.0 <= c <= 1.0):
            self.counters["bad_confidence"] += 1
            return False
        return True

    def _clean_md(self, md):
        out = {}
        for k, v in (md or {}).items():
            if is_forbidden_key(k):
                self.counters["privacy_stripped"] += 1
            elif k in PROVIDER_METADATA_KEYS:
                out[k] = v
            else:
                self.counters["metadata_stripped"] += 1
        return out

    def _rate_ok(self, mono):
        cutoff = mono - 60.0
        self.episode_times = [t for t in self.episode_times if t >= cutoff]
        if len(self.episode_times) >= self.max_episodes_per_min:
            self.counters["rate_limited"] += 1
            return False
        self.episode_times.append(mono)
        return True

    def _event(self, kind, conf, zone_id, observed_ms, mono, md, extra):
        meta = dict(md)
        meta.update(extra)
        meta["zone_id"] = zone_id
        meta["observation"] = OBSERVATION_LABEL[kind]
        meta["provider"] = _slug(self.provider.name)
        meta["provider_version"] = str(self.provider.version)[:64]
        self.counters["events"] += 1
        return PendingEvent(kind, conf, zone_id, int(observed_ms), mono, meta)

    # -- main entry -----------------------------------------------------------------------------
    def process(self, observations, captured_utc_ms, captured_mono):
        events = []
        seen = set()
        for obs in observations:
            self.counters["observations"] += 1
            if not self._admit(obs):
                continue
            key = (obs.zone_id, obs.kind)
            if key in seen:
                continue  # one observation per (zone, kind) per frame
            seen.add(key)
            s = self.streaks.setdefault(key, _Streak())
            s.hit(captured_utc_ms, captured_mono, None if obs.confidence is None else float(obs.confidence),
                  self._clean_md(obs.metadata))
            if obs.kind in PRESENCE_EXIT or obs.kind in ENTER_ONLY:
                if not s.occupied and s.count >= self.enter_frames:
                    if obs.kind in ENTER_ONLY and captured_mono < s.cooldown_until:
                        self.counters["cooldown_suppressed"] += 1
                        s.occupied = True  # stay latched until the motion stops, then re-arm
                        continue
                    if not self._rate_ok(captured_mono):
                        s.occupied = True
                        continue
                    s.occupied = True
                    s.emitted = True
                    s.cooldown_until = captured_mono + self.cooldown_s
                    events.append(self._event(obs.kind, s.confidence(), obs.zone_id, s.first_ms, captured_mono,
                                              s.md, {"frames_confirmed": s.count}))
            else:  # instant
                if s.count >= self.instant_frames and captured_mono >= s.cooldown_until:
                    if self._rate_ok(captured_mono):
                        s.cooldown_until = captured_mono + self.cooldown_s
                        events.append(self._event(obs.kind, s.confidence(), obs.zone_id, s.first_ms,
                                                  captured_mono, s.md, {"frames_confirmed": s.count}))
                    s.count = 0
                elif s.count >= self.instant_frames:
                    self.counters["cooldown_suppressed"] += 1
                    s.count = 0
        # absence handling for every streak not seen in this frame
        for key, s in list(self.streaks.items()):
            if key in seen:
                continue
            zone_id, kind = key
            if kind in INSTANT or not s.occupied:
                s.count = 0
                continue
            if s.last_mono is not None and captured_mono - s.last_mono >= self.exit_s:
                exit_kind = PRESENCE_EXIT.get(kind)
                if exit_kind and s.emitted:
                    dwell = int(max(0, s.last_ms - s.first_ms))
                    md = {k: v for k, v in s.md.items() if k in PROVIDER_METADATA_KEYS}
                    events.append(self._event(exit_kind, None, zone_id, s.last_ms, captured_mono, md,
                                              {"dwell_ms": dwell}))
                s.occupied = False
                s.emitted = False
                s.count = 0
        return events

    def camera_lost(self):
        """No frames = no knowledge. Forget everything; never claim the zone is clear."""
        self.counters["camera_resets"] += 1
        cooldowns = {k: s.cooldown_until for k, s in self.streaks.items()}
        self.streaks = {}
        for k, until in cooldowns.items():  # keep cooldowns so a flapping camera can't flood events
            s = _Streak()
            s.cooldown_until = until
            self.streaks[k] = s

    def occupied_zones(self):
        return sorted(set(z for (z, k), s in self.streaks.items() if s.occupied and k in PRESENCE_EXIT))


def _slug(text):
    out = "".join(ch if (ch.isalnum() or ch in "_.:-") else "_" for ch in str(text).lower())
    return (out or "provider")[:64]


__all__ = ["EventNormalizer", "PendingEvent", "PRESENCE_EXIT", "ENTER_ONLY", "INSTANT"]
