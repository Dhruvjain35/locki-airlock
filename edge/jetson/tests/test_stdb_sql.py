"""sql_rows: parses SpacetimeDB's HTTP SQL response (SATS-JSON) into plain dict rows."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from locki_edge.stdb import StdbClient  # noqa: E402

IDENT = "ab" * 32   # synthetic, same shape as a real 64-hex identity


class _Resp(object):
    def __init__(self, body, status=200):
        self._body, self.status = body.encode("utf-8"), status

    def read(self):
        return self._body

    def close(self):
        pass


def _client(body, seen=None):
    def urlopen(req, timeout=None):
        if seen is not None:
            seen.append((req.full_url, req.data.decode("utf-8"), req.get_header("Authorization")))
        return _Resp(body)
    return StdbClient("https://maincloud.spacetimedb.com", "locki-w91bw", "tok", timeout=5, urlopen_fn=urlopen)


def _doc(rows):
    names = ["id", "workstation_id", "identity", "role", "label", "enabled", "last_seq", "last_event_at_ms"]
    return json.dumps([{"schema": {"elements": [{"name": {"some": n}, "algebraic_type": {}} for n in names]},
                        "rows": rows, "total_duration_micros": 120}])


class SqlRowsTest(unittest.TestCase):
    def test_identity_array_wrapper_flattened(self):
        seen = []
        c = _client(_doc([["mac-dev-cam-01", "ws-1", ["0x" + IDENT], "camera", "Mac", True, 0, 0]]), seen)
        status, rows, err = c.sql_rows("SELECT * FROM my_devices")
        self.assertEqual((status, err), (200, ""))
        self.assertEqual(rows[0]["identity"], "0x" + IDENT)
        self.assertEqual(rows[0]["workstation_id"], "ws-1")
        self.assertTrue(seen[0][0].endswith("/v1/database/locki-w91bw/sql"))
        self.assertEqual(seen[0][2], "Bearer tok")

    def test_identity_dict_wrapper_flattened(self):
        c = _client(_doc([["d", "ws-1", {"__identity__": "0x" + IDENT}, "camera", "", True, 5, 9]]))
        self.assertEqual(c.sql_rows("SELECT * FROM my_devices")[1][0]["identity"], "0x" + IDENT)

    def test_large_body_not_truncated(self):
        rows = [["dev-%04d" % i, "ws-1", ["0x" + IDENT], "camera", "x" * 200, True, i, i] for i in range(200)]
        status, parsed, err = _client(_doc(rows)).sql_rows("SELECT * FROM my_devices")
        self.assertEqual((status, err, len(parsed)), (200, "", 200))

    def test_empty_view(self):
        self.assertEqual(_client(_doc([])).sql_rows("SELECT * FROM my_devices"), (200, [], ""))

    def test_unparsable_body_reported_not_raised(self):
        status, rows, err = _client("not json").sql_rows("SELECT 1")
        self.assertEqual((status, rows), (200, []))
        self.assertIn("unparsable", err)


if __name__ == "__main__":
    unittest.main()
