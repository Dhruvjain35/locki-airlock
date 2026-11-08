"""Presence re-asserts (correlation window), the person_cleared exit kind, and the camera_offline/online kinds.
Hermetic: synthetic clocks, no network.

Sid's module pairs an unknown USB with a person_in_zone only if |delta observedAt| <= 10 s (policy.ts correlates()),
so while a zone stays occupied the edge re-sends person_in_zone every AIRLOCK_PRESENCE_REASSERT_S. The deployed
module refuses person_left_zone and camera_unavailable for role jetson; they go out as person_cleared and
camera_offline (+ camera_online when an announced outage ends), each only once the contract's kinds list it.

    cd edge/jetson && python3 -m unittest tests.test_presence_reassert -v
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from locki_edge import contract as C  # noqa: E402
from locki_edge.capture import STATE_DISCONNECTED, STATE_STREAMING, Frame  # noqa: E402
from locki_edge.config import ConfigError, EdgeConfig  # noqa: E402
from locki_edge.normalizer import EventNormalizer  # noqa: E402
from locki_edge.observers import FixtureProvider, VisionObservation  # noqa: E402
from locki_edge.runtime import EdgeRuntime  # noqa: E402
from locki_edge.stdb import PublishReceipt, RawResult  # noqa: E402

FPS = 30.0
T0_MS = 1794100000000
FRAME_MS = 1000.0 / FPS
LIVE_CONTRACT = os.path.join(ROOT, "contract", "locki-w91bw.live.json")


def run(norm, present, total, start=0, conf=0.8, t0=1000.0, kind="person_in_zone"):
    """Feed frames start..total-1 at 30 fps. `present(i)` (or a set) says whether the person is in desk_01."""
    events = []
    for i in range(start, total):
        seen = present(i) if callable(present) else i in present
        c = conf(i) if callable(conf) else conf
        obs = [VisionObservation(kind, c, "desk_01", "", "v", {"bbox": [0.1, 0.1, 0.5, 0.9]})] if seen else []
        events += norm.process(obs, int(T0_MS + i * FRAME_MS), t0 + i / FPS)
    return events


def normalizer(**kw):
    kw.setdefault("enter_frames", 3)
    kw.setdefault("exit_s", 2.0)
    return EventNormalizer(FixtureProvider({}, capabilities=("person",)), ["desk_01"], **kw)


class ReassertNormalizerTest(unittest.TestCase):
    def test_reasserts_every_5s_while_occupied_then_one_clear(self):
        norm = normalizer(reassert_s=5.0)
        events = run(norm, set(range(10, 10 + 22 * 30)), 10 + 25 * 30)   # 22 s visit, then empty
        self.assertEqual([e.kind for e in events], ["person_in_zone"] * 5 + ["person_cleared"])
        self.assertEqual([e.reassert for e in events], [False, True, True, True, True, False])
        ins = [e.observed_at_ms for e in events if e.kind == "person_in_zone"]
        self.assertEqual(ins[0], int(T0_MS + 10 * FRAME_MS))               # enter = first frame of the streak
        for a, b in zip(ins, ins[1:]):
            self.assertAlmostEqual(b - a, 5000, delta=FRAME_MS + 1)        # cadence measured in observed time
        self.assertEqual(norm.counters["reasserts"], 4)
        self.assertEqual(norm.counters["rate_limited"], 0)
        r = events[1]
        self.assertEqual(r.metadata["observation"], "presence")
        self.assertEqual(r.metadata["zone_id"], "desk_01")
        self.assertAlmostEqual(r.metadata["dwell_ms"], r.observed_at_ms - ins[0], delta=1)
        self.assertGreater(r.metadata["frames_confirmed"], 140)

    def test_observed_at_is_the_latest_frame_seen_and_confidence_the_current_streak(self):
        norm = normalizer(reassert_s=5.0)
        conf = lambda i: 0.6 if i < 60 else 1.0  # noqa: E731
        events = run(norm, set(range(0, 200)), 200, conf=conf)
        enter, re1 = [e for e in events if e.kind == "person_in_zone"]
        self.assertTrue(re1.reassert)
        frame = int(round((re1.observed_at_ms - T0_MS) / FRAME_MS))
        self.assertEqual(re1.observed_at_ms, int(T0_MS + frame * FRAME_MS))  # an actual frame where they were seen
        self.assertEqual(re1.captured_mono, 1000.0 + frame / FPS)            # ... and the frame that triggered it
        seen = [conf(i) for i in range(0, frame + 1)]
        self.assertEqual(re1.confidence, round(sum(seen) / len(seen), 4))
        self.assertEqual(enter.confidence, 0.6)

    def test_no_reassert_during_exit_hold_or_after_clear(self):
        norm = normalizer(reassert_s=5.0, exit_s=4.0)
        # seen 0..6.5 s, then gone: the hold covers 10 s (5 s after the re-assert) and must not re-assert
        events = run(norm, set(range(0, 195)), 30 * 40)
        self.assertEqual([(e.kind, e.reassert) for e in events],
                         [("person_in_zone", False), ("person_in_zone", True), ("person_cleared", False)])
        self.assertEqual(events[-1].observed_at_ms, int(T0_MS + 194 * FRAME_MS))
        # a new visit after the clear starts with a fresh enter, never a re-assert of the old one
        again = run(norm, set(range(1300, 1310)), 1310, start=30 * 40)
        self.assertEqual([(e.kind, e.reassert) for e in again], [("person_in_zone", False)])
        self.assertEqual(again[0].observed_at_ms, int(T0_MS + 1300 * FRAME_MS))

    def test_no_reassert_after_camera_loss(self):
        norm = normalizer(reassert_s=5.0)
        events = run(norm, set(range(0, 120)), 120)            # 4 s: enter only (its re-assert would be at 5 s)
        self.assertEqual([e.reassert for e in events], [False])
        norm.camera_lost()
        # camera back, person still there: the old visit is never re-asserted; a new confirmed enter comes first
        after = run(norm, set(range(120, 300)), 300, start=120)
        self.assertEqual([(e.kind, e.reassert) for e in after], [("person_in_zone", False), ("person_in_zone", True)])
        self.assertEqual(after[0].observed_at_ms, int(T0_MS + 120 * FRAME_MS))
        self.assertAlmostEqual(after[1].observed_at_ms - after[0].observed_at_ms, 5000, delta=FRAME_MS + 1)
        norm.camera_lost()
        self.assertEqual(run(norm, set(), 30 * 30, start=300), [])   # nobody after a loss: no re-assert, no clear
        self.assertEqual(norm.counters["reasserts"], 1)

    def test_zero_disables_reassert(self):
        norm = normalizer(reassert_s=0)
        events = run(norm, set(range(0, 30 * 30)), 30 * 33)     # 30 s visit
        self.assertEqual([e.kind for e in events], ["person_in_zone", "person_cleared"])
        self.assertEqual(norm.counters["reasserts"], 0)

    def test_reasserts_bypass_the_episode_rate_limit(self):
        norm = normalizer(reassert_s=5.0, max_episodes_per_min=1)
        events = run(norm, set(range(0, 30 * 31)), 30 * 31)     # 31 s, one budget slot
        self.assertEqual([e.reassert for e in events], [False] + [True] * 6)
        self.assertEqual(norm.counters["rate_limited"], 0)
        self.assertEqual(len(norm.episode_times), 1)            # re-asserts took no slot

    def test_no_reassert_when_the_enter_itself_was_rate_limited(self):
        norm = normalizer(reassert_s=5.0, max_episodes_per_min=1)
        first = run(norm, set(range(0, 30)), 30 * 4)            # visit 1 uses the only slot
        self.assertEqual([e.kind for e in first], ["person_in_zone", "person_cleared"])
        second = run(norm, set(range(120, 120 + 30 * 20)), 120 + 30 * 23, start=30 * 4)
        self.assertEqual(second, [])                            # enter dropped by the limit (counted), so no re-asserts
        self.assertEqual(norm.counters["rate_limited"], 1)
        self.assertEqual(norm.counters["reasserts"], 0)

    def test_constructor_rejects_out_of_range_intervals(self):
        for bad in (10, 10.0, 12, -1, -0.5, float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalizer(reassert_s=bad)
        self.assertEqual(normalizer(reassert_s=9.99).reassert_s, 9.99)
        self.assertEqual(normalizer().reassert_s, 5.0)          # on by default, same as the config default


class ReassertConfigTest(unittest.TestCase):
    BASE = {"AIRLOCK_EDGE_RUNTIME": "jetson", "AIRLOCK_PUBLISH": "off"}

    def cfg(self, **over):
        return EdgeConfig(dict(self.BASE, **over), platform_name="jetson")

    def test_default_zero_and_in_range(self):
        self.assertEqual(self.cfg().presence_reassert_s, 5.0)
        self.assertEqual(self.cfg(AIRLOCK_PRESENCE_REASSERT_S="0").presence_reassert_s, 0.0)
        self.assertEqual(self.cfg(AIRLOCK_PRESENCE_REASSERT_S="9.5").presence_reassert_s, 9.5)
        self.assertEqual(self.cfg().public()["presence_reassert_s"], 5.0)

    def test_rejects_ten_or_more_and_negative(self):
        for bad in ("10", "10.0", "15", "-1", "-0.01", "nan", "inf", "abc", ""):
            with self.assertRaises(ConfigError, msg=bad):
                self.cfg(AIRLOCK_PRESENCE_REASSERT_S=bad)


class ClearedKindTest(unittest.TestCase):
    def test_contract_defaults_carry_person_cleared_not_person_left_zone(self):
        self.assertEqual(C.KIND_CAPABILITY["person_cleared"], "person")
        self.assertNotIn("person_left_zone", C.KIND_CAPABILITY)
        self.assertIn("person_cleared", C.Contract().kinds)

    def test_normalizer_exit_is_person_cleared_on_the_wire(self):
        events = run(normalizer(reassert_s=0), set(range(0, 60)), 200)
        clear = events[-1]
        self.assertEqual(clear.kind, "person_cleared")
        self.assertEqual(clear.metadata["observation"], "absence")
        n = [0]

        def seq():
            n[0] += 1
            return n[0]
        ev = C.make_event(C.Contract(), seq, "ws-1", "s-1", "jetson-01", "jetson_usb_camera", clear.kind,
                          clear.observed_at_ms, clear.confidence, clear.metadata, False)
        self.assertEqual(C.ingest_args(ev, C.Contract())[4], "person_cleared")


# ---------------------------------------------------------------------------------- runtime (hermetic)
class FakeImage(object):
    shape = (480, 640, 3)


class ScriptedCapture(object):
    """Frames on a synthetic 30 fps clock; step() and monitor_tick() are driven by hand: no thread, no real time."""
    capture_source = "jetson_usb_camera"

    def __init__(self):
        self.i = 0
        self.mono0 = time.monotonic()
        self.state, self.state_for_s = STATE_STREAMING, 0.0

    def watchdog(self):
        return self.state

    def health(self):
        return {"state": self.state, "state_for_s": self.state_for_s, "last_error": None, "fps": 30.0,
                "frame_age_s": 0.0, "capture_source": self.capture_source}

    def wait_frame(self, timeout):
        f = Frame(FakeImage(), self.i, self.mono0 + self.i / FPS, int(T0_MS + self.i * FRAME_MS), 640, 480)
        self.i += 1
        return f

    def stop(self):
        pass


class FakeAlarm(object):
    def __init__(self):
        self.plays = 0

    def play(self, force=False):
        self.plays += 1
        return {"played": True}


class Sql(object):
    def sql_rows(self, q):
        return 200, [], ""

    def call(self, reducer, args):   # report_health from monitor_tick
        return RawResult("http", 200, "", "", 1.0)


class RuntimeReassertTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        tok = os.path.join(self.d, "tok")
        with open(tok, "w") as f:
            f.write("t")
        os.chmod(tok, 0o600)
        # Derived from the live contract so whatever the operator sets there (kinds, health) is what runs here,
        # but with person_cleared pinned in or out.
        with open(LIVE_CONTRACT) as f:
            live = json.load(f)
        base = [k for k in live["kinds"] if k != "person_cleared"]
        self.live_plus_cleared = os.path.join(self.d, "live_plus_cleared.json")
        self.live_without_cleared = os.path.join(self.d, "live_without_cleared.json")
        for path, kinds in ((self.live_plus_cleared, base + ["person_cleared"]), (self.live_without_cleared, base)):
            with open(path, "w") as f:
                json.dump(dict(live, kinds=kinds, wire_metadata_keys=[]), f)
        self.person_only = os.path.join(self.d, "person_only.json")   # the kinds accepted live so far
        with open(self.person_only, "w") as f:
            json.dump(dict(live, kinds=["person_in_zone"], wire_metadata_keys=[]), f)
        self.values = {"AIRLOCK_EDGE_RUNTIME": "jetson", "AIRLOCK_DEVICE_ID": "jetson-01",
                       "AIRLOCK_WORKSTATION_ID": "ws-1", "AIRLOCK_EDGE_AUTH_FILE": tok,
                       "AIRLOCK_STATE_DIR": os.path.join(self.d, "state"), "AIRLOCK_SESSION_ID": "ws-session-1",
                       "AIRLOCK_CONTRACT_FILE": self.live_plus_cleared, "AIRLOCK_EXIT_S": "2",
                       "AIRLOCK_LOCAL_ALARM": "on", "AIRLOCK_PUSH_URL": "https://ntfy.sh/locki-test",
                       "AIRLOCK_PUSH_MIN_INTERVAL_S": "0"}   # isolate once-per-visit from the 600 s spacing
        self.present = set()

    def tearDown(self):
        shutil.rmtree(self.d)

    def runtime(self, **over):
        self.values.update(over)
        cfg = EdgeConfig(self.values, platform_name="jetson")
        provider = FixtureProvider(lambda i: [("person_in_zone", 0.9, "desk_01")] if (i - 1) in self.present else [],
                                   capabilities=("person",))
        rt = EdgeRuntime(cfg, capture=ScriptedCapture(), provider=provider, client=Sql())
        rt.alarm = FakeAlarm()
        self.pushes = []
        if rt.push is not None:
            rt.push.send_once = lambda payload: "test-receipt"   # never reach the network
            real_on_receipt = rt.push.on_receipt

            def on_receipt(receipt):   # count what the pusher actually queues for ntfy
                result = real_on_receipt(receipt)
                if result == "queued":
                    self.pushes.append(receipt.event_id)
                return result
            rt.push.on_receipt = on_receipt
        return rt

    def frames(self, rt, n):
        for _ in range(n):
            rt.step()

    def queued(self, rt):
        """What would go on the wire: the outbox's frozen positional args, in seq order."""
        db = sqlite3.connect(os.path.join(rt.cfg.state_dir, "outbox.sqlite3"))
        try:
            return [json.loads(a) for (a,) in db.execute("SELECT args_json FROM outbox ORDER BY seq")]
        finally:
            db.close()

    def audit(self, rt):
        with open(rt.audit_path) as f:
            return [json.loads(line) for line in f]

    def test_each_reassert_is_a_new_event_next_seq_advancing_time_and_bare_wire(self):
        rt = self.runtime()
        self.present = set(range(0, 30 * 16))                   # 16 s visit, then 3 s empty
        self.frames(rt, 30 * 19)
        rt.outbox.close()
        args = self.queued(rt)
        kinds = [a[4] for a in args]
        self.assertEqual(kinds, ["person_in_zone"] * 4 + ["person_cleared"])
        ids, seqs, obs = [a[3] for a in args], [a[6] for a in args], [a[5] for a in args]
        self.assertEqual(len(set(ids)), len(ids))                # fresh event_id each time
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))   # next seq through the same counter
        self.assertEqual(rt.seq.value, seqs[-1])
        for a, b in zip(obs[:4], obs[1:4]):
            self.assertAlmostEqual(b - a, 5000, delta=FRAME_MS + 1)
        self.assertEqual(obs[4], int(T0_MS + (30 * 16 - 1) * FRAME_MS))   # clear = last frame seen
        for a in args:
            self.assertEqual(a[8], "{}")                         # live contract: wire metadata stays {}
            self.assertNotIn("reassert", json.dumps(a))          # the local flag never reaches the wire
        audit = self.audit(rt)
        self.assertEqual([r.get("reassert", False) for r in audit], [False, True, True, True, False])
        self.assertEqual([r["kind"] for r in audit], kinds)
        self.assertTrue(all("reassert" not in r["metadata"] for r in audit))
        self.assertEqual(rt.normalizer.counters["reasserts"], 3)

    def test_reassert_never_re_alarms_or_re_pages_within_a_visit(self):
        rt = self.runtime()
        self.present = set(range(0, 30 * 22)) | set(range(30 * 30, 30 * 32))   # 22 s visit, gone, 2 s visit
        self.frames(rt, 30 * 22)
        self.assertEqual(rt.normalizer.counters["reasserts"], 4)
        self.assertEqual(rt.alarm.plays, 1)                      # enter only; 4 re-asserts stayed silent
        self.frames(rt, 30 * 11)                                 # clear, then the 2nd visit (still present at end)
        self.assertEqual(rt.alarm.plays, 2)                      # the next visit's enter alarms again
        rt.outbox.close()
        args = self.queued(rt)
        self.assertEqual([a[4] for a in args], ["person_in_zone"] * 5 + ["person_cleared", "person_in_zone"])
        first_reassert = args[1]
        # a re-assert that needs a retry stays quiet when it is finally accepted
        rt._on_receipt(PublishReceipt(first_reassert[3], first_reassert[6], "person_in_zone", "retryable", False,
                                      True, 503, "", "", "http_5xx", 1, "x", 1.0))
        for a in args:
            rt._on_receipt(PublishReceipt(a[3], a[6], a[4], "accepted", True, False, 200, "", "", "ok", 1, "x", 1.0))
        self.assertEqual(len(self.pushes), 2)                    # one per visit: the two enters
        self.assertEqual(len(rt.push.events), 0)                 # final receipts released every tracked id

    def test_clear_stays_local_until_the_contract_lists_person_cleared(self):
        rt = self.runtime(AIRLOCK_CONTRACT_FILE=self.live_without_cleared, AIRLOCK_PRESENCE_REASSERT_S="0")
        self.present = set(range(0, 30 * 2))
        self.frames(rt, 30 * 5)
        rt.outbox.close()
        self.assertEqual([a[4] for a in self.queued(rt)], ["person_in_zone"])
        local = [r for r in self.audit(rt) if r.get("local_only")]
        self.assertEqual([(r["kind"], r["reason"], r["metadata"]["observation"]) for r in local],
                         [("person_cleared", "kind_not_in_contract", "absence")])
        self.assertEqual(rt.local_only, {"kind_not_in_contract": 1})

    def test_disarmed_reasserts_are_kept_local_and_marked(self):
        rt = self.runtime(AIRLOCK_SESSION_ID="")                 # static empty session = never armed
        self.present = set(range(0, 30 * 11))
        self.frames(rt, 30 * 11)
        rt.outbox.close()
        self.assertEqual(self.queued(rt), [])
        local = self.audit(rt)
        self.assertEqual([(r["reason"], r.get("reassert", False)) for r in local],
                         [("not_armed", False), ("not_armed", True), ("not_armed", True)])
        self.assertEqual(rt.alarm.plays, 0)


    # ---------------------------------------------------------------- camera kinds (module names for role jetson)
    def outage(self, rt):
        cam = rt.capture
        rt.monitor_tick()                                        # streaming at start: no camera_online
        cam.state, cam.state_for_s = STATE_DISCONNECTED, 5.0
        rt.monitor_tick()
        rt.monitor_tick()                                        # one camera_offline per outage
        cam.state, cam.state_for_s = STATE_STREAMING, 0.0
        rt.monitor_tick()
        rt.monitor_tick()                                        # one camera_online per recovery

    def test_camera_loss_is_camera_offline_and_recovery_camera_online_once_listed(self):
        with open(self.live_plus_cleared) as f:
            doc = json.load(f)
        doc["kinds"] += ["camera_offline", "camera_online"]
        with open(self.live_plus_cleared, "w") as f:
            json.dump(doc, f)
        rt = self.runtime()
        self.outage(rt)
        rt.outbox.close()
        args = self.queued(rt)
        self.assertEqual([a[4] for a in args], ["camera_offline", "camera_online"])
        self.assertEqual([a[6] for a in args], [args[0][6], args[0][6] + 1])
        self.assertEqual(rt.alarm.plays, 0)
        self.assertNotIn("camera_unavailable", C.KIND_CAPABILITY)   # the module refused that name

    def test_camera_kinds_stay_local_until_the_contract_lists_them(self):
        rt = self.runtime(AIRLOCK_CONTRACT_FILE=self.person_only)
        self.outage(rt)
        rt.outbox.close()
        self.assertEqual(self.queued(rt), [])
        self.assertEqual([(r["kind"], r["reason"], r["metadata"]["reason"]) for r in self.audit(rt)],
                         [("camera_offline", "kind_not_in_contract", "disconnected"),
                          ("camera_online", "kind_not_in_contract", "recovered")])


if __name__ == "__main__":
    unittest.main()
