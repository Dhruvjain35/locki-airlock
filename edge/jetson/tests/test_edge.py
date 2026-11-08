"""Unit + integration tests. stdlib unittest only (runs on the Jetson's Python 3.6 too).

    python3 -m unittest discover -s edge/jetson/tests -v     # from the repo root: the folder also holds Sid's tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from locki_edge.vision import Zone, ZoneTracker, Detection  # noqa: E402
from locki_edge.publish import (SequenceStore, make_event, validate_event, SpacetimeSink, Outbox,  # noqa: E402
                                JsonlSink)
from locki_edge import api as api_mod  # noqa: E402

try:
    import numpy as np
    import cv2
    HAVE_CV = True
except ImportError:
    HAVE_CV = False


class FakeStdb(object):
    """Mimics SpacetimeDB's documented reducer-call endpoint. Scriptable status codes."""

    def __init__(self, token="tok", script=None):
        self.calls = []
        self.token = token
        self.script = list(script or [])
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                    "ctype": self.headers.get("Content-Type"), "body": body})
                code = outer.script.pop(0) if outer.script else 200
                if outer.token and self.headers.get("Authorization") != "Bearer " + outer.token:
                    code = 401
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever)
        t.daemon = True
        t.start()

    def events(self):
        return [json.loads(json.loads(c["body"].decode())[0]) for c in self.calls]

    def close(self):
        self.server.shutdown()


class TestZone(unittest.TestCase):
    def test_overlap(self):
        z = Zone(0.25, 0.25, 0.75, 0.75)
        self.assertAlmostEqual(z.overlap((0.3, 0.3, 0.4, 0.4)), 1.0)      # small box fully inside
        self.assertAlmostEqual(z.overlap((0.0, 0.0, 1.0, 1.0)), 1.0)      # box covers whole zone
        self.assertEqual(z.overlap((0.8, 0.8, 0.9, 0.9)), 0.0)            # outside
        self.assertAlmostEqual(z.overlap((0.5, 0.25, 1.0, 0.75)), 0.5, places=5)  # half of box in zone

    def test_bad_zone(self):
        for bad in ("0.5,0.5,0.4,0.9", "0,0,1.2,1", "1,2,3"):
            with self.assertRaises(ValueError):
                Zone.parse(bad)


class TestTracker(unittest.TestCase):
    def test_enter_needs_consecutive_frames(self):
        t = ZoneTracker(enter_frames=3, clear_seconds=2.0)
        self.assertIsNone(t.update(True, 0.0))
        self.assertIsNone(t.update(True, 0.1))
        self.assertIsNone(t.update(False, 0.2))   # flicker resets streak
        self.assertIsNone(t.update(True, 0.3))
        self.assertIsNone(t.update(True, 0.4))
        self.assertEqual(t.update(True, 0.5), "enter")
        self.assertIsNone(t.update(True, 0.6))    # one event per episode

    def test_clear_after_timeout(self):
        t = ZoneTracker(enter_frames=1, clear_seconds=2.0)
        self.assertEqual(t.update(True, 0.0), "enter")
        self.assertIsNone(t.update(False, 1.9))
        self.assertIsNone(t.update(True, 1.95))   # came back: no clear
        self.assertIsNone(t.update(False, 3.9))
        self.assertEqual(t.update(False, 3.96), "clear")
        self.assertIsNone(t.update(False, 10.0))

    def test_reset_does_not_claim_clear(self):
        t = ZoneTracker(enter_frames=1)
        t.update(True, 0)
        t.reset()
        self.assertFalse(t.occupied)
        self.assertIsNone(t.update(False, 100))


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_seq_monotonic_across_restarts(self):
        p = os.path.join(self.d, "seq")
        s1 = SequenceStore(p)
        a = [s1.next() for _ in range(5)]
        s2 = SequenceStore(p)
        self.assertEqual(a, [1, 2, 3, 4, 5])
        self.assertEqual(s2.next(), 6)

    def test_seq_corrupt_file_restarts_safely(self):
        p = os.path.join(self.d, "seq")
        with open(p, "w") as f:
            f.write("garbage")
        self.assertEqual(SequenceStore(p).next(), 1)

    def test_event_shape_and_validation(self):
        s = SequenceStore(os.path.join(self.d, "seq"))
        e = make_event(s, "jetson", "person_in_zone", 0.87654321, {"zone_overlap": 0.9})
        self.assertEqual(sorted(e), sorted(["schema_version", "event_id", "source", "kind", "observed_at", "seq",
                                            "confidence", "metadata"]))
        self.assertEqual(e["confidence"], 0.8765)
        self.assertIsNone(make_event(s, "jetson", "heartbeat")["confidence"])
        for mutate in ({"kind": "malware_detected"}, {"source": "police"}, {"seq": 0}, {"confidence": 1.5},
                       {"extra": 1}, {"event_id": "nope"}, {"observed_at": "today"}, {"metadata": {"x": "a" * 5000}}):
            bad = dict(e, **mutate)
            with self.assertRaises(ValueError):
                validate_event(bad)

    def test_fixtures_validate(self):
        path = os.path.join(HERE, "fixtures", "legacy_jetson_events.json")
        with open(path) as f:
            fixtures = json.load(f)
        self.assertGreaterEqual(len(fixtures), 5)
        for e in fixtures:
            self.assertTrue(validate_event(e))


