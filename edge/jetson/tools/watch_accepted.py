"""Watch a RUNNING edge service (any owner, incl. systemd) for its first accepted event and verify it.

    python3 tools/watch_accepted.py --env-file ~/.config/locki-edge/airlock.env --since 2026-11-08T02:19:49 \
        --timeout-s 7200

Read-only: opens the service's outbox.sqlite3 with mode=ro (WAL allows a concurrent reader), never touches
the camera, the process, or the queue. On the first `accepted` receipt it queries my_device_events for that
event_id as the device's own identity and writes evidence to docs/airlock/edge/evidence/<runtime>-watch-<stamp>/.
Exit 0 = accepted AND visible; 1 = timeout or not visible. Python 3.6 compatible.
"""
import argparse
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from locki_edge.config import load_config  # noqa: E402
from locki_edge.stdb import StdbClient  # noqa: E402
from accept_run import REPO, run_audit, run_receipts, utc  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", required=True)
    p.add_argument("--since", required=True, help="UTC ISO prefix, e.g. 2026-11-08T02:19:49")
    p.add_argument("--timeout-s", type=float, default=7200.0)
    p.add_argument("--interval-s", type=float, default=3.0)
    p.add_argument("--out", default=os.path.join(REPO, "docs", "airlock", "edge", "evidence"))
    a = p.parse_args(argv)
    cfg = load_config(a.env_file)
    if not cfg.token:
        print("refusing: no device token (%s)" % cfg.publish_reason, file=sys.stderr)
        return 2
    deadline = time.time() + a.timeout_s
    first = None
    while time.time() < deadline:
        receipts = run_receipts(cfg.state_dir, a.since)
        accepted = [r for r in receipts if r["status"] == "accepted"]
        if accepted:
            first = accepted[0]
            break
        time.sleep(a.interval_s)
    if first is None:
        print(json.dumps({"accepted": False, "since": a.since, "timeout_s": a.timeout_s}))
        return 1
    time.sleep(1.0)
    uuid.UUID(first["event_id"])  # only a UUID is ever interpolated into SQL
    client = StdbClient(cfg.server, cfg.database, cfg.token)
    status, rows, err = client.sql_rows("SELECT * FROM my_device_events WHERE id = '%s'" % first["event_id"])
    receipts = run_receipts(cfg.state_dir, a.since)
    result = {"device_id": cfg.device_id, "workstation_id": cfg.workstation_id, "identity": cfg.identity,
              "runtime": cfg.runtime, "camera_source": cfg.camera_source, "watched_since": a.since,
              "first_accepted_receipt": first, "receipts_since": receipts,
              "verify_my_device_events": {"http_status": status, "rows": rows, "error": err, "checked_at_utc": utc()},
              "audit_record": run_audit(cfg.state_dir, {first["event_id"]}),
              "verdict": "PASS" if status == 200 and rows else "FAIL"}
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = os.path.join(a.out, "%s-watch-%s" % (cfg.runtime, stamp))
    os.makedirs(out)
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump(result, f, indent=1, sort_keys=True, default=str)
    row = rows[0] if rows else {}
    lines = ["# EDGE-06 accepted event (%s, watched)" % cfg.runtime, "",
             "**Verdict: %s**" % result["verdict"], "",
             "| Field | Value |", "|---|---|",
             "| event_id | `%s` |" % first["event_id"],
             "| kind / seq | %s / %s |" % (first["kind"], first["seq"]),
             "| receipt | %s, HTTP %s, %s ms, attempt %s, at %s |" % (
                 first["status"], first["http_status"], first["latency_ms"], first["attempt"], first["attempted_at_utc"]),
             "| in my_device_events | %s |" % ("yes: session `%s`, observed_at_ms %s, received_at_ms %s, simulated %s, "
                                              "classification `%s`" % (row.get("session_id"), row.get("observed_at_ms"),
                                                                     row.get("received_at_ms"), row.get("simulated"),
                                                                     row.get("classification")) if row else
                                              "NO (http %s %s)" % (status, (err or "")[:100])),
             "| device / workstation | `%s` / `%s` (identity `%s`) |" % (cfg.device_id, cfg.workstation_id, cfg.identity),
             "| camera | runtime `%s`, AIRLOCK_CAMERA_SOURCE `%s` |" % (cfg.runtime, cfg.camera_source), ""]
    with open(os.path.join(out, "SUMMARY.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("evidence: %s" % out)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
