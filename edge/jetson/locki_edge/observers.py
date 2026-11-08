"""Vision observer interface + providers. No new CV algorithms live here.

    provider.observe(frame_bgr, captured_at_utc) -> [VisionObservation, ...]

Providers:
  BaselineMotionProvider  pixel change inside each zone (OpenCV MOG2, from vision.py). Capability
                          "motion" ONLY: it can emit motion_in_zone, never person or face kinds.
  DetectNetPersonProvider NVIDIA jetson-inference detectNet person boxes (Jetson only, TensorRT).
  OpenCvDnnPersonProvider MobileNet-SSD person boxes on OpenCV DNN (JetPack 6, where detectNet's models fail).
  FixtureProvider         scripted observations for tests. Always simulated=true.
  PluginProvider          Dhruv's CV adapter, loaded from "package.module:factory" WITHOUT copying
                          his code. Its declared capabilities gate what it may emit.

Python 3.6 compatible on purpose.
"""
import importlib
import math
import time
import sys
from typing import Any, Dict, NamedTuple, Optional

KNOWN_CAPABILITIES = frozenset(("motion", "person", "face", "hand_zone"))


class VisionObservation(NamedTuple):
    kind: str                     # the event kind this single frame supports, e.g. "person_in_zone"
    confidence: Optional[float]   # calibrated score, or None when the provider has none
    zone_id: str
    captured_at_utc: str          # ISO-8601 UTC of the frame
    provider_version: str
    metadata: Dict[str, Any]      # narrowly scoped; the normalizer re-applies the allowlist


class Zone(NamedTuple):
    zone_id: str
    x1: float
    y1: float
    x2: float
    y2: float

    def overlap(self, bbox):
        """Intersection / min(bbox area, zone area). 1.0 = one fully contains the other."""
        bx1, by1, bx2, by2 = bbox
        ix = max(0.0, min(self.x2, bx2) - max(self.x1, bx1))
        iy = max(0.0, min(self.y2, by2) - max(self.y1, by1))
        b_area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        z_area = (self.x2 - self.x1) * (self.y2 - self.y1)
        denom = min(b_area, z_area)
        return (ix * iy) / denom if denom > 0 else 0.0


def parse_zones(raw):
    """[{"id": "desk_01", "rect": [x1, y1, x2, y2]}] -> [Zone]. Normalized 0..1 coordinates."""
    zones = []
    for item in raw:
        x1, y1, x2, y2 = [float(v) for v in item["rect"]]
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError("zone %s: need 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1" % item.get("id"))
        zones.append(Zone(str(item["id"]), x1, y1, x2, y2))
    if not zones:
        raise ValueError("at least one zone is required")
    if len(set(z.zone_id for z in zones)) != len(zones):
        raise ValueError("zone ids must be unique")
    return zones


class ObservationProvider(object):
    """Interface. Subclass or duck-type: name, version, capabilities, simulated, observe(), close()."""
    name = "abstract"
    version = "0"
    capabilities = frozenset()
    simulated = False

    def observe(self, frame, captured_at_utc):
        raise NotImplementedError

    def close(self):
        pass

    def describe(self):
        return {"name": self.name, "version": self.version, "capabilities": sorted(self.capabilities),
                "simulated": bool(self.simulated)}


class BaselineMotionProvider(ObservationProvider):
    name = "baseline_motion"
    version = "1"
    capabilities = frozenset(("motion",))

    def __init__(self, zones, min_fraction=0.04, warmup_frames=30):
        from .vision import MotionDetector, Zone as VZone
        self.zones = zones
        self.detectors = [(z, MotionDetector(VZone(z.x1, z.y1, z.x2, z.y2), min_fraction, warmup_frames))
                          for z in zones]

    def observe(self, frame, captured_at_utc):
        out = []
        for zone, det in self.detectors:
            for d in det.detect(frame):
                out.append(VisionObservation("motion_in_zone", None, zone.zone_id, captured_at_utc, self.version,
                                             {"bbox": [round(v, 3) for v in d.bbox]}))
        return out


