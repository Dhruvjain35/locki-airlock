"""Apple Vision Mac person baseline (injected detector) + the deployed module's health vocabulary."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from locki_edge.config import ConfigError, EdgeConfig  # noqa: E402
from locki_edge.contract import Contract, ingest_args, make_event  # noqa: E402
from locki_edge.normalizer import EventNormalizer  # noqa: E402
from locki_edge.observers import AppleVisionPersonProvider, Zone  # noqa: E402

TS = "2026-11-08T00:30:00.000Z"


def provider(dets, threshold=0.5):
    return AppleVisionPersonProvider([Zone("desk_01", 0.25, 0.35, 0.75, 1.0)], threshold=threshold,
                                     detect=lambda frame: list(dets))


class AppleVisionProviderTest(unittest.TestCase):
    def test_person_in_zone_with_bbox_and_overlap(self):
        obs = provider([((0.3, 0.4, 0.7, 0.95), 0.71)]).observe(None, TS)
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertEqual((o.kind, o.zone_id, o.confidence, o.captured_at_utc), ("person_in_zone", "desk_01", 0.71, TS))
        self.assertEqual(o.metadata["bbox"], [0.3, 0.4, 0.7, 0.95])
        self.assertEqual(o.metadata["zone_overlap"], 1.0)

    def test_below_threshold_or_outside_zone_is_nothing(self):
        self.assertEqual(provider([((0.3, 0.4, 0.7, 0.95), 0.49)]).observe(None, TS), [])
        self.assertEqual(provider([((0.0, 0.0, 0.2, 0.3), 0.9)]).observe(None, TS), [])

    def test_best_overlap_wins_and_stats_reported(self):
        p = provider([((0.0, 0.0, 0.3, 0.4), 0.95), ((0.3, 0.4, 0.7, 0.95), 0.6)])
        self.assertEqual(p.observe(None, TS)[0].confidence, 0.6)
        d = p.describe()
        self.assertEqual((d["name"], d["capabilities"], d["simulated"], d["inferences"]),
                         ("apple_vision_person", ["person"], False, 1))
        self.assertIsNotNone(d["infer_ms_p50"])

    def test_normalizer_turns_three_frames_into_one_enter(self):
        p = provider([((0.3, 0.4, 0.7, 0.95), 0.71)])
        n = EventNormalizer(p, ["desk_01"], enter_frames=3)
        events = []
        for i in range(10):
            events += n.process(p.observe(None, TS), 1794100000000 + i * 33, 100.0 + i * 0.033)
        self.assertEqual([e.kind for e in events], ["person_in_zone"])
        self.assertEqual(events[0].metadata["zone_id"], "desk_01")


class AppleVisionConfigTest(unittest.TestCase):
    def test_refused_outside_mac_dev(self):
        with self.assertRaises(ConfigError):
            EdgeConfig({"AIRLOCK_EDGE_RUNTIME": "jetson", "AIRLOCK_OBSERVER": "apple_vision",
                        "AIRLOCK_PUBLISH": "off"}, platform_name="jetson")

    def test_mac_dev_ok(self):
        cfg = EdgeConfig({"AIRLOCK_EDGE_RUNTIME": "mac_dev", "AIRLOCK_OBSERVER": "apple_vision",
                          "AIRLOCK_PUBLISH": "off", "AIRLOCK_PERSON_THRESHOLD": "0.6"}, platform_name="macos")
        self.assertEqual((cfg.observer, cfg.person_threshold), ("apple_vision", 0.6))


class HealthVocabularyTest(unittest.TestCase):
    def test_defaults_match_deployed_module(self):
        c = Contract()
        self.assertEqual(c.health_components, frozenset(["camera"]))
        self.assertEqual([c.health_status(s) for s in ("streaming", "starting", "stalled", "disconnected", "stopped")],
                         ["online", "degraded", "degraded", "offline", "offline"])
        self.assertEqual(c.health_status("weird_state"), "weird_state")

    def test_contract_file_overrides(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"health_components": ["camera", "edge"], "health_status_map": {"streaming": "ok"}}, f)
        try:
            c = Contract.from_file(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(c.health_components, frozenset(["camera", "edge"]))
        self.assertEqual((c.health_status("streaming"), c.health_status("stopped")), ("ok", "stopped"))


class WireMetadataTest(unittest.TestCase):
    def test_values_go_out_as_bounded_strings(self):
        c = Contract()
        md = {"bbox": [0.116, 0.3, 0.623, 0.994], "frames_confirmed": 3, "capture_to_event_ms": 5.9,
              "zone_overlap": 1.0, "zone_id": "desk_01", "provider_version": "x" * 100}
        wire = c.wire_metadata(md)
        self.assertEqual(wire["bbox"], "0.116,0.300,0.623,0.994")
        self.assertEqual((wire["frames_confirmed"], wire["capture_to_event_ms"], wire["zone_overlap"]),
                         ("3", "5.9", "1"))
        self.assertEqual(len(wire["provider_version"]), 64)
        self.assertTrue(all(isinstance(v, str) for v in wire.values()))

    def test_ingest_args_metadata_json_is_string_only(self):
        c = Contract()
        ev = make_event(c, lambda: 7, "dhruv-m4-air", "", "mac-dev-cam-01", "mac_dev_camera", "person_in_zone",
                        1794098241000, 0.71, {"zone_id": "desk_01", "bbox": [0.1, 0.2, 0.3, 0.4],
                                              "frames_confirmed": 3}, False)
        md = json.loads(ingest_args(ev, c)[8])
        self.assertEqual(md, {"zone_id": "desk_01", "bbox": "0.100,0.200,0.300,0.400", "frames_confirmed": "3"})

    def test_typed_mode_still_available(self):
        self.assertEqual(Contract(metadata_strings=False).wire_metadata({"n": 3}), {"n": 3})


class SystemStatsTest(unittest.TestCase):
    """Orin: some thermal zones (and the GPU load node) raise TypeError on read instead of returning a number."""

    def test_unreadable_nodes_are_skipped_not_fatal(self):
        from unittest import mock
        from locki_edge.api import SystemStats

        class Node(object):
            def __init__(self, text):
                self.text = text

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                if self.text is None:
                    raise TypeError("decoder got no data")
                return self.text

        nodes = {"/tz0/temp": Node(None), "/tz1/temp": Node("45000\n"), "/tz2/temp": Node("51500\n"),
                 "/gpu/load": Node(None)}
        stats = SystemStats.__new__(SystemStats)
        stats.gpu_path = "/gpu/load"
        with mock.patch("locki_edge.api.glob.glob", return_value=["/tz0/temp", "/tz1/temp", "/tz2/temp"]), \
                mock.patch("locki_edge.api.open", side_effect=lambda path, *a, **k: nodes[path], create=True):
            self.assertEqual(stats._temp(), 51.5)
            self.assertIsNone(stats._gpu())


if __name__ == "__main__":
    unittest.main()
