"""Per-frame observations -> a small number of truthful, timestamped events.

Rules (PRD E2):
  * Evidence guard: a kind is emitted only if the provider HAS the capability it requires
    (contract.KIND_CAPABILITY). A motion provider can never produce person or face events, and a
    provider can't produce runtime kinds such as camera_offline / camera_online.
  * Presence (person_in_zone / person_cleared): enter after `enter_frames` consecutive frames;
    clear after `exit_s` with no observation. One enter per crossing plus periodic re-asserts while
    occupied, so a device event anywhere in the visit falls inside the module's 10 s correlation window
    (Sid's policy.ts correlates(): an unknown USB pairs with a person_in_zone only if |delta observedAt|
    <= 10 s). A re-assert is a new person_in_zone (new event_id, next seq) sent every `reassert_s` of
    observed time while the person is still seen; reassert_s=0 turns it off. Re-asserts are not new
    episodes: they bypass the episode rate limit (they are already bounded to one per reassert_s per
    zone), never follow a rate-limited enter, stop the moment the clear is confirmed, and are flagged
    PendingEvent.reassert so the runtime never re-alarms or re-pages on them. Never one per frame.
  * motion_in_zone: enter-only (background subtraction absorbs a still person, so "motion stopped"
    is not evidence the zone is clear) with a per-zone cooldown.
  * Instant kinds (face / interaction): `instant_frames` consecutive frames, then a cooldown.
  * Camera loss resets state WITHOUT emitting any leave/clear event.
  * Metadata is reduced to the contract allowlist; privacy-forbidden keys are stripped and counted.
  * Global rate limit on new episodes; clear events for already-emitted enters always pass.

Times: enter.observed_at = first frame of the confirmed streak. re-assert.observed_at = the latest frame
the person was seen (the frame that triggered it). clear.observed_at = last frame the person was seen
(confirmed exit_s later). captured_mono = the frame that confirmed the decision.

Wire kind for the clear is person_cleared: the deployed module refuses person_left_zone for role jetson
(contract/locki-w91bw.live.json) and its source permits person_cleared, which Dhruv's M4 maps to its own
clear handling (spacetime_adapter.py REMOTE_CAMERA_KINDS). The local audit labels it observation=absence.

Python 3.6 compatible on purpose.
"""
import math
from typing import Any, Dict, NamedTuple, Optional

from .contract import KIND_CAPABILITY, is_forbidden_key

PRESENCE_EXIT = {"person_in_zone": "person_cleared"}
ENTER_ONLY = frozenset(("motion_in_zone",))
INSTANT = frozenset(("owner_face_match", "unrecognized_face", "face_uncertain", "keyboard_interaction",
                     "usb_zone_interaction"))
OBSERVATION_LABEL = {"person_in_zone": "presence", "person_cleared": "absence", "motion_in_zone": "motion",
                     "owner_face_match": "face", "unrecognized_face": "face", "face_uncertain": "face",
                     "keyboard_interaction": "interaction", "usb_zone_interaction": "interaction"}
PROVIDER_METADATA_KEYS = frozenset(("bbox", "zone_overlap"))  # what a provider may pass through
CORRELATION_WINDOW_S = 10.0   # Sid's module: CORRELATION_WINDOW_MS = 10_000 (spacetime/src/policy.ts)
DEFAULT_REASSERT_S = 5.0


MIN_REASSERT_S = 1.0


def check_reassert_s(value):
    """0 = off; otherwise at least 1 s (a re-assert per frame would flood the module's 64 events/s per device
    limit) and below the module's 10 s correlation window."""
    v = float(value)
    if not (v == 0.0 or MIN_REASSERT_S <= v < CORRELATION_WINDOW_S):
        raise ValueError("presence re-assert interval must be 0 (off) or at least %g s and below %g s, got %r"
                         % (MIN_REASSERT_S, CORRELATION_WINDOW_S, value))
    return v


class PendingEvent(NamedTuple):
    kind: str
    confidence: Optional[float]
    zone_id: str
    observed_at_ms: int
    captured_mono: float
    metadata: Dict[str, Any]
    reassert: bool          # LOCAL ONLY: a periodic re-assert of an already-announced visit. Never on the wire.


