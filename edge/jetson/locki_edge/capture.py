"""Threaded camera capture with frame stamps, FPS/drop metrics, health state and reconnection.

One reader thread owns the device. It keeps ONLY the newest frame in a 1-slot mailbox, so a
slow consumer never processes seconds-old video: an unconsumed frame that gets replaced is
counted as `dropped_stale`. Every frame carries a monotonic capture time (for latency and
offline detection; NTP jumps can't fake an outage) and a UTC wall-clock time (for events).

Sources:
  mac_dev   -> AVFoundation via OpenCV (built-in camera or a Logitech plugged into the Mac)
  jetson    -> V4L2 via OpenCV, MJPG at 640x480 (keeps full rate over USB 2.0 on UVC Logitechs)
  file      -> a video file, paced at its own FPS (tests only, tagged as such)

Python 3.6 compatible on purpose. Do not add dataclasses or 3.7+ syntax.
"""
import platform
import threading
import time
from typing import Any, NamedTuple

STATE_STARTING = "starting"
STATE_STREAMING = "streaming"
STATE_STALLED = "stalled"          # device open, but no frame for `stall_after_s`
STATE_DISCONNECTED = "disconnected"  # open failed / read failing, reconnect loop running
STATE_STOPPED = "stopped"


class Frame(NamedTuple):
    image: Any               # numpy BGR array. Stays on the device: never logged, never published
    index: int               # per-source frame counter since process start
    captured_mono: float     # time.monotonic() right after the read returned
    captured_utc_ms: int     # wall clock, unix epoch ms
    width: int
    height: int


def is_device_path(source):
    s = str(source)
    return isinstance(source, int) or s.isdigit() or s.startswith("/dev/") or s.startswith("gst:")


def open_capture(cv2, source, width, height, fps=None, system=None):
    """Open one OpenCV capture with the right backend for this OS. Returns (cap, backend_name)."""
    system = system or platform.system()
    src = int(source) if str(source).isdigit() else source
    if not is_device_path(source):
        return cv2.VideoCapture(src), "file"
    if str(source).startswith("gst:"):
        # Explicit GStreamer pipeline (Jetson), e.g.
        # gst:v4l2src device=/dev/video0 ! image/jpeg,width=640,height=480,framerate=30/1 ! jpegdec !
        #     videoconvert ! video/x-raw,format=BGR ! appsink drop=1 max-buffers=1
        return cv2.VideoCapture(str(source)[4:], cv2.CAP_GSTREAMER), "gstreamer"
    if system == "Linux":
        cap = cv2.VideoCapture(src, cv2.CAP_V4L2)
        backend = "v4l2"
    elif system == "Darwin":
        cap = cv2.VideoCapture(src, cv2.CAP_AVFOUNDATION)
        backend = "avfoundation"
    else:
        cap = cv2.VideoCapture(src)
        backend = "any"
    if cap is not None and cap.isOpened():
        if backend == "v4l2":
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fps:
            cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap, backend


class RateWindow(object):
    """FPS over the last `window_s` seconds of monotonic timestamps."""

    def __init__(self, window_s=5.0):
        self.window_s = window_s
        self.stamps = []

    def add(self, t):
        self.stamps.append(t)
        cutoff = t - self.window_s
        while self.stamps and self.stamps[0] < cutoff:
            self.stamps.pop(0)

    def rate(self, now):
        stamps = [s for s in self.stamps if s >= now - self.window_s]
        if len(stamps) < 2:
            return 0.0
        span = stamps[-1] - stamps[0]
        return round((len(stamps) - 1) / span, 2) if span > 0 else 0.0


