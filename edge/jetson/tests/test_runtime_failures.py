"""Integration + failure tests for the edge runtime (stdlib unittest; cv2 not required).

Covers: camera disconnect/reconnect (EDGE-02), event correctness (EDGE-03), temporal debounce
(EDGE-04), device authentication (EDGE-05 locally), DB outages and timeouts (EDGE-07), privacy
(EDGE-12), restart durability and clean shutdown.

    cd edge/jetson && python3 -m unittest tests.test_runtime_failures -v
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from locki_edge import contract as C  # noqa: E402
from locki_edge.capture import (STATE_DISCONNECTED, STATE_STREAMING, CaptureSource)  # noqa: E402
from locki_edge.config import ConfigError, EdgeConfig  # noqa: E402
from locki_edge.normalizer import EventNormalizer  # noqa: E402
from locki_edge.observers import FixtureProvider, PluginProvider, VisionObservation  # noqa: E402
from locki_edge.outbox import DurableOutbox  # noqa: E402
from locki_edge.runtime import EdgeRuntime  # noqa: E402
from locki_edge.stdb import RawResult, SpacetimePublisher, StdbClient, classify  # noqa: E402


# ---------------------------------------------------------------------------------- fakes
class FakeImage(object):
    shape = (480, 640, 3)


class FakeCap(object):
    """Scriptable cv2.VideoCapture stand-in. `plugged` is shared with FakeCamera."""

    def __init__(self, cam):
        self.cam = cam
        self.open = cam.plugged

    def isOpened(self):
        return self.open

    def read(self):
        time.sleep(0.005)
        if not self.cam.plugged:
            return False, None
        return True, FakeImage()

    def release(self):
        self.open = False

    def get(self, prop):
        return 0


class FakeCamera(object):
    def __init__(self):
        self.plugged = True
        self.opens = 0

    def opener(self, source, w, h):
        self.opens += 1
        return FakeCap(self), "fake"


class FakeStdb(object):
    """SpacetimeDB HTTP stand-in: enforces the token, the 11-arg ingest signature, seq order and dedupe
    the way the real reducer is expected to; scriptable failures per call."""

    def __init__(self, token="device-token", script=None, delay_s=0.0):
        self.token = token
        self.script = list(script or [])
        self.delay_s = delay_s
        self.calls = []
        self.events = {}
        self.health = []
        self.last_seq = 0
        self.active_session = "ws-session-1"   # the workstation's live armed session ("" = disarmed)
        self.session_state = "armed"
        self.sql_calls = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                path = self.path
                if path.endswith("/sql"):   # session discovery reads; not scripted, not counted in calls
                    outer.sql_calls += 1
                    return self._sql(body.decode(), self.headers.get("Authorization") == "Bearer " + outer.token)
                outer.calls.append(path)
                action = outer.script.pop(0) if outer.script else None
                if action == "sleep":
                    time.sleep(outer.delay_s)
                elif isinstance(action, int):
                    return self._reply(action, "scripted %d" % action)
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    return self._reply(530, "Not an enrolled member of this workstation")
                args = json.loads(body.decode())
                if path.endswith("/call/report_health"):
                    if len(args) != 5:
                        return self._reply(400, "bad args")
                    outer.health.append(args)
                    return self._reply(200, "")
                if not path.endswith("/call/ingest_device_event"):
                    return self._reply(404, "no such reducer")
                if len(args) != 11 or not isinstance(args[5], int) or not isinstance(args[7], float):
                    return self._reply(400, "invalid arguments")
                md = json.loads(args[8])
                if not all(isinstance(v, str) and len(v) <= 64 for v in md.values()):
                    return self._reply(530, "Metadata must contain only bounded redacted string fields")
                if args[4] == "motion_in_zone":
                    return self._reply(530, "Event kind not permitted for publisher role")
                if not outer.active_session or outer.session_state != "armed" or args[1] != outer.active_session:
                    return self._reply(530, "No matching live armed session")
                event_id, seq = args[3], args[6]
                if event_id in outer.events:
                    return self._reply(530, "duplicate event_id")
                if seq <= outer.last_seq:
                    return self._reply(530, "stale seq: %d <= last_seq %d" % (seq, outer.last_seq))
                outer.last_seq = seq
                outer.events[event_id] = args
                return self._reply(200, "")

            def _sql(self, query, authed):
                def table(names, rows):
                    return [{"schema": {"elements": [{"name": {"some": n}} for n in names]}, "rows": rows}]
                if not authed:
                    doc = table(["id"], [])
                elif "my_workstations" in query:
                    doc = table(["id", "active_session_id"], [["ws-1", outer.active_session]])
                elif "my_sessions" in query and outer.active_session:
                    doc = table(["id", "state", "expires_at_ms", "disarmed_at_ms"],
                                [[outer.active_session, outer.session_state, 0, 0]])
                else:
                    doc = table(["id"], [])
                data = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _reply(self, code, text):
                data = text.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port  # nothing listens here


class Seq(object):
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return self.value


def make(seq, kind="person_in_zone", **over):
    kw = dict(workstation_id="ws-1", session_id="ws-session-1", device_id="jetson-01", source="jetson_usb_camera", kind=kind,
              observed_at_ms=int(time.time() * 1000), confidence=0.9,
              metadata={"zone_id": "desk_01", "observation": "presence"}, simulated=False)
    kw.update(over)
    return C.make_event(C.Contract(), seq, **kw)


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ---------------------------------------------------------------------------------- capture
class TestCaptureFailures(unittest.TestCase):
    def test_unplug_then_replug_recovers_and_counts(self):
        cam = FakeCamera()
        states = []
        cap = CaptureSource("/dev/video0", "jetson", stall_after_s=0.3, reconnect_backoff_s=(0.05,),
                            opener=cam.opener, on_state=lambda s, e: states.append(s)).start()
        try:
            self.assertTrue(wait_for(lambda: cap.state == STATE_STREAMING))
            self.assertEqual(cap.capture_source, "jetson_usb_camera")
            cam.plugged = False
            self.assertTrue(wait_for(lambda: cap.state == STATE_DISCONNECTED, 3))
            frames_during_outage = cap.health()["counters"]["frames"]
            time.sleep(0.3)
            self.assertEqual(cap.health()["counters"]["frames"], frames_during_outage)  # no fake frames
            cam.plugged = True
            self.assertTrue(wait_for(lambda: cap.state == STATE_STREAMING, 3))
            h = cap.health()
            self.assertGreaterEqual(h["counters"]["disconnects"], 1)
            self.assertGreaterEqual(h["counters"]["reconnects"], 1)
            self.assertGreater(h["counters"]["read_failures"], 0)
            self.assertIn(STATE_DISCONNECTED, states)
        finally:
            cap.stop()

    def test_never_returns_the_same_frame_twice_and_counts_stale_drops(self):
        cam = FakeCamera()
        cap = CaptureSource(0, "mac_dev", opener=cam.opener).start()
        try:
            first = cap.wait_frame(2.0)
            self.assertIsNotNone(first)
            time.sleep(0.2)  # slow consumer: reader replaces unconsumed frames
            second = cap.wait_frame(2.0)
            self.assertGreater(second.index, first.index)
            self.assertGreater(cap.health()["counters"]["dropped_stale"], 0)
            self.assertEqual(cap.capture_source, "mac_dev_camera")
            self.assertLess(abs(second.captured_utc_ms / 1000.0 - time.time()), 2.0)
        finally:
            cap.stop()

    def test_watchdog_flags_a_blocked_read(self):
        cam = FakeCamera()
        cap = CaptureSource(0, "mac_dev", stall_after_s=0.2, opener=cam.opener).start()
        try:
            self.assertTrue(wait_for(lambda: cap.state == STATE_STREAMING))
            cap.running = False           # freeze the reader as if read() blocked
            cap.thread.join(1.0)
            time.sleep(0.3)
            self.assertNotEqual(cap.watchdog(), STATE_STREAMING)
        finally:
            cap.stop()


# ---------------------------------------------------------------------------------- normalizer
class TestNormalizer(unittest.TestCase):
    def run_frames(self, norm, present_frames, total, kind="person_in_zone", fps=30.0, t0=1000.0):
        events = []
        for i in range(total):
            mono = t0 + i / fps
            obs = [VisionObservation(kind, 0.8, "desk_01", "", "v", {"bbox": [0.1, 0.1, 0.5, 0.9]})] \
                if i in present_frames else []
            events += norm.process(obs, int(1794100000000 + i * 1000 / fps), mono)
        return events

    def test_one_event_per_sustained_crossing(self):
        prov = FixtureProvider({}, capabilities=("person",))
        norm = EventNormalizer(prov, ["desk_01"], enter_frames=3, exit_s=2.0)
        events = self.run_frames(norm, set(range(10, 310)), 400)   # 10 s of presence at 30 fps
        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["person_in_zone", "person_left_zone"])
        enter, left = events
        self.assertEqual(enter.observed_at_ms, int(1794100000000 + 10 * 1000 / 30.0))  # first frame of streak
        self.assertEqual(enter.metadata["frames_confirmed"], 3)
        self.assertEqual(left.observed_at_ms, int(1794100000000 + 309 * 1000 / 30.0))  # last frame seen
        self.assertAlmostEqual(left.metadata["dwell_ms"], 299 * 1000 / 30.0, delta=2)
        self.assertEqual(enter.confidence, 0.8)

    def test_flicker_does_not_trigger(self):
        prov = FixtureProvider({}, capabilities=("person",))
        norm = EventNormalizer(prov, ["desk_01"], enter_frames=3)
        self.assertEqual(self.run_frames(norm, {5, 6, 8, 9, 11, 12}, 100), [])

    def test_motion_provider_cannot_emit_person_or_face(self):
        prov = FixtureProvider({}, capabilities=("motion",))
        norm = EventNormalizer(prov, ["desk_01"], enter_frames=1)
        for kind in ("person_in_zone", "unrecognized_face", "owner_face_match", "usb_zone_interaction",
                     "camera_unavailable"):
            self.assertEqual(self.run_frames(norm, set(range(0, 30)), 40, kind=kind), [], kind)
        self.assertGreaterEqual(norm.counters["capability_violations"], 4 * 30)

    def test_motion_is_enter_only_with_cooldown(self):
        prov = FixtureProvider({}, capabilities=("motion",))
        norm = EventNormalizer(prov, ["desk_01"], enter_frames=3, exit_s=0.5, cooldown_s=10.0)
        present = set(range(0, 30)) | set(range(60, 90)) | set(range(400, 430))  # 3 bursts; 2nd inside cooldown
        events = self.run_frames(norm, present, 500, kind="motion_in_zone")
        self.assertEqual([e.kind for e in events], ["motion_in_zone", "motion_in_zone"])
        self.assertEqual(norm.counters["cooldown_suppressed"], 1)

    def test_camera_loss_never_emits_a_clear(self):
        prov = FixtureProvider({}, capabilities=("person",))
        norm = EventNormalizer(prov, ["desk_01"], enter_frames=3, exit_s=2.0)
        events = self.run_frames(norm, set(range(0, 60)), 60)
        self.assertEqual([e.kind for e in events], ["person_in_zone"])
        norm.camera_lost()
        self.assertEqual(norm.occupied_zones(), [])
        after = self.run_frames(norm, set(), 200, t0=2000.0)
        self.assertEqual(after, [])  # no person_left_zone invented after the outage

    def test_privacy_and_allowlist_on_provider_metadata(self):
        prov = FixtureProvider({}, capabilities=("person",))
        norm = EventNormalizer(prov, ["desk_01"], enter_frames=1)
        obs = [VisionObservation("person_in_zone", 0.7, "desk_01", "", "v",
                                 {"face_embedding": [0.1] * 128, "frame_jpeg": "xx", "bbox": [0.1, 0.1, 0.4, 0.9],
                                  "arbitrary": {"nested": True}})]
        events = norm.process(obs, 1794100000000, 1.0)
        self.assertEqual(len(events), 1)
        md = events[0].metadata
        self.assertNotIn("face_embedding", md)
        self.assertNotIn("frame_jpeg", md)
        self.assertNotIn("arbitrary", md)
        self.assertEqual(norm.counters["privacy_stripped"], 2)
        self.assertEqual(norm.counters["metadata_stripped"], 1)

    def test_rate_limit(self):
        prov = FixtureProvider({}, capabilities=("face",))
        norm = EventNormalizer(prov, ["desk_01"], instant_frames=1, cooldown_s=0.0, max_episodes_per_min=5)
        events = self.run_frames(norm, set(range(0, 300)), 300, kind="unrecognized_face")
        self.assertEqual(len(events), 5)
        self.assertGreater(norm.counters["rate_limited"], 0)

    def test_plugin_adapter_maps_kinds_and_survives_crashes(self):
        class Dhruv(object):
            name = "airlock_cv"
            version = "0.3"
            capabilities = ("person", "face", "teleport")
            calls = 0

            def observe(self, frame, utc):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("model hiccup")
                return [{"kind": "person_present", "confidence": 0.9, "zone_id": "desk_01"},
                        {"kind": "person_present"}]  # malformed: no zone

        p = PluginProvider(Dhruv(), kind_map={"person_present": "person_in_zone"})
        self.assertEqual(p.capabilities, frozenset(("person", "face")))
        self.assertEqual(p.describe()["ignored_capabilities"], ["teleport"])
        out = p.observe(FakeImage(), "2026-11-07T00:00:00.000Z")
        self.assertEqual([o.kind for o in out], ["person_in_zone"])
        self.assertEqual(p.malformed, 1)
        self.assertEqual(p.observe(FakeImage(), "x"), [])
        self.assertEqual(p.errors, 1)


class TestDetectNetPersonProvider(unittest.TestCase):
    """No Jetson here: a fake jetson_inference proves zone overlap -> person_in_zone mapping."""

    def test_zone_mapping_and_capability(self):
        import types
        try:
            import numpy as np
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("needs numpy + cv2")

        class D(object):
            def __init__(self, cid, conf, l, t, r, b):
                self.ClassID, self.Confidence, self.Left, self.Top, self.Right, self.Bottom = cid, conf, l, t, r, b

        class Net(object):
            def __init__(self, model, threshold=0.5):
                pass

            def Detect(self, img, overlay="none"):
                return [D(1, 0.88, 320, 120, 640, 480), D(1, 0.95, 0, 0, 40, 40), D(3, 0.99, 300, 200, 400, 300)]

            def GetClassDesc(self, cid):
                return {1: "person", 3: "car"}[cid]

        ji = types.ModuleType("jetson_inference")
        ji.detectNet = Net
        ju = types.ModuleType("jetson_utils")
        ju.cudaFromNumpy = lambda a: a
        sys.modules["jetson_inference"], sys.modules["jetson_utils"] = ji, ju
        try:
            from locki_edge.observers import DetectNetPersonProvider, Zone
            p = DetectNetPersonProvider([Zone("desk_01", 0.4, 0.3, 1.0, 1.0), Zone("door", 0.0, 0.0, 0.05, 0.05)])
            self.assertEqual(p.capabilities, frozenset(("person",)))
            obs = p.observe(np.zeros((480, 640, 3), np.uint8), "2026-11-07T00:00:00.000Z")
            by_zone = {o.zone_id: o for o in obs}
            self.assertEqual(sorted(by_zone), ["desk_01", "door"])
            self.assertEqual(by_zone["desk_01"].kind, "person_in_zone")
            self.assertAlmostEqual(by_zone["desk_01"].confidence, 0.88)
            self.assertEqual(by_zone["desk_01"].metadata["bbox"], [0.5, 0.25, 1.0, 1.0])
            self.assertAlmostEqual(by_zone["door"].confidence, 0.95)   # the car never counts
        finally:
            del sys.modules["jetson_inference"], sys.modules["jetson_utils"]


class TestEnvExample(unittest.TestCase):
    def test_checked_in_example_parses_and_has_no_secrets(self):
        from locki_edge.config import parse_env_file
        path = os.path.join(ROOT, "airlock.env.example")
        values = parse_env_file(path)
        # The template's contract path is relative to edge/jetson, the service's working directory.
        values["AIRLOCK_CONTRACT_FILE"] = os.path.join(ROOT, values["AIRLOCK_CONTRACT_FILE"])
        self.assertEqual(values["AIRLOCK_EDGE_RUNTIME"], "mac_dev")
        self.assertEqual(values["AIRLOCK_DATABASE"], "locki-w91bw")
        self.assertEqual(values["AIRLOCK_EDGE_AUTH"] if "AIRLOCK_EDGE_AUTH" in values else "", "")
        with open(path) as f:
            text = f.read()
        self.assertNotIn("eyJ", text)          # no JWT pasted in by accident
        cfg = EdgeConfig(dict(values, AIRLOCK_EDGE_AUTH_FILE="/nonexistent"), platform_name="macos")
        self.assertFalse(cfg.publish)          # placeholder config never publishes


# ---------------------------------------------------------------------------------- publisher / outbox
class TestClassify(unittest.TestCase):
    def r(self, outcome="http", code=None, body=""):
        return RawResult(outcome, code, body, "", 1.0)

    def test_policy_table(self):
        cases = [
            (self.r(code=200), False, "accepted"),
            (self.r(code=530, body="Not an enrolled member of this workstation"), False, "rejected"),
            (self.r(code=401), False, "rejected"),
            (self.r(code=400, body="invalid arguments"), False, "rejected"),
            (self.r(code=503), False, "retryable"),
            (self.r(code=429), False, "retryable"),
            (self.r(code=502), False, "ambiguous"),
            (self.r(outcome="unreachable"), False, "retryable"),
            (self.r(outcome="ambiguous_network"), False, "ambiguous"),
            (self.r(code=530, body="duplicate event_id"), True, "duplicate"),
            (self.r(code=530, body="duplicate event_id"), False, "rejected"),
            (self.r(code=530, body="stale seq: 5 <= last_seq 5"), True, "duplicate"),
        ]
        for raw, prior, want in cases:
            self.assertEqual(classify(raw, prior)[0], want, (raw, prior))
        self.assertEqual(classify(self.r(code=530, body="Not an enrolled member of this workstation"))[1], "auth")


class TestOutbox(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "outbox.sqlite3")
        self.fakes = []

    def tearDown(self):
        for f in self.fakes:
            f.close()
        shutil.rmtree(self.d)

    def fake(self, **kw):
        f = FakeStdb(**kw)
        self.fakes.append(f)
        return f

    def outbox(self, url, token="device-token", timeout=2.0, **kw):
        pub = SpacetimePublisher(StdbClient(url, "locki-w91bw", token, timeout=timeout), C.Contract())
        kw.setdefault("backoff_s", (0.01,))
        return DurableOutbox(self.db, pub, C.Contract(), **kw)

    def drain(self, ob, n=200):
        out = []
        for _ in range(n):
            r = ob.pump_once()
            if r is not None:
                out.append(r)
            elif ob.pending_count() == 0:
                break
            else:
                time.sleep(0.012)
        return out

    def test_wire_format_matches_deployed_reducer(self):
        f = self.fake()
        ob = self.outbox(f.url)
        e = make(Seq())
        ob.enqueue(e)
        (r,) = self.drain(ob)
        self.assertEqual(r.status, "accepted")
        args = f.events[e.event_id]
        self.assertEqual(dict(zip([p[0] for p in C.INGEST_PARAMS], args))["seq"], e.seq)
        self.assertEqual(f.calls, ["/v1/database/locki-w91bw/call/ingest_device_event"])
        ob.close()

    def test_outage_retries_with_no_false_success_then_delivers_in_order(self):
        url = free_port_url()  # nothing listening: transport outage
        ob = self.outbox(url)
        seq = Seq()
        evs = [make(seq) for _ in range(3)]
        for e in evs:
            ob.enqueue(e)
        receipts = [ob.pump_once() for _ in range(3)]
        receipts = [r for r in receipts if r is not None]
        self.assertTrue(receipts)
        self.assertTrue(all(not r.delivered and r.status == "retryable" for r in receipts))
        self.assertEqual(ob.stats()["delivered"], 0)
        self.assertEqual(ob.pending_count(), 3)
        for _ in range(3):
            ob.pump_once()
        self.assertIn(ob.link_state(), ("offline", "degraded"))
        ob.close()
        # server comes back on a new URL (same DB file): everything delivers, in seq order
        f = self.fake()
        ob2 = self.outbox(f.url)
        self.drain(ob2)
        self.assertEqual([a[6] for a in f.events.values()], [e.seq for e in evs])
        self.assertEqual(ob2.stats()["delivered"], 3)
        self.assertEqual(ob2.link_state(), "online")
        ob2.close()

    def test_timeout_then_duplicate_counts_as_delivered_once(self):
        f = self.fake(script=["sleep"], delay_s=0.6)   # first call commits but answers after the timeout
        ob = self.outbox(f.url, timeout=0.2)
        e = make(Seq())
        ob.enqueue(e)
        first = ob.pump_once()
        self.assertEqual(first.status, "ambiguous")
        self.assertFalse(first.delivered)
        time.sleep(0.7)
        second = self.drain(ob)[-1]
        self.assertEqual(second.status, "duplicate")
        self.assertTrue(second.delivered)
        self.assertEqual(len(f.events), 1)               # the server holds exactly one copy
        self.assertEqual(ob.event_state(e.event_id)["state"], "delivered")
        ob.close()

    def test_auth_rejection_is_final_and_reported(self):
        f = self.fake(token="the-real-device-token")
        ob = self.outbox(f.url, token="someone-elses-token")
        e = make(Seq())
        ob.enqueue(e)
        (r,) = self.drain(ob)
        self.assertEqual((r.status, r.reason, r.delivered, r.retry_eligible), ("rejected", "auth", False, False))
        self.assertEqual(ob.link_state(), "auth_rejected")
        self.assertEqual(len(f.events), 0)
        ob.close()

    def test_rejected_event_does_not_block_the_queue(self):
        f = self.fake(script=[400])
        ob = self.outbox(f.url)
        seq = Seq()
        a, b = make(seq), make(seq)
        ob.enqueue(a)
        ob.enqueue(b)
        receipts = self.drain(ob)
        self.assertEqual([r.status for r in receipts], ["rejected", "accepted"])
        ob.close()

    def test_local_dedupe_and_bounded_queue(self):
        ob = self.outbox(free_port_url(), max_pending=5)
        seq = Seq()
        e = make(seq)
        self.assertTrue(ob.enqueue(e))
        self.assertFalse(ob.enqueue(e))                  # same event_id twice: ignored
        for _ in range(10):
            ob.enqueue(make(seq))
        self.assertEqual(ob.pending_count(), 5)
        self.assertEqual(ob.stats()["dropped_overflow"], 6)
        self.assertEqual(ob.counter("dedupe_ignored"), 1)
        ob.close()

    def test_crash_mid_send_is_recovered_as_ambiguous(self):
        f = self.fake()
        ob = self.outbox(f.url)
        e = make(Seq())
        ob.enqueue(e)
        ob.db.execute("UPDATE outbox SET state='in_flight' WHERE event_id=?", (e.event_id,))
        f.events[e.event_id] = ["already committed before the crash"]
        f.last_seq = e.seq
        ob.close()
        ob2 = self.outbox(f.url)                           # restart
        self.assertEqual(ob2.event_state(e.event_id)["ambiguous"], 1)
        (r,) = self.drain(ob2)
        self.assertEqual(r.status, "duplicate")
        self.assertTrue(r.delivered)
        ob2.close()

    def test_stale_events_expire_instead_of_being_reported_delivered(self):
        ob = self.outbox(free_port_url(), max_age_s=0.05)
        ob.enqueue(make(Seq()))
        time.sleep(0.1)
        self.assertIsNone(ob.pump_once())
        s = ob.stats()
        self.assertEqual((s["expired"], s["delivered"], s["pending"]), (1, 0, 0))
        ob.close()


# ---------------------------------------------------------------------------------- config / auth
class TestConfigAuth(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.token_path = os.path.join(self.d, "device_token")
        # a JWT-shaped token whose payload claims hex_identity "ab12"
        payload = "eyJoZXhfaWRlbnRpdHkiOiJhYjEyIn0"
        with open(self.token_path, "w") as f:
            f.write("x.%s.y" % payload)
        os.chmod(self.token_path, 0o600)

    def tearDown(self):
        shutil.rmtree(self.d)

    def cfg(self, platform_name="macos", **over):
        v = {"AIRLOCK_EDGE_RUNTIME": "mac_dev", "AIRLOCK_DEVICE_ID": "mac-dev-cam-01",
             "AIRLOCK_WORKSTATION_ID": "ws-1", "AIRLOCK_EDGE_AUTH_FILE": self.token_path,
             "AIRLOCK_STATE_DIR": self.d}
        v.update(over)
        return EdgeConfig(v, platform_name=platform_name)

    def test_mac_capture_can_never_be_labeled_jetson(self):
        with self.assertRaises(ConfigError):
            self.cfg(AIRLOCK_EDGE_RUNTIME="jetson", platform_name="macos")
        with self.assertRaises(ConfigError):
            self.cfg(AIRLOCK_EDGE_RUNTIME="mac_dev", platform_name="jetson")
        self.assertEqual(self.cfg(AIRLOCK_EDGE_RUNTIME="jetson", platform_name="jetson").runtime, "jetson")

    def test_opencv_dnn_needs_its_model_files(self):
        with self.assertRaises(ConfigError):
            self.cfg(AIRLOCK_OBSERVER="opencv_dnn", AIRLOCK_DNN_PROTOTXT=os.path.join(self.d, "missing.prototxt"))
        proto, model = os.path.join(self.d, "m.prototxt"), os.path.join(self.d, "m.caffemodel")
        for f in (proto, model):
            open(f, "w").close()
        c = self.cfg(AIRLOCK_OBSERVER="opencv_dnn", AIRLOCK_DNN_PROTOTXT=proto, AIRLOCK_DNN_MODEL=model)
        self.assertEqual((c.observer, c.dnn_prototxt, c.dnn_model), ("opencv_dnn", proto, model))

    def test_min_person_area_is_a_frame_fraction(self):
        self.assertEqual(self.cfg().min_person_area, 0.0)
        self.assertEqual(self.cfg(AIRLOCK_MIN_PERSON_AREA="0.12").min_person_area, 0.12)
        for bad in ("1", "1.5", "-0.1"):
            with self.assertRaises(ConfigError):
                self.cfg(AIRLOCK_MIN_PERSON_AREA=bad)

    def test_world_readable_token_is_refused(self):
        os.chmod(self.token_path, 0o644)
        with self.assertRaises(ConfigError):
            self.cfg()

    def test_owner_identity_is_refused_no_admin_fallback(self):
        with self.assertRaises(ConfigError):
            self.cfg(AIRLOCK_FORBIDDEN_IDENTITIES="ffff,AB12")

    def test_owner_identity_copied_from_sql_with_0x_prefix_is_still_refused(self):
        with self.assertRaises(ConfigError):   # my_workstations.owner prints as 0x...; the JWT claim has no prefix
            self.cfg(AIRLOCK_FORBIDDEN_IDENTITIES=" 0xAB12 ")
        self.assertTrue(self.cfg(AIRLOCK_FORBIDDEN_IDENTITIES="0xffff").token)

    def test_missing_token_means_local_only_not_some_other_credential(self):
        c = self.cfg(AIRLOCK_EDGE_AUTH_FILE=os.path.join(self.d, "nope"))
        self.assertFalse(c.publish)
        self.assertIsNone(c.token)
        self.assertIn("missing", c.publish_reason)
        with self.assertRaises(ConfigError):
            self.cfg(AIRLOCK_EDGE_AUTH_FILE=os.path.join(self.d, "nope"), AIRLOCK_PUBLISH="on")

    def test_public_config_never_contains_the_token(self):
        c = self.cfg()
        self.assertTrue(c.publish)
        self.assertEqual(c.identity, "ab12")
        self.assertNotIn(c.token, json.dumps(c.public()))


class TestSeqFloor(unittest.TestCase):
    def test_wiped_state_dir_resumes_from_server_last_seq_not_the_clock(self):
        """The reducer rejects seq <= last_seq and a large jump above it (live 07:08Z, clock-seeded seq)."""
        d = tempfile.mkdtemp()
        tok = os.path.join(d, "tok")
        with open(tok, "w") as f:
            f.write("t")
        os.chmod(tok, 0o600)
        state = os.path.join(d, "state")

        class Sql(object):
            last = 7

            def sql_rows(self, q):
                if "my_devices" in q:
                    return 200, [{"id": "jetson-01", "workstation_id": "ws-1", "last_seq": Sql.last}], ""
                return 200, [], ""
        try:
            cfg = EdgeConfig({"AIRLOCK_EDGE_RUNTIME": "jetson", "AIRLOCK_DEVICE_ID": "jetson-01",
                              "AIRLOCK_WORKSTATION_ID": "ws-1", "AIRLOCK_EDGE_AUTH_FILE": tok,
                              "AIRLOCK_STATE_DIR": state}, platform_name="jetson")
            mk = lambda: EdgeRuntime(cfg, capture=CaptureSource(0, "jetson", opener=FakeCamera().opener),
                                     provider=FixtureProvider({}), client=Sql())
            self.assertEqual(mk().seq.next(), 8)
            Sql.last = 8                           # the server accepted seq 8
            shutil.rmtree(state)                   # operator wipes the state dir
            self.assertEqual(mk().seq.next(), 9)   # strictly above last_seq, and no clock-sized jump
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------------- runtime e2e
class TestRuntimeEndToEnd(unittest.TestCase):
    """Fake camera + fixture observer + fake SpacetimeDB: the whole wiring, including camera loss."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.fake = FakeStdb()
        token_path = os.path.join(self.d, "device_token")
        with open(token_path, "w") as f:
            f.write("device-token")
        os.chmod(token_path, 0o600)
        self.cfg = EdgeConfig({"AIRLOCK_EDGE_RUNTIME": "jetson", "AIRLOCK_DEVICE_ID": "jetson-01",
                               "AIRLOCK_WORKSTATION_ID": "ws-1", "AIRLOCK_EDGE_AUTH_FILE": token_path,
                               "AIRLOCK_SERVER": self.fake.url, "AIRLOCK_STATE_DIR": os.path.join(self.d, "state"),
                               "AIRLOCK_UNAVAILABLE_AFTER_S": "0.4", "AIRLOCK_HEALTH_INTERVAL_S": "0.5",
                               "AIRLOCK_EXIT_S": "0.3"}, platform_name="jetson")

    def tearDown(self):
        self.fake.close()
        shutil.rmtree(self.d)

    def test_person_then_unplug_then_shutdown(self):
        cam = FakeCamera()
        capture = CaptureSource("/dev/video0", "jetson", stall_after_s=0.3, reconnect_backoff_s=(0.05,),
                                opener=cam.opener)
        provider = FixtureProvider(lambda i: [("person_in_zone", 0.91, "desk_01")] if i >= 5 else [],
                                   capabilities=("person",))
        rt = EdgeRuntime(self.cfg, capture=capture, provider=provider).start(with_api=False)
        loop = threading.Thread(target=rt.run)   # main frame loop; run() ends with shutdown()
        loop.start()
        try:
            self.assertTrue(wait_for(lambda: rt.events.get("person_in_zone") == 1, 5))
            # health is sampled at 4 Hz: let the monitor report "streaming" (sent as "online") before unplugging
            self.assertTrue(wait_for(lambda: ["camera", "online"] in [h[2:4] for h in self.fake.health], 5))
            cam.plugged = False                                     # unplug while a person is present
            self.assertTrue(wait_for(lambda: rt.events.get("camera_unavailable") == 1, 5))
            time.sleep(0.6)
            self.assertNotIn("person_left_zone", rt.events)          # no fake clear during the outage
            self.assertTrue(wait_for(lambda: len(self.fake.events) == 2, 5))
            h = rt.health()
            self.assertEqual(h["status"], "camera_unavailable")
            self.assertEqual(h["config"]["runtime"], "jetson")
        finally:
            rt.request_stop()
            loop.join(15)
        self.assertFalse(loop.is_alive())
        self.assertTrue(rt.shutdown_done)
        kinds = sorted(a[4] for a in self.fake.events.values())
        self.assertEqual(kinds, ["camera_unavailable", "person_in_zone"])
        for args in self.fake.events.values():
            typed = dict(zip([p[0] for p in C.INGEST_PARAMS], args))
            self.assertEqual(typed["device_id"], "jetson-01")
            self.assertTrue(typed["simulated"])                     # fixture observer => simulated, always
            md = json.loads(typed["metadata_json"])
            self.assertEqual(md["capture_source"], "jetson_usb_camera")
            self.assertEqual(md["runtime"], "jetson")
        statuses = [(h[2], h[3]) for h in self.fake.health]
        # deployed module (role "jetson"): only component "camera", status in online/degraded/offline terms
        self.assertIn(("camera", "online"), statuses)
        self.assertIn(("camera", "offline"), statuses)
        self.assertEqual(statuses[-1], ("camera", "offline"))
        self.assertNotIn("edge", [c for c, _ in statuses])
        details = [h[4] for h in self.fake.health if h[2] == "camera"]
        self.assertTrue(any(d.startswith("state=streaming ") for d in details))
        self.assertTrue(any(d.startswith("state=disconnected ") for d in details))
        self.assertEqual(rt.health_results["edge"]["result"], "not_sent")
        with open(os.path.join(self.d, "state", "events.jsonl")) as f:
            audit = f.read()
        for word in ("jpeg", "embedding", "image"):
            self.assertNotIn(word, audit)