class _Streak(object):
    __slots__ = ("count", "first_ms", "first_mono", "last_ms", "last_mono", "conf_sum", "conf_n", "conf_missing",
                 "occupied", "md", "cooldown_until", "emitted", "asserted_mono")

    def __init__(self):
        self.count = 0
        self.first_ms = self.first_mono = self.last_ms = self.last_mono = None
        self.conf_sum, self.conf_n, self.conf_missing = 0.0, 0, False
        self.occupied = False
        self.md = {}
        self.cooldown_until = -1e18
        self.emitted = False   # an enter event went out for the current episode
        self.asserted_mono = None   # frame time of the last person_in_zone announced for this episode

    def hit(self, ms, mono, conf, md):
        if self.count == 0:
            self.first_ms, self.first_mono = ms, mono
            self.conf_sum, self.conf_n, self.conf_missing = 0.0, 0, False
        self.count += 1
        self.last_ms, self.last_mono = ms, mono
        # Running mean, not a list: a re-asserted visit can last hours and confidence() runs every re-assert.
        if conf is None:
            self.conf_missing = True
        else:
            self.conf_sum += conf
            self.conf_n += 1
        self.md = md

    def confidence(self):
        if not self.conf_n or self.conf_missing:
            return None
        return round(self.conf_sum / self.conf_n, 4)


class EventNormalizer(object):
    def __init__(self, provider, zone_ids, enter_frames=3, exit_s=2.0, instant_frames=2, cooldown_s=10.0,
                 max_episodes_per_min=30, reassert_s=DEFAULT_REASSERT_S):
        self.provider = provider
        self.capabilities = frozenset(provider.capabilities)
        self.zone_ids = frozenset(zone_ids)
        self.enter_frames = int(enter_frames)
        self.exit_s = float(exit_s)
        self.instant_frames = int(instant_frames)
        self.cooldown_s = float(cooldown_s)
        self.max_episodes_per_min = int(max_episodes_per_min)
        self.reassert_s = check_reassert_s(reassert_s)
        self.streaks = {}
        self.episode_times = []
        self.counters = {"observations": 0, "capability_violations": 0, "unknown_zone": 0, "bad_confidence": 0,
                         "unknown_kind": 0, "metadata_stripped": 0, "privacy_stripped": 0, "rate_limited": 0,
                         "cooldown_suppressed": 0, "camera_resets": 0, "events": 0, "reasserts": 0}

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

    def _event(self, kind, conf, zone_id, observed_ms, mono, md, extra, reassert=False):
        meta = dict(md)
        meta.update(extra)
        meta["zone_id"] = zone_id
        meta["observation"] = OBSERVATION_LABEL[kind]
        meta["provider"] = _slug(self.provider.name)
        meta["provider_version"] = str(self.provider.version)[:64]
        self.counters["events"] += 1
        return PendingEvent(kind, conf, zone_id, int(observed_ms), mono, meta, bool(reassert))

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
                    s.asserted_mono = s.first_mono   # enter.observed_at is the streak's first frame
                    s.cooldown_until = captured_mono + self.cooldown_s
                    events.append(self._event(obs.kind, s.confidence(), obs.zone_id, s.first_ms, captured_mono,
                                              s.md, {"frames_confirmed": s.count}))
                elif (self.reassert_s > 0 and obs.kind in PRESENCE_EXIT and s.occupied and s.emitted
                      and captured_mono - s.asserted_mono >= self.reassert_s):
                    # Still seen, enter already announced: re-assert with THIS frame's time so the module's
                    # 10 s window always has a recent person_in_zone. Not a new episode: no rate-limit slot.
                    s.asserted_mono = captured_mono
                    self.counters["reasserts"] += 1
                    events.append(self._event(obs.kind, s.confidence(), obs.zone_id, s.last_ms, captured_mono,
                                              s.md, {"frames_confirmed": s.count,
                                                     "dwell_ms": int(max(0, s.last_ms - s.first_ms))},
                                              reassert=True))
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


__all__ = ["EventNormalizer", "PendingEvent", "PRESENCE_EXIT", "ENTER_ONLY", "INSTANT", "DEFAULT_REASSERT_S",
           "check_reassert_s"]
