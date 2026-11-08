"""The AIRLOCK camera pipeline: frames -> detection -> trackers -> timestamped events.

One capture thread (LOCKED IN's ``CameraSource``: a camera index, an MJPEG URL or a recorded file) keeps
only the newest frame; one processing thread runs the detector on it and the trackers on the result.
Events leave through ``sink(VisionEvent)``, which on the Mac builds a SecurityEventV1 and hands it to the
security publisher. Frames stay in memory; nothing is written to disk or sent anywhere.

Timing: ``capture`` is when OpenCV handed the frame over. The camera's own delay before that (exposure
and USB transfer, typically tens of ms) is not measured, so glass-to-event is somewhat longer than
the ``capture_to_event_ms`` reported here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .camera_health import CameraHealth, CameraHealthConfig
from .geometry import ZoneMap
from .observations import Observation
from .presence import PresenceConfig, PresenceTracker

log = logging.getLogger(__name__)

SOFT_KINDS_MIN_GAP_S = {"person_in_view": 10.0, "person_approaching": 10.0, "zone_clear": 5.0}


@dataclass(frozen=True)
class VisionEvent:
    kind: str
    observed_at_ms: int
    confidence: float | None
    metadata: dict  # VisionMetadata fields, without armed and session_id (the sink knows those)
    capture_mono: float
    emitted_mono: float


class EventLimiter:
    """Soft observations at most once per ``min_gap_s``, and a token bucket over everything, so a
    flickering scene can never flood the database or the speaker. Suppressed events are counted."""

    def __init__(self, min_gap_s: dict[str, float] | None = None, burst: int = 6, per_s: float = 2.0, clock=time.monotonic):
        self.min_gap_s = dict(SOFT_KINDS_MIN_GAP_S if min_gap_s is None else min_gap_s)
        self.burst, self.per_s, self._clock = burst, per_s, clock
        self._tokens = float(burst)
        self._refilled = clock()
        self._last: dict[str, float] = {}
        self.suppressed: Counter = Counter()

    def allow(self, kind: str, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        self._tokens = min(self.burst, self._tokens + (now - self._refilled) * self.per_s)
        self._refilled = now
        gap = self.min_gap_s.get(kind)
        if gap is not None and kind in self._last and now - self._last[kind] < gap:
            self.suppressed[kind] += 1
            return False
        if self._tokens < 1.0:
            self.suppressed[kind] += 1
            return False
        self._tokens -= 1.0
        self._last[kind] = now
        return True


def _pct(values, q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(len(v) * q))], 1)


@dataclass
class _Stats:
    detect_ms: deque = field(default_factory=lambda: deque(maxlen=300))
    total_ms: deque = field(default_factory=lambda: deque(maxlen=300))
    capture_to_result_ms: deque = field(default_factory=lambda: deque(maxlen=300))
    capture_to_event_ms: deque = field(default_factory=lambda: deque(maxlen=100))
    processed: int = 0
    fps: float = 0.0
    last_t: float | None = None


class AirlockVision:
    def __init__(
        self,
        models_dir: Path,
        sink: Callable[[VisionEvent], None],
        *,
        zones: ZoneMap | None = None,
        camera_label: str = "mac_webcam",
        width: int = 640,
        height: int = 480,
        presence: PresenceConfig | None = None,
        health: CameraHealthConfig | None = None,
        detector_factory: Callable[[], object] | None = None,
        faces: object | None = None,
        hands: object | None = None,
        limiter: EventLimiter | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.models_dir = Path(models_dir)
        self.sink = sink
        self.zones = zones or ZoneMap()
        self.camera_label = camera_label[:60]
        self.width, self.height = width, height
        self._presence_cfg = presence or PresenceConfig()
        self._health_cfg = health or CameraHealthConfig()
        self._detector_factory = detector_factory or self._default_detector
        self.faces = faces  # a FaceStage, or None to watch people only
        self.hands = hands  # a HandStage, or None to skip zone interaction
        # Enrollment runs on this thread, on the same frames: one camera consumer, never two.
        self.enroller = None  # a FaceEnrollment while the owner is enrolling
        self.enroll_progress: dict | None = None
        self.on_enrolled: Callable[[object], None] | None = None
        self.stream_clients = 0
        self._jpeg: tuple[int, bytes] | None = None
        self.status_provider: Callable[[], dict] | None = None
        self.last_persons: list = []
        self.first_person_mono: float | None = None
        self.live_since: float | None = None
        self._reobserve = False  # set from another thread; this thread acts on it at the next frame
        self.limiter = limiter or EventLimiter(clock=clock)
        self._clock, self._wall = clock, wall
        self._mono_to_wall = wall() - clock()
        self.detector = None
        self.camera = None
        self.presence = PresenceTracker(self.zones, self._presence_cfg)
        self.health = CameraHealth(self._health_cfg)
        self.stats = _Stats()
        self.emitted: Counter = Counter()
        self.sink_errors = 0
        self.last_events: deque = deque(maxlen=20)
        self.error: str | None = None
        self._source_states: deque = deque()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False

    def _default_detector(self):
        from .inference import MediaPipePersonDetector

        return MediaPipePersonDetector(self.models_dir)

    # ------------------------------------------------------------------ control
    def start(self, source: str = "0", *, mirror: bool | None = None, frame_source=None) -> None:
        """``frame_source``: anything with start(), stop(), read_latest(after_id, timeout) and an
        ``on_state_change`` attribute. Defaults to a CameraSource on ``source``."""
        self.stop()
        if self.detector is None:
            self.detector = self._detector_factory()  # raises ModelMissing before the camera turns on
        if frame_source is None:
            from ..webcam import CameraSource

            frame_source = CameraSource(source, self.width, self.height, mirror=mirror, label=self.camera_label)
        self.camera = frame_source
        self.camera.on_state_change = self._on_source_state
        self.presence = PresenceTracker(self.zones, self._presence_cfg)
        self.health = CameraHealth(self._health_cfg)
        self.health.start(self._clock())
        self._mono_to_wall = self._wall() - self._clock()
        self.error = None
        self._running = True
        self.camera.start()
        self._thread = threading.Thread(target=self._run, name="airlock-vision", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self.camera is not None:
            try:
                self.camera.stop()
            except Exception:
                log.debug("camera stop failed", exc_info=True)
            self.camera = None

    def close(self) -> None:
        self.stop()
        if self.hands is not None and hasattr(self.hands, "close"):
            self.hands.close()
        if self.detector is not None:
            try:
                self.detector.close()
            except Exception:
                log.debug("detector close failed", exc_info=True)
            self.detector = None

    @property
    def running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ processing
    def _on_source_state(self, state: str, reason: str) -> None:  # the capture thread
        with self._lock:
            self._source_states.append((self._clock(), state, reason))

    def _run(self) -> None:
        try:
            self._loop()
        except Exception as exc:  # the Mac's USB and app monitoring carry on without the camera
            log.exception("AIRLOCK vision stopped")
            self.error = f"{type(exc).__name__}: {exc}"
            self._publish([Observation("camera_offline", self._clock(), None, detail=f"vision pipeline stopped: {self.error}"[:240])], None)
        finally:
            self._running = False

    def _loop(self) -> None:
        import cv2

        from .inference import frame_stats

        last_id = 0
        while self._running:
            now = self._clock()
            with self._lock:
                pending = list(self._source_states)
                self._source_states.clear()
            for t, state, reason in pending:
                self._publish(self.health.on_source(t, state, reason), None)
            self._publish(self.health.tick(now), None)
            if not self.health.can_see:
                self.presence.suspend()
            item = self.camera.read_latest(last_id, timeout=0.25)
            if item is None:
                continue
            last_id, bgr, captured = item
            if self._reobserve:
                self._reobserve = False
                self.presence.suspend()
                if self.faces is not None:
                    self.faces.reset()
                if self.hands is not None:
                    self.hands.reset()
            t0 = self._clock()
            self._publish(self.health.on_frame(captured, frame_stats(bgr)), captured)
            if self.live_since is None and self.health.can_see:
                self.live_since = captured
            if not self.health.can_see:
                self.presence.suspend()
                if self.faces is not None:
                    self.faces.reset()
                if self.hands is not None:
                    self.hands.reset()
                self._preview(last_id, bgr, blind=f"camera {self.health.state}: {self.health.reason}"[:80])
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            persons = self.detector.detect(rgb, int((captured + self._mono_to_wall) * 1000))
            t1 = self._clock()
            face_obs: list[Observation] = []
            desk_face = False
            if self.faces is not None:
                someone = bool(persons) or self.presence.state in ("in_view", "at_desk")
                face_obs = self.faces.process(captured, bgr, someone)
                desk_face = self.faces.close_face_at_desk(captured, self.zones)
            hand_obs = [] if self.hands is None else self.hands.process(captured, rgb, int((captured + self._mono_to_wall) * 1000))
            self.last_persons = persons
            if persons and self.first_person_mono is None:
                self.first_person_mono = captured
            self._publish(self.presence.update(captured, persons, desk_face), captured)
            self._publish(face_obs, captured)
            self._publish(hand_obs, captured)
            self._enroll(bgr, captured)
            self._record(captured, t0, t1)
            self._preview(last_id, bgr)

    def _enroll(self, bgr, captured: float) -> None:
        enroller = self.enroller
        if enroller is None:
            return
        try:
            self.enroll_progress = enroller.offer(bgr, captured)
        except Exception as exc:  # enrollment failing never stops the camera
            log.exception("enrollment step failed")
            self.enroll_progress = {**(self.enroll_progress or {}), "error": f"{type(exc).__name__}: {exc}"[:160]}
            return
        if self.enroll_progress.get("complete"):
            self.enroller = None
            if self.on_enrolled is not None:
                try:
                    self.on_enrolled(enroller)
                except Exception:
                    log.exception("enrollment callback failed")

    def _preview(self, frame_id: int, bgr, blind: str | None = None) -> None:
        """Only while the dashboard is watching: the frame with what was detected on it."""
        if self.stream_clients <= 0:
            return
        from .preview import render

        try:
            status = self.status_provider() if self.status_provider is not None else {}
        except Exception:
            status = {}
        s = self.stats
        header = {"mode": status.get("mode"), "armed": "ARMED" if status.get("armed") else "disarmed", "fps": f"{s.fps:.0f} fps",
                  "latency": None if not s.detect_ms else f"detect {s.detect_ms[-1]:.0f} ms", "camera": f"camera {self.health.state}"}
        faces = self.faces.display() if self.faces is not None else []
        hands = list(getattr(self.hands, "last_hands", []) or []) if self.hands is not None else []
        try:
            jpg = render(bgr, persons=self.last_persons, faces=faces, hands=hands, zones=self.zones, header=header,
                         enrollment=self.enroll_progress if self.enroller is not None else None, blind=blind)
        except Exception:
            log.debug("preview render failed", exc_info=True)
            return
        self._jpeg = (frame_id, jpg)

    def reobserve(self) -> None:
        """Settle presence, faces and hands again from the next frames (thread-safe: the processing thread does it)."""
        self._reobserve = True

    def latest_jpeg(self) -> tuple[int, bytes] | None:
        return self._jpeg

    def _record(self, captured: float, t0: float, t1: float) -> None:
        s = self.stats
        s.detect_ms.append((t1 - t0) * 1000)
        s.total_ms.append((self._clock() - t0) * 1000)
        s.capture_to_result_ms.append((t1 - captured) * 1000)
        s.processed += 1
        if s.last_t is not None and t1 > s.last_t:
            inst = 1.0 / (t1 - s.last_t)
            s.fps = inst if s.fps == 0 else 0.9 * s.fps + 0.1 * inst
        s.last_t = t1

    def wall_ms(self, mono: float) -> int:
        return int(round((mono + self._mono_to_wall) * 1000))

    def _publish(self, observations: list[Observation], captured: float | None) -> None:
        for obs in observations:
            now = self._clock()
            if not self.limiter.allow(obs.kind, now):
                continue
            settled = captured if captured is not None else obs.at
            meta = {
                "camera": self.camera_label,
                "inference": getattr(self.detector, "inference", "mac_mediapipe"),
                "detector": getattr(self.detector, "name", None),
                "zone": obs.zone,
                "started_at": None if obs.started is None else self.wall_ms(obs.started),
                "dwell_ms": obs.dwell_ms,
                "persons_in_view": obs.persons_in_view,
                "capture_to_event_ms": round(max(0.0, now - settled) * 1000, 1),
                "frame_rate": round(self.stats.fps, 1) if self.stats.fps else None,
                "detail": obs.detail,
                "track_id": obs.track,
            }
            event = VisionEvent(obs.kind, self.wall_ms(obs.at), obs.confidence, {k: v for k, v in meta.items() if v is not None}, settled, now)
            self.stats.capture_to_event_ms.append((now - settled) * 1000)
            self.emitted[obs.kind] += 1
            self.last_events.append({"kind": obs.kind, "observed_at": event.observed_at_ms, "confidence": obs.confidence, "zone": obs.zone})
            try:
                self.sink(event)
            except Exception:
                self.sink_errors += 1
                log.exception("vision event sink failed for %s", obs.kind)

    # ------------------------------------------------------------------ status
    def snapshot(self) -> dict:
        s = self.stats
        cam = None
        if self.camera is not None and hasattr(self.camera, "status"):
            try:
                cam = self.camera.status()
            except Exception:
                cam = None
        rss = None
        try:
            import psutil

            rss = round(psutil.Process().memory_info().rss / 1e6, 1)
        except Exception:
            pass
        return {
            "running": self.running,
            "error": self.error,
            "camera": self.health.snapshot(),
            "capture": None if cam is None else {k: cam.get(k) for k in ("state", "fps", "frames", "dropped", "size", "source", "label")},
            "presence": self.presence.snapshot(),
            "faces": None if self.faces is None else self.faces.snapshot(),
            "hands": None if self.hands is None else self.hands.snapshot(),
            "detector": getattr(self.detector, "name", None),
            "inference": getattr(self.detector, "inference", None),
            "zones": self.zones.to_dict(),
            "stats": {
                "frames_processed": s.processed,
                "processing_fps": round(s.fps, 1),
                "detect_ms_p50": _pct(s.detect_ms, 0.5),
                "detect_ms_p95": _pct(s.detect_ms, 0.95),
                "capture_to_result_ms_p50": _pct(s.capture_to_result_ms, 0.5),
                "capture_to_result_ms_p95": _pct(s.capture_to_result_ms, 0.95),
                "capture_to_event_ms_p50": _pct(s.capture_to_event_ms, 0.5),
                "capture_to_event_ms_p95": _pct(s.capture_to_event_ms, 0.95),
                "process_rss_mb": rss,
            },
            "events": dict(self.emitted),
            "suppressed": dict(self.limiter.suppressed),
            "sink_errors": self.sink_errors,
            "last_events": list(self.last_events),
        }
