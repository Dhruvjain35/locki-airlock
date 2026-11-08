"""Armed-session discovery (the module only accepts camera events inside the live armed session)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from locki_edge.capture import CaptureSource  # noqa: E402
from locki_edge.config import EdgeConfig  # noqa: E402
from locki_edge.observers import FixtureProvider  # noqa: E402
from locki_edge.runtime import EdgeRuntime  # noqa: E402
from locki_edge.session import SessionTracker  # noqa: E402
from test_runtime_failures import FakeCamera, FakeStdb, wait_for  # noqa: E402

NOW = 1794100000.0


class FakeSql(object):
    def __init__(self, active="s-1", state="armed", expires_ms=0, fail=False):
        self.active, self.state, self.expires_ms, self.fail, self.queries = active, state, expires_ms, fail, []

    def sql_rows(self, q):
        self.queries.append(q)
        if self.fail:
            return None, [], "unreachable"
        if "my_workstations" in q:
            return 200, [{"id": "ws-1", "active_session_id": self.active}], ""
        if "my_sessions" in q:
            return 200, ([{"id": self.active, "state": self.state, "expires_at_ms": self.expires_ms,
                           "disarmed_at_ms": 0}] if self.active else []), ""
        return 200, [], ""


def tracker(sql, **kw):
    t = {"now": NOW}
    tr = SessionTracker(sql, "ws-1", clock=lambda: t["now"], **kw)
    return tr, t


class SessionTrackerTest(unittest.TestCase):
    def test_armed_session_found(self):
        tr, _ = tracker(FakeSql())
        self.assertEqual(tr.poll_once(), ("s-1", True))
        self.assertEqual(tr.describe()["mode"], "auto")

    def test_disarmed_and_expired_and_non_armed_state(self):
        self.assertEqual(tracker(FakeSql(active=""))[0].poll_once(), ("", False))
        self.assertEqual(tracker(FakeSql(expires_ms=int(NOW * 1000) - 1))[0].poll_once(), ("s-1", False))
        self.assertEqual(tracker(FakeSql(state="disarmed"))[0].poll_once(), ("s-1", False))

    def test_unknown_live_state_counts_as_armed_known_inactive_does_not(self):
        tr, _ = tracker(FakeSql(state="ACTIVE"))
        self.assertEqual(tr.poll_once(), ("s-1", True))
        self.assertIn("assumed live", tr.describe()["detail"])
        self.assertEqual(tracker(FakeSql(state="expired"))[0].poll_once(), ("s-1", False))

    def test_transient_failure_keeps_last_answer_until_stale(self):
        sql = FakeSql()
        changes = []
        tr, t = tracker(sql, stale_after_s=30, on_change=lambda *a: changes.append(a))
        tr.poll_once()
        sql.fail = True
        t["now"] += 10
        self.assertEqual(tr.poll_once(), ("s-1", True))
        self.assertIn("unreachable", tr.describe()["error"])
        t["now"] += 31
        self.assertEqual(tr.poll_once(), ("", False))
        self.assertEqual([c[1] for c in changes], [True, False])

    def test_static_mode_never_queries(self):
        sql = FakeSql()
        tr, _ = tracker(sql, static_session_id="fixed")
        self.assertEqual(tr.poll_once(), ("fixed", True))
        self.assertEqual(sql.queries, [])

    def test_unsafe_ids_are_never_interpolated(self):
        sql = FakeSql(active="x' OR '1'='1")
        tr, _ = tracker(sql)
        self.assertEqual(tr.poll_once(), ("", False))
        self.assertEqual(len(sql.queries), 1)   # my_sessions never queried with the hostile id
        bad = SessionTracker(FakeSql(), "ws'1")
        self.assertEqual(bad.poll_once(), ("", False))


class RuntimeSessionTest(unittest.TestCase):
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
                               "AIRLOCK_SESSION_POLL_S": "0.1", "AIRLOCK_EXIT_S": "0.2", "AIRLOCK_COOLDOWN_S": "0",
                               "AIRLOCK_HEALTH_INTERVAL_S": "60"}, platform_name="jetson")

    def tearDown(self):
        self.fake.close()
        shutil.rmtree(self.d)

    def test_disarmed_stays_local_then_arming_publishes_with_live_session_id(self):
        self.fake.active_session = ""                       # workstation disarmed
        present = {"on": True}
        cam = FakeCamera()
        capture = CaptureSource("/dev/video0", "jetson", stall_after_s=1.0, reconnect_backoff_s=(0.05,),
                                opener=cam.opener)
        provider = FixtureProvider(lambda i: [("person_in_zone", 0.9, "desk_01")] if present["on"] else [],
                                   capabilities=("person",))
        rt = EdgeRuntime(self.cfg, capture=capture, provider=provider).start(with_api=False)
        loop = threading.Thread(target=rt.run)
        loop.start()
        try:
            self.assertTrue(wait_for(lambda: rt.local_only.get("not_armed") == 1, 5))
            self.assertEqual(self.fake.events, {})          # nothing sent while disarmed
            self.fake.active_session = "ws-session-9"       # owner arms (Dhruv's action, not the edge's)
            self.assertTrue(wait_for(lambda: rt.session.current() == ("ws-session-9", True), 5))
            present["on"] = False                           # walk out ...
            self.assertTrue(wait_for(lambda: "person_left_zone" in rt.events, 5))
            present["on"] = True                            # ... and back in while armed
            entered = lambda: [a for a in self.fake.events.values() if a[4] == "person_in_zone"]  # noqa: E731
            self.assertTrue(wait_for(lambda: len(entered()) == 1, 5))
        finally:
            rt.request_stop()
            loop.join(timeout=10)
        sent = entered()[0]
        self.assertEqual((sent[1], sent[4], sent[9]), ("ws-session-9", "person_in_zone", True))
        self.assertTrue(all(a[1] == "ws-session-9" for a in self.fake.events.values()))
        audit = [json.loads(l) for l in open(os.path.join(self.cfg.state_dir, "events.jsonl"))]
        self.assertEqual(audit[0]["reason"], "not_armed")
        self.assertTrue(audit[0]["local_only"])
        self.assertEqual(rt.health_results.get("camera", {}).get("result"), "accepted")


if __name__ == "__main__":
    unittest.main()