class TestPublisher(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.seq = SequenceStore(os.path.join(self.d, "seq"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_http_contract(self):
        fake = FakeStdb()
        try:
            sink = SpacetimeSink(fake.url, "locki", "tok")
            e = make_event(self.seq, "jetson", "person_in_zone", 0.9)
            self.assertEqual(sink.send(e), "ok")
            c = fake.calls[0]
            self.assertEqual(c["path"], "/v1/database/locki/call/ingest_device_event")
            self.assertEqual(c["auth"], "Bearer tok")
            self.assertEqual(c["ctype"], "application/json")
            args = json.loads(c["body"].decode())
            self.assertEqual(len(args), 1)                     # exactly one string arg
            self.assertEqual(json.loads(args[0]), e)
        finally:
            fake.close()

    def test_bad_token_is_rejected_not_retried(self):
        fake = FakeStdb(token="right")
        try:
            sink = SpacetimeSink(fake.url, "locki", "wrong")
            self.assertEqual(sink.send(make_event(self.seq, "jetson", "zone_clear")), "reject")
            self.assertIn("http_401", sink.last_error)
        finally:
            fake.close()

    def test_outbox_retries_5xx_then_delivers_in_order(self):
        fake = FakeStdb(script=[503, 500])
        try:
            ob = Outbox(SpacetimeSink(fake.url, "locki", "tok"), max_backoff=0.05)
            evs = [make_event(self.seq, "jetson", k) for k in ("person_in_zone", "zone_clear")]
            for e in evs:
                ob.put(e)
            deadline = time.time() + 5
            while ob.stats["sent"] < 2 and time.time() < deadline:
                time.sleep(0.02)
            ob.stop()
            self.assertEqual(ob.stats["sent"], 2)
            self.assertEqual(ob.stats["retries"], 2)
            self.assertEqual([e["event_id"] for e in fake.events()][-2:], [e["event_id"] for e in evs])
        finally:
            fake.close()

    def test_outbox_drops_failed_heartbeat(self):
        fake = FakeStdb(script=[503])
        try:
            ob = Outbox(SpacetimeSink(fake.url, "locki", "tok"), max_backoff=0.05)
            ob.put(make_event(self.seq, "jetson", "heartbeat"))
            time.sleep(0.5)
            ob.stop()
            self.assertEqual(ob.stats["dropped_heartbeats"], 1)
            self.assertEqual(len(fake.calls), 1)
        finally:
            fake.close()

    def test_network_down_is_retry(self):
        sink = SpacetimeSink("http://127.0.0.1:9", "locki", "tok", timeout=0.5)
        self.assertEqual(sink.send(make_event(self.seq, "jetson", "zone_clear")), "retry")

    def test_jsonl_sink(self):
        p = os.path.join(self.d, "ev.jsonl")
        JsonlSink(p).send(make_event(self.seq, "jetson", "zone_clear"))
        with open(p) as f:
            self.assertEqual(json.loads(f.readline())["kind"], "zone_clear")


class _State(object):
    def __init__(self, token):
        self.token = token

    def health(self):
        return {"status": "ok"}

    def metrics_payload(self):
        return {"fps": 30}

    def local_test_alert(self):
        return {"played": True}


class TestApi(unittest.TestCase):
    def _get(self, port, path, token=None, method="GET"):
        req = Request("http://127.0.0.1:%d%s" % (port, path), data=b"" if method == "POST" else None, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            r = urlopen(req, timeout=3)
            try:
                return r.getcode(), json.loads(r.read().decode())
            finally:
                r.close()
        except HTTPError as e:
            e.close()
            return e.code, None

    def test_token_enforced(self):
        srv = api_mod.start_api(_State("s3cret"), "127.0.0.1", 0)
        port = srv.server_address[1]
        try:
            self.assertEqual(self._get(port, "/health")[0], 401)
            self.assertEqual(self._get(port, "/health", "wrong")[0], 401)
            self.assertEqual(self._get(port, "/local-test-alert", method="POST")[0], 401)
            self.assertEqual(self._get(port, "/health", "s3cret"), (200, {"status": "ok"}))
            self.assertEqual(self._get(port, "/metrics", "s3cret"), (200, {"fps": 30}))
            self.assertEqual(self._get(port, "/local-test-alert", "s3cret", "POST"), (200, {"played": True}))
            self.assertEqual(self._get(port, "/nope", "s3cret")[0], 404)
        finally:
            srv.shutdown()

    def test_refuses_lan_bind_without_token(self):
        with self.assertRaises(SystemExit):
            api_mod.start_api(_State(None), "0.0.0.0", 0)

    def test_percentile(self):
        self.assertEqual(api_mod.percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertIsNone(api_mod.percentile([], 50))


@unittest.skipUnless(HAVE_CV, "needs numpy + cv2")
class TestMotionDetector(unittest.TestCase):
    def test_block_in_zone_detected_outside_ignored(self):
        from locki_edge.vision import MotionDetector
        zone = Zone(0.5, 0.0, 1.0, 1.0)
        det = MotionDetector(zone, warmup_frames=20)
        bg = np.full((240, 320, 3), 90, np.uint8)
        for _ in range(40):
            self.assertEqual(det.detect(bg.copy()), [])
        outside = bg.copy()
        outside[60:180, 20:120] = 250                     # left half: not the zone
        self.assertEqual(det.detect(outside), [])
        inside = bg.copy()
        inside[60:180, 200:300] = 250
        hits = det.detect(inside)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].label, "motion")
        self.assertIsNone(hits[0].confidence)
        self.assertGreater(zone.overlap(hits[0].bbox), 0.9)


class TestDetectNetAdapter(unittest.TestCase):
    """No Jetson here: inject a fake jetson_inference to verify our adapter (filtering + normalization)."""

    def test_person_filter_and_normalized_bbox(self):
        if not HAVE_CV:
            self.skipTest("needs numpy + cv2")

        class D(object):
            def __init__(self, cid, conf, l, t, r, b):
                self.ClassID, self.Confidence, self.Left, self.Top, self.Right, self.Bottom = cid, conf, l, t, r, b

        class Net(object):
            def __init__(self, model, threshold=0.5):
                self.model, self.threshold = model, threshold

            def Detect(self, img, overlay="box"):
                assert overlay == "none"
                return [D(1, 0.91, 320, 120, 640, 480), D(3, 0.99, 0, 0, 10, 10)]

            def GetClassDesc(self, cid):
                return {1: "person", 3: "car"}[cid]

        ji = types.ModuleType("jetson_inference")
        ji.detectNet = Net
        ju = types.ModuleType("jetson_utils")
        ju.cudaFromNumpy = lambda a: a
        sys.modules["jetson_inference"], sys.modules["jetson_utils"] = ji, ju
        try:
            from locki_edge.vision import make_detector
            det = make_detector("auto", Zone(0, 0, 1, 1))
            self.assertEqual(det.name, "detectnet")
            out = det.detect(np.zeros((480, 640, 3), np.uint8))
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0].label, "person")
            self.assertAlmostEqual(out[0].confidence, 0.91)
            self.assertEqual(out[0].bbox, (0.5, 0.25, 1.0, 1.0))
        finally:
            del sys.modules["jetson_inference"], sys.modules["jetson_utils"]

    def test_auto_falls_back_to_motion_without_jetson(self):
        if not HAVE_CV:
            self.skipTest("needs numpy + cv2")
        from locki_edge.vision import make_detector
        # Simulate a box WITHOUT jetson-inference even on a Jetson that has it built (None blocks the import).
        names = ("jetson_inference", "jetson_utils", "jetson", "jetson.inference", "jetson.utils")
        saved = {n: sys.modules[n] for n in names if n in sys.modules}
        sys.modules.update({n: None for n in names})
        try:
            self.assertEqual(make_detector("auto", Zone(0, 0, 1, 1)).name, "motion")
            with self.assertRaises(ImportError):
                make_detector("detectnet", Zone(0, 0, 1, 1))
        finally:
            for n in names:
                sys.modules.pop(n, None)
            sys.modules.update(saved)


@unittest.skipUnless(HAVE_CV, "needs numpy + cv2")
class TestEndToEnd(unittest.TestCase):
    """Synthetic 'camera' video -> real service process -> fake SpacetimeDB. Checks the full event story."""

    def make_video(self, path, fps=15):
        w, h = 320, 240
        out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
        bg = np.full((h, w, 3), 80, np.uint8)
        frames = [bg] * (fps * 3)                              # 3 s empty (background warmup)
        person = bg.copy()
        person[40:240, 170:290] = 230                          # 'person' inside zone x>0.5
        frames += [person] * (fps * 2)                         # 2 s in zone
        frames += [bg] * (fps * 4)                             # 4 s empty -> zone_clear after 1 s
        for f in frames:
            out.write(f)
        out.release()

    def test_full_story(self):
        d = tempfile.mkdtemp()
        fake = FakeStdb(token="tok")
        try:
            video = os.path.join(d, "cam.avi")
            self.make_video(video)
            tok = os.path.join(d, "token")
            with open(tok, "w") as f:
                f.write("tok")
            env = dict(os.environ, LOCKI_EDGE_API_TOKEN="apitok", PYTHONPATH=ROOT)
            cmd = [sys.executable, "-m", "locki_edge.service", "--camera", video, "--detector", "motion",
                   "--zone", "0.5,0.0,1.0,1.0", "--clear-seconds", "1.0", "--state-dir", d,
                   "--stdb-url", fake.url, "--stdb-db", "locki", "--stdb-token-file", tok,
                   "--api-port", "18765", "--exit-on-camera-eof", "--run-seconds", "30"]
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            time.sleep(2.0)
            req = Request("http://127.0.0.1:18765/health")
            req.add_header("Authorization", "Bearer apitok")
            health = json.loads(urlopen(req, timeout=3).read().decode())
            out, _ = proc.communicate(timeout=40)
            self.assertEqual(proc.returncode, 0, out.decode())
            self.assertEqual(health["detector"], "motion")
            self.assertTrue(health["camera_ok"])

            evs = fake.events()
            kinds = [e["kind"] for e in evs if e["kind"] != "heartbeat"]
            self.assertEqual(kinds, ["camera_online", "person_in_zone", "zone_clear", "camera_offline"], out.decode())
            self.assertIn("heartbeat", [e["kind"] for e in evs])
            seqs = [e["seq"] for e in evs]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(len(set(seqs)), len(seqs))
            self.assertEqual(len(set(e["event_id"] for e in evs)), len(evs))
            pz = [e for e in evs if e["kind"] == "person_in_zone"][0]
            self.assertIsNone(pz["confidence"])                 # motion has no calibrated score
            self.assertEqual(pz["metadata"]["detector"], "motion")
            self.assertLess(pz["metadata"]["capture_to_event_ms"], 500)
            for e in evs:
                validate_event(e)
                self.assertLess(len(json.dumps(e)), 4096)        # no frames ever leave the device
            with open(os.path.join(d, "events.jsonl")) as f:
                audit = [json.loads(l) for l in f]
            self.assertEqual([e["event_id"] for e in audit], [e["event_id"] for e in evs])
        finally:
            fake.close()
            shutil.rmtree(d)


class TestTrials(unittest.TestCase):
    def test_scoring(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import trials
        tr = [{"trial": 1, "label": "at_desk", "start_ms": 1000, "end_ms": 9000},
              {"trial": 2, "label": "walk_by", "start_ms": 20000, "end_ms": 28000},
              {"trial": 3, "label": "empty", "start_ms": 40000, "end_ms": 48000},
              {"trial": 4, "label": "at_desk", "start_ms": 60000, "end_ms": 68000}]
        ev = [{"kind": "person_in_zone", "observed_at": 2500, "confidence": 0.8, "metadata": {"detector": "detectnet"}},
              {"kind": "person_in_zone", "observed_at": 24000, "confidence": 0.6, "metadata": {"detector": "detectnet"}},
              {"kind": "heartbeat", "observed_at": 41000, "confidence": None, "metadata": {}}]
        s = trials.summarize(trials.score(tr, ev, 1500))
        self.assertEqual((s["tp"], s["fn"], s["fp"], s["tn"]), (1, 1, 1, 1))
        self.assertEqual(s["recall"], 0.5)
        self.assertEqual(s["false_alerts_per_20_benign"], 10.0)
        self.assertEqual(s["latency_p50_ms"], 1500)


class TestReviewRegressions(unittest.TestCase):
    """One test per bug found in the independent review."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.seq = SequenceStore(os.path.join(self.d, "seq"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_reducer_error_530_is_reject_and_does_not_block_queue(self):
        fake = FakeStdb(script=[530])
        try:
            ob = Outbox(SpacetimeSink(fake.url, "locki", "tok"), max_backoff=0.05)
            a, b = make_event(self.seq, "jetson", "person_in_zone"), make_event(self.seq, "jetson", "zone_clear")
            ob.put(a)
            ob.put(b)
            deadline = time.time() + 3
            while ob.stats["sent"] < 1 and time.time() < deadline:
                time.sleep(0.02)
            ob.stop()
            self.assertEqual((ob.stats["rejected"], ob.stats["sent"]), (1, 1))
            self.assertEqual(fake.events()[-1]["event_id"], b["event_id"])
        finally:
            fake.close()

    def test_retry_cap_gives_up_instead_of_stalling_forever(self):
        fake = FakeStdb(script=[503] * 50)
        try:
            ob = Outbox(SpacetimeSink(fake.url, "locki", "tok"), max_backoff=0.01, max_attempts=3)
            ob.put(make_event(self.seq, "jetson", "person_in_zone"))
            later = make_event(self.seq, "jetson", "zone_clear")
            ob.put(later)
            deadline = time.time() + 3
            while ob.stats["gave_up"] < 1 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(ob.stats["gave_up"], 1)
            self.assertGreaterEqual(len(fake.calls), 3)  # 3 attempts on the first event, then it moved on
            ob.stop(flush_seconds=0.2)
        finally:
            fake.close()

    def test_http_client_exception_does_not_kill_outbox(self):
        sink = SpacetimeSink("http://127.0.0.1:30OO", "locki", "tok")   # typo'd port -> InvalidURL
        self.assertEqual(sink.send(make_event(self.seq, "jetson", "zone_clear")), "retry")
        self.assertIn("network:", sink.last_error)

    def test_auto_falls_back_when_detectnet_fails_to_load(self):
        if not HAVE_CV:
            self.skipTest("needs numpy + cv2")

        class Broken(object):
            def __init__(self, *a, **k):
                raise Exception("detectNet -- failed to load network")

        ji = types.ModuleType("jetson_inference")
        ji.detectNet = Broken
        ju = types.ModuleType("jetson_utils")
        sys.modules["jetson_inference"], sys.modules["jetson_utils"] = ji, ju
        try:
            from locki_edge.vision import make_detector
            self.assertEqual(make_detector("auto", Zone(0, 0, 1, 1)).name, "motion")
            with self.assertRaises(Exception):
                make_detector("detectnet", Zone(0, 0, 1, 1))
        finally:
            del sys.modules["jetson_inference"], sys.modules["jetson_utils"]

    def test_timedatectl_parsing_ubuntu_1804_and_newer(self):
        from locki_edge.service import parse_timedatectl_status
        bionic = "      Local time: Sat 2026-11-07\n System clock synchronized: yes\nsystemd-timesyncd.service active: yes\n"
        old = "  NTP synchronized: no\n"
        self.assertTrue(parse_timedatectl_status(bionic))
        self.assertFalse(parse_timedatectl_status(old))
        self.assertIsNone(parse_timedatectl_status("garbage"))

    def test_camera_uses_monotonic_clock(self):
        if not HAVE_CV:
            self.skipTest("needs numpy + cv2")
        from locki_edge.vision import Camera
        cam = Camera(os.path.join(self.d, "missing.avi"))
        self.assertIs(cam.clock, time.monotonic)
        cam.release()


class TestPython36Compat(unittest.TestCase):
    """Original Jetson Nano = JetPack 4.6 = Python 3.6. vermin checks syntax/stdlib use (dev machines only)."""

    def test_vermin(self):
        vermin = shutil.which("vermin") or os.path.join(os.path.dirname(sys.executable), "vermin")
        if not os.path.exists(vermin):
            self.skipTest("vermin not installed")
        files = [os.path.join(ROOT, "locki_edge"), os.path.join(ROOT, "tools")]
        r = subprocess.run([vermin, "-t=3.6-", "--no-tips", "--violations"] + files,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(r.returncode, 0, r.stdout.decode())


if __name__ == "__main__":
    unittest.main()
