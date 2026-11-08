"""EDGE-03 event correctness against the PINNED deployed contract of locki-w91bw.

    cd edge/jetson && python3 -m unittest tests.test_contract -v
"""
import json
import os
import sys
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from locki_edge import contract as C  # noqa: E402

PINNED = os.path.join(ROOT, "contract", "locki-w91bw.pinned.json")


class Counter(object):
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return self.value


def ev(contract=None, seq=None, **over):
    kw = dict(workstation_id="ws-areeb", session_id="", device_id="mac-dev-cam-01", source="mac_dev_camera",
              kind="motion_in_zone", observed_at_ms=1794100000123, confidence=None,
              metadata={"zone_id": "desk_01", "observation": "motion", "runtime": "mac_dev",
                        "capture_source": "mac_dev_camera", "provider": "baseline_motion"},
              simulated=False)
    kw.update(over)
    return C.make_event(contract or C.Contract(), seq or Counter(), **kw)


class TestPinnedSignature(unittest.TestCase):
    def test_code_matches_deployed_snapshot(self):
        pinned = C.load_pinned(PINNED)
        self.assertEqual(C.check_pinned_signature(pinned), [])
        self.assertEqual(pinned["database"], "locki-w91bw")

    def test_drift_is_detected(self):
        pinned = C.load_pinned(PINNED)
        pinned["reducers"]["ingest_device_event"] = pinned["reducers"]["ingest_device_event"][::-1]
        diffs = C.check_pinned_signature(pinned)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["reducer"], "ingest_device_event")


class TestDriftTool(unittest.TestCase):
    def test_parses_real_describe_output_including_cli_banner(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import contract_check
        path = os.path.join(HERE, "fixtures", "describe_reducers_2026-11-07.json")
        self.assertEqual(contract_check.main(["--describe-json", path]), 0)
        with open(path) as f:
            parsed = contract_check.reducers_from_describe(f.read())
        self.assertEqual(parsed["reducers"]["enroll_device"],
                         [["workstation_id", "String"], ["device_id", "String"], ["identity_hex", "String"],
                          ["role", "String"], ["label", "String"]])


class TestEventShape(unittest.TestCase):
    def test_ingest_args_positional_types(self):
        e = ev(confidence=0.876543)
        args = C.ingest_args(e, C.Contract())
        self.assertEqual(len(args), 11)
        typed = dict(zip([p[0] for p in C.INGEST_PARAMS], args))
        self.assertEqual(typed["event_id"], e.event_id)
        self.assertEqual(typed["observed_at_ms"], 1794100000123)
        self.assertEqual(typed["confidence"], 0.8765)
        self.assertIs(typed["simulated"], False)
        self.assertEqual(typed["schema_version"], 1)
        self.assertEqual(json.loads(typed["metadata_json"])["zone_id"], "desk_01")
        # JSON round-trip is exactly what the HTTP API receives: ints stay ints, F64 stays float.
        wire = json.loads(json.dumps(args))
        self.assertIsInstance(wire[5], int)
        self.assertIsInstance(wire[6], int)
        self.assertIsInstance(wire[7], float)

    def test_utc_timestamp_and_uuid(self):
        e = ev()
        self.assertEqual(e.observed_at, "2026-11-08T01:06:40.123Z")
        self.assertEqual(str(uuid.UUID(e.event_id)), e.event_id)

    def test_null_confidence_uses_contract_convention(self):
        e = ev(confidence=None)
        self.assertEqual(C.ingest_args(e, C.Contract())[7], 0.0)
        self.assertEqual(C.ingest_args(e, C.Contract(null_confidence=-1.0))[7], -1.0)

    def test_seq_not_burned_by_invalid_event(self):
        seq = Counter()
        with self.assertRaises(C.ContractError):
            ev(seq=seq, kind="malware_detected")
        self.assertEqual(seq.value, 0)
        self.assertEqual(ev(seq=seq).seq, 1)

    def test_rejections(self):
        bad = [
            dict(kind="malware_detected"),
            dict(source="jetson_but_lying"),
            dict(device_id=""),
            dict(device_id="bad id with spaces"),
            dict(confidence=1.5),
            dict(confidence=float("nan")),
            dict(observed_at_ms=0),
            dict(metadata={"zone_id": "desk_01", "frame_jpeg": "..."}),         # privacy
            dict(metadata={"embedding": [0.1, 0.2]}),                           # privacy
            dict(metadata={"person_name": "someone"}),                          # privacy
            dict(metadata={"zone_id": "desk_01", "nested": {"a": 1}}),          # not allowlisted
            dict(metadata={"bbox": [0.5, 0.5, 0.4, 0.9]}),                      # inverted box
            dict(metadata={"zone_id": "Desk 01!"}),                             # not a slug
            dict(source="fixture", simulated=False),                            # fixtures must say simulated
        ]
        for over in bad:
            with self.assertRaises(C.ContractError, msg=repr(over)):
                ev(**over)

    def test_privacy_keys_cannot_be_allowlisted(self):
        with self.assertRaises(C.ContractError):
            C.Contract(metadata_rules={"face_embedding": "text"})

    def test_metadata_size_cap(self):
        contract = C.Contract(max_metadata_bytes=64)
        with self.assertRaises(C.ContractError):
            ev(contract=contract, metadata={"zone_id": "z" * 60, "observation": "motion"})

    def test_health_args(self):
        self.assertEqual(C.health_args("ws-areeb", "jetson-01", "camera", "ok", "fps=29.9"),
                         ["ws-areeb", "jetson-01", "camera", "ok", "fps=29.9"])
        with self.assertRaises(C.ContractError):
            C.health_args("ws-areeb", "jetson-01", "camera", "Not A Slug", "")


class TestLiveContract(unittest.TestCase):
    # The module's allowlist for role "jetson" (spacetime/src/index.ts:434).
    MODULE_JETSON_KINDS = frozenset(("person_in_zone", "person_cleared", "camera_offline", "camera_online", "heartbeat"))

    def test_lists_only_kinds_the_module_allows_for_role_jetson(self):
        live = C.Contract.from_file(os.path.join(ROOT, "contract", "locki-w91bw.live.json"))
        self.assertEqual(live.kinds, frozenset(("person_in_zone", "person_cleared", "camera_offline", "camera_online")))
        self.assertTrue(live.kinds <= self.MODULE_JETSON_KINDS)
        for kind in ("owner_face_match", "unrecognized_face", "face_uncertain", "keyboard_interaction",
                     "usb_zone_interaction"):
            with self.assertRaises(C.ContractError):     # face and interaction kinds stay in the local audit
                ev(live, kind=kind, confidence=0.9)


if __name__ == "__main__":
    unittest.main()
