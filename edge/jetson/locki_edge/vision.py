"""Camera capture, person/motion detection, and desk-zone tracking.

Python 3.6 compatible on purpose: the original Jetson Nano tops out at
JetPack 4.6 (Ubuntu 18.04, Python 3.6). Do not add dataclasses, walrus,
or other 3.7+ syntax here.
"""
import platform
import sys
import time


class Zone(object):
    """Desk zone as a normalized rectangle (0..1 on both axes)."""

    def __init__(self, x1, y1, x2, y2):
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError("zone must satisfy 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1")
        self.x1, self.y1, self.x2, self.y2 = float(x1), float(y1), float(x2), float(y2)

    @classmethod
    def parse(cls, text):
        parts = [float(p) for p in text.split(",")]
        if len(parts) != 4:
            raise ValueError("zone needs 4 comma-separated numbers: x1,y1,x2,y2")
        return cls(*parts)

    def area(self):
        return (self.x2 - self.x1) * (self.y2 - self.y1)

    def overlap(self, bbox):
        """Intersection / min(bbox area, zone area). 1.0 = one fully contains the other."""
        bx1, by1, bx2, by2 = bbox
        ix = max(0.0, min(self.x2, bx2) - max(self.x1, bx1))
        iy = max(0.0, min(self.y2, by2) - max(self.y1, by1))
        inter = ix * iy
        b_area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        denom = min(b_area, self.area())
        return inter / denom if denom > 0 else 0.0

    def as_list(self):
        return [self.x1, self.y1, self.x2, self.y2]


class Detection(object):
    __slots__ = ("label", "confidence", "bbox")

    def __init__(self, label, confidence, bbox):
        self.label = label            # "person" or "motion"
        self.confidence = confidence  # float 0..1, or None when the detector has no calibrated score
        self.bbox = bbox              # normalized (x1, y1, x2, y2)


class ZoneTracker(object):
    """Debounces per-frame zone presence into enter/clear events.

    enter: `enter_frames` consecutive frames with a qualifying detection.
    clear: no qualifying detection for `clear_seconds`.
    """

    def __init__(self, enter_frames=3, clear_seconds=2.0):
        self.enter_frames = int(enter_frames)
        self.clear_seconds = float(clear_seconds)
        self.occupied = False
        self.streak = 0
        self.last_seen = None
        self.entered_at = None

    def update(self, present, now):
        if present:
            self.last_seen = now
            self.streak += 1
            if not self.occupied and self.streak >= self.enter_frames:
                self.occupied = True
                self.entered_at = now
                return "enter"
            return None
        self.streak = 0
        if self.occupied and self.last_seen is not None and now - self.last_seen >= self.clear_seconds:
            self.occupied = False
            return "clear"
        return None

    def reset(self):
        """Camera lost: forget state. We do not claim the zone is clear without frames."""
        self.occupied = False
        self.streak = 0
        self.last_seen = None
        self.entered_at = None


class MotionDetector(object):
    """Background subtraction inside the zone only. Fallback when TensorRT detectNet is unavailable.

    It detects *pixel change*, not people. Events it drives are labeled detector="motion".
    """

    name = "motion"

    def __init__(self, zone, min_fraction=0.04, warmup_frames=30):
        import cv2
        self.cv2 = cv2
        self.zone = zone
        self.min_fraction = float(min_fraction)
        self.warmup_frames = int(warmup_frames)
        self.frames = 0
        self.subtractor = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=False)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def detect(self, frame):
        cv2 = self.cv2
        h, w = frame.shape[:2]
        x1, y1 = int(self.zone.x1 * w), int(self.zone.y1 * h)
        x2, y2 = int(self.zone.x2 * w), int(self.zone.y2 * h)
        roi = frame[y1:y2, x1:x2]
        gray = cv2.GaussianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        mask = self.subtractor.apply(gray)
        self.frames += 1
        if self.frames <= self.warmup_frames:
            return []
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        fg = cv2.countNonZero(mask)
        if fg < self.min_fraction * mask.size:
            return []
        pts = cv2.findNonZero(mask)
        bx, by, bw, bh = cv2.boundingRect(pts)
        bbox = ((x1 + bx) / float(w), (y1 + by) / float(h), (x1 + bx + bw) / float(w), (y1 + by + bh) / float(h))
        return [Detection("motion", None, bbox)]