class TestOpenCvDnnPersonProvider(unittest.TestCase):
    """Fake cv2.dnn net: only VOC class 15 above threshold becomes a person box mapped to its zone."""

    def test_person_only_threshold_and_zone(self):
        try:
            import numpy as np
            import cv2
        except ImportError:
            self.skipTest("needs numpy + cv2")
        from unittest import mock

        dets = np.zeros((1, 1, 4, 7), np.float32)
        dets[0, 0, 0] = [0, 15, 0.82, 0.5, 0.25, 1.0, 1.0]   # person in desk zone
        dets[0, 0, 1] = [0, 15, 0.30, 0.0, 0.0, 0.04, 0.04]  # person below threshold
        dets[0, 0, 2] = [0, 7, 0.99, 0.4, 0.4, 0.9, 0.9]     # car: never counts
        dets[0, 0, 3] = [0, 15, 0.91, -0.1, -0.1, 0.05, 0.05]  # person in door zone, clamped to frame

        class Net(object):
            def setInput(self, blob):
                self.blob = blob

            def forward(self):
                return dets

        # create=True: some OpenCV wheels (the Mac venv's) ship without the dnn functions.
        with mock.patch.object(cv2.dnn, "readNetFromCaffe", return_value=Net(), create=True), \
                mock.patch.object(cv2.dnn, "blobFromImage", side_effect=lambda img, *a: img, create=True):
            from locki_edge.observers import OpenCvDnnPersonProvider, Zone
            p = OpenCvDnnPersonProvider([Zone("desk_01", 0.4, 0.3, 1.0, 1.0), Zone("door", 0.0, 0.0, 0.05, 0.05)],
                                        "x.prototxt", "x.caffemodel", threshold=0.5)
            obs = p.observe(np.zeros((480, 640, 3), np.uint8), "2026-11-08T00:00:00.000Z")
        by_zone = {o.zone_id: o for o in obs}
        self.assertEqual(p.capabilities, frozenset(("person",)))
        self.assertEqual(sorted(by_zone), ["desk_01", "door"])
        self.assertEqual(by_zone["desk_01"].kind, "person_in_zone")
        self.assertAlmostEqual(by_zone["desk_01"].confidence, 0.82, places=5)
        self.assertEqual(by_zone["desk_01"].metadata["bbox"], [0.5, 0.25, 1.0, 1.0])
        self.assertAlmostEqual(by_zone["door"].confidence, 0.91, places=5)
        self.assertEqual(by_zone["door"].metadata["bbox"], [0.0, 0.0, 0.05, 0.05])
        self.assertEqual(p.version, "opencv-dnn/mobilenet-ssd-voc")

        # min_area: the door-zone person (box area 0.0025) is a far-away passerby and is ignored.
        with mock.patch.object(cv2.dnn, "readNetFromCaffe", return_value=Net(), create=True), \
                mock.patch.object(cv2.dnn, "blobFromImage", side_effect=lambda img, *a: img, create=True):
            p2 = OpenCvDnnPersonProvider([Zone("desk_01", 0.4, 0.3, 1.0, 1.0), Zone("door", 0.0, 0.0, 0.05, 0.05)],
                                         "x.prototxt", "x.caffemodel", threshold=0.5, min_area=0.05)
            obs2 = p2.observe(np.zeros((480, 640, 3), np.uint8), "2026-11-08T00:00:00.000Z")
        self.assertEqual([o.zone_id for o in obs2], ["desk_01"])


if __name__ == "__main__":
    unittest.main()
