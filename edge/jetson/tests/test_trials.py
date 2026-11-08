"""trials.py scores the runtime's audit log (ISO observed_at, local-only records), not just the legacy service's."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
sys.path.insert(0, HERE)

import trials  # noqa: E402
from locki_edge.contract import utc_iso_ms  # noqa: E402

T0 = 1794105000000  # 2026-11-08T02:30:00Z


class TrialsRuntimeAuditTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, name, rows):
        with open(os.path.join(self.d, name), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_runtime_published_and_local_only_records_are_scored(self):
        self.write("trials.jsonl", [
            {"trial": 1, "label": "at_desk", "start_ms": T0, "end_ms": T0 + 8000, "note": ""},
            {"trial": 2, "label": "at_desk", "start_ms": T0 + 20000, "end_ms": T0 + 28000, "note": ""},
            {"trial": 3, "label": "empty", "start_ms": T0 + 40000, "end_ms": T0 + 48000, "note": ""}])
        self.write("events.jsonl", [
            # published runtime event: SecurityEventV1._asdict(), ISO observed_at only
            {"kind": "person_in_zone", "observed_at": utc_iso_ms(T0 + 2345), "confidence": 0.81,
             "metadata": {"provider": "detectnet", "zone_id": "desk_01"}, "event_id": "x", "seq": 1},
            # disarmed: kept local, still a detection
            {"local_only": True, "reason": "not_armed", "kind": "person_in_zone",
             "observed_at": utc_iso_ms(T0 + 21000), "observed_at_ms": T0 + 21000, "confidence": 0.7,
             "metadata": {"provider": "detectnet"}},
            {"local_only": True, "reason": "kind_not_in_contract", "kind": "person_cleared",
             "observed_at": utc_iso_ms(T0 + 44000), "observed_at_ms": T0 + 44000, "confidence": None, "metadata": {}}])
        rows = trials.score(trials.load_jsonl(os.path.join(self.d, "trials.jsonl")),
                            trials.load_jsonl(os.path.join(self.d, "events.jsonl")), 1500)
        self.assertEqual([(r["detected"], r["latency_ms"], r["detector"], r["sent"]) for r in rows],
                         [(True, 2345, "detectnet", "sent"), (True, 1000, "detectnet", "local:not_armed"),
                          (False, None, None, None)])
        s = trials.summarize(rows)
        self.assertEqual((s["tp"], s["fn"], s["fp"], s["tn"], s["recall"]), (2, 0, 0, 1, 1.0))

    def test_event_ms_handles_every_shape(self):
        self.assertEqual(trials.event_ms({"observed_at": T0}), T0)                       # legacy int
        self.assertEqual(trials.event_ms({"observed_at": utc_iso_ms(T0 + 7)}), T0 + 7)   # runtime ISO
        self.assertEqual(trials.event_ms({"observed_at": "2026-11-08T02:30:00Z"}), T0)
        self.assertIsNone(trials.event_ms({"observed_at": "garbage"}))
        self.assertIsNone(trials.event_ms({"observed_at": True}))


if __name__ == "__main__":
    unittest.main()