def _import_jetson():
    try:
        import jetson_inference as ji
        import jetson_utils as ju
    except ImportError:
        import jetson.inference as ji  # pre-2021 jetson-inference builds
        import jetson.utils as ju
    return ji, ju


class DetectNetDetector(object):
    """NVIDIA jetson-inference detectNet (TensorRT) person detector."""

    name = "detectnet"

    def __init__(self, model="ssd-mobilenet-v2", threshold=0.5):
        import cv2
        self.cv2 = cv2
        ji, ju = _import_jetson()
        self.ju = ju
        self.model = model
        self.net = ji.detectNet(model, threshold=threshold)

    def detect(self, frame):
        h, w = frame.shape[:2]
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        img = self.ju.cudaFromNumpy(rgb)
        out = []
        for d in self.net.Detect(img, overlay="none"):
            if self.net.GetClassDesc(d.ClassID) != "person":
                continue
            bbox = (d.Left / float(w), d.Top / float(h), d.Right / float(w), d.Bottom / float(h))
            out.append(Detection("person", float(d.Confidence), bbox))
        return out


def make_detector(kind, zone, threshold=0.5, model="ssd-mobilenet-v2"):
    """kind: auto | detectnet | motion. auto = detectnet if importable, else motion (logged)."""
    if kind in ("auto", "detectnet"):
        try:
            return DetectNetDetector(model=model, threshold=threshold)
        except Exception as err:  # ImportError, or jetson-inference "failed to load network"
            if kind == "detectnet":
                raise
            sys.stdout.write("[locki-edge] detectNet unavailable (%s: %s) -> motion fallback\n"
                             % (type(err).__name__, err))
    return MotionDetector(zone)


class Camera(object):
    """OpenCV capture with offline detection and bounded reconnects.

    source: integer index, /dev/videoN, a stable /dev/v4l/by-id/... path, or a video file (tests).
    """

    def __init__(self, source, width=640, height=480, offline_after=2.0, reopen_every=2.0, clock=time.monotonic):
        import cv2
        self.cv2 = cv2
        self.source = int(source) if str(source).isdigit() else source
        self.is_file = isinstance(self.source, str) and not self.source.startswith("/dev/")
        self.width, self.height = width, height
        self.offline_after = offline_after
        self.reopen_every = reopen_every
        self.clock = clock
        self.cap = None
        self.last_frame_at = None
        self.last_open_try = 0.0
        self.started_at = clock()
        self.file_frame_period = 0.0
        self.next_file_frame_at = 0.0
        self.last_error = None
        self._open()

    def _open(self):
        cv2 = self.cv2
        self.last_open_try = self.clock()
        if self.cap is not None:
            self.cap.release()
        if self.is_file:
            self.cap = cv2.VideoCapture(self.source)
            fps = self.cap.get(cv2.CAP_PROP_FPS) or 0
            self.file_frame_period = 1.0 / fps if fps > 0 else 0.0
            return
        backend = cv2.CAP_V4L2 if platform.system() == "Linux" else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(self.source, backend)
        if self.cap.isOpened():
            # MJPG keeps 640x480 at full rate over USB 2.0 on Logitech UVC cameras.
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            self.last_error = "open_failed"

    def read(self):
        """Returns (frame, capture_time_monotonic) or (None, None). Monotonic: NTP jumps can't fake offline."""
        now = self.clock()
        if self.cap is None or not self.cap.isOpened():
            if not self.is_file and now - self.last_open_try >= self.reopen_every:
                self._open()
            return None, None
        if self.is_file and self.file_frame_period:
            wait = self.next_file_frame_at - now
            if wait > 0:
                time.sleep(wait)
            self.next_file_frame_at = max(now, self.next_file_frame_at) + self.file_frame_period
        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.last_error = "read_failed"
            if not self.is_file and now - (self.last_frame_at or self.started_at) >= self.reopen_every:
                self._open()
            return None, None
        self.last_frame_at = self.clock()
        self.last_error = None
        return frame, self.last_frame_at

    def is_online(self):
        ref = self.last_frame_at
        if ref is None:
            return False
        return self.clock() - ref < self.offline_after

    def frame_age(self):
        if self.last_frame_at is None:
            return None
        return self.clock() - self.last_frame_at

    def release(self):
        if self.cap is not None:
            self.cap.release()
