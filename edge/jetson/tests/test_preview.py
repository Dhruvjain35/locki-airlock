"""Owner preview stream (locki_edge/preview.py, GET /stream.mjpg): off by default, loopback API only, bounded viewers."""
import os
import sys
import threading
import types
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from locki_edge import api as api_mod  # noqa: E402
from locki_edge import preview  # noqa: E402
from locki_edge.observers import VisionObservation, Zone  # noqa: E402
from locki_edge.runtime import EdgeRuntime  # noqa: E402

try:
    import numpy as np
    import cv2  # noqa: F401
    HAVE_CV = True
except ImportError:  # pragma: no cover
    HAVE_CV = False

ZONES = [Zone("desk_01", 0.2, 0.3, 0.8, 0.95)]


def _obs(kind="person_in_zone", conf=0.91):
    return VisionObservation(kind, conf, "desk_01", "2026-11-08T12:00:00.000Z", "test", {})


@unittest.skipUnless(HAVE_CV, "needs numpy + OpenCV")
class TestRender(unittest.TestCase):
    def test_jpeg_with_zone(self):
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        jpeg = preview.render_preview(img, ZONES, [_obs()], fresh=True)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertEqual(img.sum(), 0, "the captured frame itself is never drawn on")

    def test_wide_frames_are_scaled_down(self):
        img = np.zeros((1080, 1920, 3), dtype=np.uint8)
        out = cv2.imdecode(np.frombuffer(preview.render_preview(img, ZONES), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(out.shape[1], preview.MAX_WIDTH)

    def test_nothing_without_a_frame(self):
        self.assertIsNone(preview.render_preview(None, ZONES))

    def test_part_framing(self):
        p = preview.part(b"\xff\xd8xx")
        self.assertTrue(p.startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n"))


class TestRuntimePreview(unittest.TestCase):
    def _rt(self, on, frame):
        return types.SimpleNamespace(cfg=types.SimpleNamespace(preview_stream=on, zones=ZONES),
                                     capture=types.SimpleNamespace(last_frame=frame), _preview_obs=(None, ()))

    def test_off_by_default(self):
        frame = types.SimpleNamespace(image=object())
        self.assertIsNone(EdgeRuntime.preview_jpeg(self._rt(False, frame)))

    def test_no_frame_yet(self):
        self.assertIsNone(EdgeRuntime.preview_jpeg(self._rt(True, None)))

    @unittest.skipUnless(HAVE_CV, "needs numpy + OpenCV")
    def test_on_with_a_frame(self):
        frame = types.SimpleNamespace(image=np.zeros((48, 64, 3), dtype=np.uint8))
        self.assertTrue(EdgeRuntime.preview_jpeg(self._rt(True, frame)).startswith(b"\xff\xd8"))


class _State(object):
    def __init__(self, on, slots=2):
        self.token = None
        self.running = True
        self.cfg = types.SimpleNamespace(preview_stream=on)
        self.preview_slots = threading.BoundedSemaphore(slots)

    def health(self):
        return {"status": "ok"}

    def preview_jpeg(self):
        return b"\xff\xd8fake-jpeg"


class TestStreamRoute(unittest.TestCase):
    def test_off_is_404(self):
        srv = api_mod.start_api(_State(False), "127.0.0.1", 0)
        try:
            with self.assertRaises(HTTPError) as e:
                urlopen("http://127.0.0.1:%d/stream.mjpg" % srv.server_address[1], timeout=3)
            self.assertEqual(e.exception.code, 404)
            e.exception.close()
        finally:
            srv.shutdown()

    def test_on_streams_jpeg_parts_and_keeps_health_answering(self):
        state = _State(True, slots=1)
        srv = api_mod.start_api(state, "127.0.0.1", 0)
        port = srv.server_address[1]
        try:
            r = urlopen("http://127.0.0.1:%d/stream.mjpg?t=1" % port, timeout=3)
            self.assertTrue(r.headers["Content-Type"].startswith("multipart/x-mixed-replace; boundary=frame"))
            self.assertIn(b"\xff\xd8fake-jpeg", r.read(200))
            # The stream holds its only slot, so a second viewer is refused, and /health still answers meanwhile.
            with self.assertRaises(HTTPError) as e:
                urlopen("http://127.0.0.1:%d/stream.mjpg" % port, timeout=3)
            self.assertEqual(e.exception.code, 429)
            e.exception.close()
            self.assertEqual(urlopen("http://127.0.0.1:%d/health" % port, timeout=3).getcode(), 200)
            r.close()
        finally:
            state.running = False
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