class DetectNetPersonProvider(ObservationProvider):
    name = "detectnet_person"
    capabilities = frozenset(("person",))

    def __init__(self, zones, threshold=0.5, model="ssd-mobilenet-v2", min_overlap=0.3):
        from .vision import DetectNetDetector  # raises ImportError off-Jetson: no silent fallback here
        self.detector = DetectNetDetector(model=model, threshold=threshold)
        self.version = "jetson-inference/%s" % model
        self.zones = zones
        self.min_overlap = float(min_overlap)

    def observe(self, frame, captured_at_utc):
        out = []
        dets = self.detector.detect(frame)
        for zone in self.zones:
            best, best_ov = None, 0.0
            for d in dets:
                x1, y1, x2, y2 = d.bbox
                if (x2 - x1) * (y2 - y1) < getattr(self, "min_area", 0.0):
                    continue  # far-away person (a passerby behind the desk), not someone AT the desk
                ov = zone.overlap(d.bbox)
                if ov >= self.min_overlap and ov > best_ov:
                    best, best_ov = d, ov
            if best is not None:
                out.append(VisionObservation("person_in_zone", best.confidence, zone.zone_id, captured_at_utc,
                                             self.version, {"bbox": [round(v, 3) for v in best.bbox],
                                                            "zone_overlap": round(best_ov, 3)}))
        return out


class OpenCvDnnPersonProvider(DetectNetPersonProvider):
    """Same zone mapping as detectNet; the boxes come from MobileNet-SSD on OpenCV DNN (JetPack 6 path)."""
    name = "opencv_dnn_person"

    def __init__(self, zones, prototxt, caffemodel, threshold=0.5, min_overlap=0.3, min_area=0.0):
        self.min_area = float(min_area)
        from .vision import OpenCvDnnDetector
        self.detector = OpenCvDnnDetector(prototxt, caffemodel, threshold=threshold)
        self.version = "opencv-dnn/mobilenet-ssd-voc"
        self.zones = zones
        self.min_overlap = float(min_overlap)


def apple_vision_human_detector():
    """macOS only: Apple Vision VNDetectHumanRectanglesRequest (upper body, on-device). Returns
    detect(frame_bgr) -> [((x1, y1, x2, y2) normalized top-left origin, confidence)]. The frame is
    JPEG-encoded in memory for VNImageRequestHandler and never written or sent anywhere."""
    import cv2
    import Foundation
    import Vision

    def detect(frame):
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        raw = buf.tobytes()
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
            Foundation.NSData.dataWithBytes_length_(raw, len(raw)), None)
        req = Vision.VNDetectHumanRectanglesRequest.alloc().init()
        done, err = handler.performRequests_error_([req], None)
        if not done:
            raise RuntimeError("Vision request failed: %s" % err)
        out = []
        for obs in req.results() or []:
            bb = obs.boundingBox()  # normalized, bottom-left origin
            x, y, w, h = float(bb.origin.x), float(bb.origin.y), float(bb.size.width), float(bb.size.height)
            out.append(((max(0.0, x), max(0.0, 1.0 - y - h), min(1.0, x + w), min(1.0, 1.0 - y)),
                        float(obs.confidence())))
        return out

    return detect


