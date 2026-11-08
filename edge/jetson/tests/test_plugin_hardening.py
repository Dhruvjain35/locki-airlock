"""Plugin adapter hardening + clean startup refusal (found by the CV contract review, 2026-11-08)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from locki_edge.config import detect_platform  # noqa: E402
from locki_edge.observers import PluginProvider  # noqa: E402
from locki_edge import runtime  # noqa: E402

TS = "2026-11-08T02:30:00.000Z"


class Inner(object):
    def __init__(self, fn, capabilities=("person", "face")):
        self.fn, self.capabilities, self.version = fn, capabilities, "dhruv-test-1"

    def observe(self, frame, captured_at_utc):
        return self.fn()


class PluginHardeningTest(unittest.TestCase):
    def test_generator_raising_mid_iteration_is_counted_not_fatal(self):
        def gen():
            yield {"kind": "person_in_zone", "zone_id": "desk", "confidence": 0.9}
            raise RuntimeError("model crashed mid-batch")
        p = PluginProvider(Inner(gen))
        self.assertEqual(p.observe(None, TS), [])
        self.assertEqual(p.errors, 1)
        self.assertIn("model crashed", p.last_error)

    def test_bare_dict_is_one_item(self):
        p = PluginProvider(Inner(lambda: {"kind": "person_in_zone", "zone_id": "desk", "confidence": 0.9}))
        obs = p.observe(None, TS)
        self.assertEqual([(o.kind, o.zone_id, o.confidence) for o in obs], [("person_in_zone", "desk", 0.9)])
        self.assertEqual(p.malformed, 0)

    def test_dhruv_native_zone_key_accepted(self):
        native = {"kind": "person_in_zone", "event_id": "dfee22c2f0344e1b8a3c05c8b18a340f",
                  "observed_at": 1794097391891, "confidence": 0.896, "zone": "desk"}
        obs = PluginProvider(Inner(lambda: [native])).observe(None, TS)
        self.assertEqual([(o.kind, o.zone_id, o.confidence) for o in obs], [("person_in_zone", "desk", 0.896)])

    def test_null_zone_and_bool_confidence_are_malformed(self):
        p = PluginProvider(Inner(lambda: [{"kind": "face_uncertain", "zone": None, "confidence": None},
                                          {"kind": "person_in_zone", "zone_id": "desk", "confidence": True}]))
        self.assertEqual(p.observe(None, TS), [])
        self.assertEqual(p.malformed, 2)

    def test_bare_string_capability_is_one_capability(self):
        self.assertEqual(PluginProvider(Inner(lambda: [], capabilities="person")).capabilities, frozenset(["person"]))


class StartupRefusalTest(unittest.TestCase):
    def test_provider_import_error_exits_2_without_opening_a_camera(self):
        d = tempfile.mkdtemp()
        try:
            env = os.path.join(d, "airlock.env")
            rt_name = "jetson" if detect_platform() == "jetson" else "mac_dev"
            with open(env, "w") as f:
                f.write("AIRLOCK_EDGE_RUNTIME=%s\nAIRLOCK_PUBLISH=off\nAIRLOCK_OBSERVER=dhruv_cv\n"
                        "AIRLOCK_OBSERVER_PLUGIN=no_such_pkg_locki_xyz:create_provider\n"
                        "AIRLOCK_CAMERA_SOURCE=/nonexistent/video9\nAIRLOCK_STATE_DIR=%s\n" % (rt_name, os.path.join(d, "s")))
            os.chmod(env, 0o600)
            self.assertEqual(runtime.main(["--env-file", env, "--run-seconds", "0.1", "--no-api"]), 2)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