class CaptureSource(object):
    """Owns one camera. Thread-safe: `latest()`, `wait_frame()`, `health()` from any thread.

    opener(source, width, height) -> (cap, backend) is injectable so tests can simulate
    unplug/replug without hardware.
    """

    def __init__(self, source, runtime, width=640, height=480, fps=None, stall_after_s=2.0,
                 reconnect_backoff_s=(0.5, 1.0, 2.0, 4.0), clock=time.monotonic, wall=time.time,
                 opener=None, on_state=None, label=None):
        self.source = source
        self.runtime = runtime
        self.width, self.height, self.fps_req = int(width), int(height), fps
        self.stall_after_s = float(stall_after_s)
        self.reconnect_backoff_s = tuple(reconnect_backoff_s)
        self.clock, self.wall = clock, wall
        self.on_state = on_state
        self.is_file = not is_device_path(source)
        self.capture_source = "fixture" if self.is_file else (
            "jetson_usb_camera" if runtime == "jetson" else "mac_dev_camera")
        self.label = label or str(source)
        if opener is None:
            import cv2
            self._cv2 = cv2
            opener = lambda s, w, h: open_capture(cv2, s, w, h, fps)  # noqa: E731
        else:
            self._cv2 = None
        self.opener = opener
        self.cond = threading.Condition()
        self.slot = None              # newest unconsumed Frame
        self.last_frame = None        # newest Frame, consumed or not (for health)
        self.state = STATE_STARTING
        self.state_since = clock()
        self.backend = None
        self.last_error = None
        self.running = False
        self.thread = None
        self.cap = None
        self.rate = RateWindow()
        self.counters = {"frames": 0, "dropped_stale": 0, "read_failures": 0, "open_failures": 0,
                         "reconnects": 0, "disconnects": 0, "consumed": 0}
        self.started_mono = clock()
        self.file_period = 0.0

    # -- lifecycle -----------------------------------------------------------------
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, name="capture")
        self.thread.daemon = True
        self.thread.start()
        return self

    def stop(self, timeout=3.0):
        self.running = False
        with self.cond:
            self.cond.notify_all()
        if self.thread is not None:
            self.thread.join(timeout)
        self._release()
        self._set_state(STATE_STOPPED, None)

    # -- consumer API ----------------------------------------------------------------
    def wait_frame(self, timeout=1.0):
        """Newest frame not yet handed out, or None on timeout. Never returns the same frame twice."""
        deadline = self.clock() + timeout
        with self.cond:
            while self.slot is None and self.running:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self.cond.wait(min(remaining, 0.25))
            frame, self.slot = self.slot, None
            if frame is not None:
                self.counters["consumed"] += 1
            return frame

    def health(self):
        now = self.clock()
        with self.cond:
            last = self.last_frame
            age = None if last is None else round(now - last.captured_mono, 3)
            return {
                "state": self.state,
                "state_for_s": round(now - self.state_since, 1),
                "connected": self.state == STATE_STREAMING,
                "source": self.label,
                "capture_source": self.capture_source,
                "runtime": self.runtime,
                "backend": self.backend,
                "fps": self.rate.rate(now),
                "frame_age_s": age,
                "last_frame_utc_ms": None if last is None else last.captured_utc_ms,
                "resolution": None if last is None else [last.width, last.height],
                "last_error": self.last_error,
                "counters": dict(self.counters),
                "uptime_s": round(now - self.started_mono, 1),
            }

    # -- internals ---------------------------------------------------------------------
    def _set_state(self, state, error):
        changed = False
        with self.cond:
            if state != self.state:
                prev, self.state, self.state_since = self.state, state, self.clock()
                changed = True
                if state == STATE_DISCONNECTED and prev in (STATE_STREAMING, STATE_STALLED):
                    self.counters["disconnects"] += 1
            if error is not None:
                self.last_error = error
        if changed and self.on_state is not None:
            try:
                self.on_state(state, error)
            except Exception:  # a broken observer callback must never kill capture
                pass

    def _release(self):
        cap, self.cap = self.cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _open(self):
        self._release()
        try:
            cap, backend = self.opener(self.source, self.width, self.height)
        except Exception as err:
            cap, backend = None, None
            self.last_error = "open_exception: %s" % type(err).__name__
        if cap is None or not cap.isOpened():
            self.counters["open_failures"] += 1
            self._release()
            return False
        self.cap, self.backend = cap, backend
        if self.is_file and self._cv2 is not None:
            fps = cap.get(self._cv2.CAP_PROP_FPS) or 0
            self.file_period = 1.0 / fps if fps > 0 else 0.0
        return True

    def _run(self):
        attempt = 0
        next_file_frame = 0.0
        while self.running:
            if self.cap is None:
                if self._open():
                    if attempt or self.counters["disconnects"]:
                        self.counters["reconnects"] += 1
                    attempt = 0
                else:
                    self._set_state(STATE_DISCONNECTED, self.last_error or "open_failed")
                    if self.is_file:
                        return
                    delay = self.reconnect_backoff_s[min(attempt, len(self.reconnect_backoff_s) - 1)]
                    attempt += 1
                    self._sleep(delay)
                    continue
            if self.is_file and self.file_period:
                wait = next_file_frame - self.clock()
                if wait > 0:
                    self._sleep(wait)
                next_file_frame = max(self.clock(), next_file_frame) + self.file_period
            try:
                ok, image = self.cap.read()
            except Exception as err:
                ok, image = False, None
                self.last_error = "read_exception: %s" % type(err).__name__
            now = self.clock()
            if not ok or image is None:
                self.counters["read_failures"] += 1
                if self.is_file:
                    self._set_state(STATE_DISCONNECTED, "end_of_file")
                    self._release()
                    return
                last_ok = self.last_frame.captured_mono if self.last_frame else self.state_since
                if now - last_ok >= self.stall_after_s:
                    # Device stopped delivering: drop the handle and go back to the open/backoff loop.
                    self._set_state(STATE_DISCONNECTED, "read_failed")
                    self._release()
                else:
                    self._set_state(STATE_STALLED if self.state == STATE_STREAMING else self.state,
                                    "read_failed")
                    self._sleep(0.02)
                continue
            h, w = image.shape[:2]
            with self.cond:
                self.counters["frames"] += 1
                frame = Frame(image, self.counters["frames"], now, int(round(self.wall() * 1000)), w, h)
                if self.slot is not None:
                    self.counters["dropped_stale"] += 1
                self.slot = frame
                self.last_frame = frame
                self.rate.add(now)
                self.cond.notify_all()
            if self.state != STATE_STREAMING:
                self._set_state(STATE_STREAMING, None)
                with self.cond:
                    self.last_error = None

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self.running:
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.05))

    def watchdog(self):
        """Call periodically from another thread: a V4L2 read on an unplugged camera can block for
        seconds, so the reader thread can't be trusted to notice its own stall. Returns state."""
        now = self.clock()
        with self.cond:
            last = self.last_frame.captured_mono if self.last_frame else None
            state = self.state
        if state == STATE_STREAMING and last is not None and now - last >= self.stall_after_s:
            self._set_state(STATE_STALLED, "no_frames_%.1fs" % (now - last))
        return self.state


def frame_age_ok(frame, clock=time.monotonic, max_age_s=0.5):
    return frame is not None and clock() - frame.captured_mono <= max_age_s


__all__ = ["CaptureSource", "Frame", "open_capture", "STATE_STREAMING", "STATE_STALLED", "STATE_DISCONNECTED",
           "STATE_STARTING", "STATE_STOPPED"]