class AppleVisionPersonProvider(ObservationProvider):
    """Person baseline for runtime=mac_dev ONLY: Apple's on-device human detector, so the Mac can emit an
    honest person_in_zone. Not Dhruv's model and never used on the Jetson (detectNet is the Jetson one)."""
    name = "apple_vision_person"
    version = "apple-vision/VNDetectHumanRectanglesRequest"
    capabilities = frozenset(("person",))

    def __init__(self, zones, threshold=0.5, min_overlap=0.3, detect=None):
        self.detect = detect if detect is not None else apple_vision_human_detector()  # ImportError off-macOS
        self.zones = zones
        self.threshold = float(threshold)
        self.min_overlap = float(min_overlap)
        self.inferences = 0
        self.infer_ms = []

    def observe(self, frame, captured_at_utc):
        t0 = time.monotonic()
        dets = [(bbox, conf) for bbox, conf in self.detect(frame) if conf >= self.threshold]
        self.inferences += 1
        self.infer_ms.append((time.monotonic() - t0) * 1000.0)
        if len(self.infer_ms) > 512:
            del self.infer_ms[:256]
        out = []
        for zone in self.zones:
            best, best_ov = None, 0.0
            for bbox, conf in dets:
                ov = zone.overlap(bbox)
                if ov >= self.min_overlap and ov > best_ov:
                    best, best_ov = (bbox, conf), ov
            if best is not None:
                out.append(VisionObservation("person_in_zone", round(best[1], 4), zone.zone_id, captured_at_utc,
                                             self.version, {"bbox": [round(v, 3) for v in best[0]],
                                                            "zone_overlap": round(best_ov, 3)}))
        return out

    def describe(self):
        d = ObservationProvider.describe(self)
        ms = sorted(self.infer_ms)
        d.update({"inferences": self.inferences,
                  "infer_ms_p50": round(ms[len(ms) // 2], 2) if ms else None,
                  "infer_ms_p95": round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 2) if ms else None})
        return d


class FixtureProvider(ObservationProvider):
    """script: {frame_index: [(kind, confidence, zone_id), ...]} or a callable(index) -> list."""
    name = "fixture"
    version = "fixture-1"
    simulated = True

    def __init__(self, script, capabilities=("person",)):
        self.script = script
        self.capabilities = frozenset(capabilities)
        self.index = 0

    def observe(self, frame, captured_at_utc):
        self.index += 1
        items = self.script(self.index) if callable(self.script) else self.script.get(self.index, [])
        return [VisionObservation(k, c, z, captured_at_utc, self.version, {}) for (k, c, z) in items]


class PluginProvider(ObservationProvider):
    """Adapter for Dhruv's provider. Accepts VisionObservation, objects with the same attributes, or dicts.

    kind_map renames his kinds to the contract's (e.g. {"person_present": "person_in_zone"}).
    Exceptions inside his observe() are counted and swallowed so a CV bug can't kill capture/health.
    """

    def __init__(self, inner, kind_map=None):
        if not callable(getattr(inner, "observe", None)):
            raise TypeError("plugin provider must have observe(frame, captured_at_utc)")
        self.inner = inner
        self.kind_map = dict(kind_map or {})
        self.name = "plugin:%s" % getattr(inner, "name", type(inner).__name__)
        self.version = str(getattr(inner, "version", getattr(inner, "provider_version", "unknown")))
        caps = getattr(inner, "capabilities", ()) or ()
        declared = frozenset((caps,) if isinstance(caps, str) else caps)   # a bare "person" is one capability
        self.capabilities = declared & KNOWN_CAPABILITIES
        self.unknown_capabilities = sorted(declared - KNOWN_CAPABILITIES)
        self.simulated = bool(getattr(inner, "simulated", False))
        self.errors = 0
        self.malformed = 0
        self.last_error = None

    def _coerce(self, item, captured_at_utc):
        get = item.get if isinstance(item, dict) else (lambda k, d=None: getattr(item, k, d))
        kind = get("kind")
        zone_id = get("zone_id") or get("zone")   # Dhruv's native camera events say "zone"
        if not isinstance(kind, str) or not isinstance(zone_id, str):
            raise ValueError("kind and zone_id must be strings")
        conf = get("confidence")
        if isinstance(conf, bool):
            raise ValueError("confidence must be a number, not a bool")
        if conf is not None:
            conf = float(conf)
            if not math.isfinite(conf):
                raise ValueError("confidence must be finite")
        md = get("metadata") or {}
        if not isinstance(md, dict):
            raise ValueError("metadata must be a dict")
        return VisionObservation(self.kind_map.get(kind, kind), conf, zone_id,
                                 get("captured_at_utc") or captured_at_utc,
                                 str(get("provider_version") or self.version), dict(md))

    def observe(self, frame, captured_at_utc):
        try:
            items = self.inner.observe(frame, captured_at_utc) or []
            # Materialize inside the try: a generator that raises mid-iteration must not escape and end the process.
            items = [items] if isinstance(items, dict) else list(items)
        except Exception as err:
            self.errors += 1
            self.last_error = "%s: %s" % (type(err).__name__, str(err)[:120])
            return []
        out = []
        for item in items:
            try:
                out.append(self._coerce(item, captured_at_utc))
            except (ValueError, TypeError, AttributeError):
                self.malformed += 1
        return out

    def close(self):
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()

    def describe(self):
        d = ObservationProvider.describe(self)
        d.update({"errors": self.errors, "malformed": self.malformed, "last_error": self.last_error,
                  "ignored_capabilities": self.unknown_capabilities})
        return d


def load_plugin(spec, search_path=None, kind_map=None, **factory_kwargs):
    """spec = "package.module:factory". search_path = Dhruv's checkout (added to sys.path, read-only use)."""
    if search_path and search_path not in sys.path:
        sys.path.insert(0, search_path)
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr or "create_provider")
    return PluginProvider(factory(**factory_kwargs), kind_map=kind_map)


__all__ = ["VisionObservation", "Zone", "parse_zones", "ObservationProvider", "BaselineMotionProvider",
           "DetectNetPersonProvider", "FixtureProvider", "PluginProvider", "load_plugin", "KNOWN_CAPABILITIES"]
