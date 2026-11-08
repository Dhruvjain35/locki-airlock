"""EDGE-06 live run: real camera -> runtime -> real reducer -> verify the event in the device's own view.

    python3 tools/accept_run.py --env-file ~/.config/locki-edge/airlock.env --seconds 60

Runs `python -m locki_edge.runtime` as a child process for --seconds, then reads THIS run's receipts from
the durable outbox (read-only), queries my_device_events / my_device_health / my_devices as the device's
own identity, and writes everything to docs/airlock/edge/evidence/<runtime>-<stamp>/. Exit 0 only if at
least one real (simulated=false) event came back `accepted` AND is visible in my_device_events.

Never prints the token. Python 3.6 compatible.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from locki_edge.config import load_config  # noqa: E402
from locki_edge.stdb import StdbClient  # noqa: E402

REPO = os.path.dirname(os.path.dirname(HERE))


def utc(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def run_receipts(state_dir, since_iso):
    path = os.path.join(state_dir, "outbox.sqlite3")
    if not os.path.exists(path):
        return []
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        rows = db.execute("SELECT event_id, seq, kind, status, reason, http_status, server_result, error, attempt, "
                          "attempted_at_utc, latency_ms FROM receipts WHERE attempted_at_utc >= ? ORDER BY id",
                          (since_iso,)).fetchall()
    finally:
        db.close()
    keys = ("event_id", "seq", "kind", "status", "reason", "http_status", "server_result", "error", "attempt",
            "attempted_at_utc", "latency_ms")
    return [dict(zip(keys, r)) for r in rows]


def run_audit(state_dir, event_ids):
    path = os.path.join(state_dir, "events.jsonl")
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event_id") in event_ids:
                    out.append(rec)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", required=True)
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--out", default=os.path.join(REPO, "docs", "airlock", "edge", "evidence"))
    p.add_argument("--until-accepted", action="store_true",
                   help="standby: stop (SIGTERM, clean shutdown) a few seconds after the first accepted event")
    a = p.parse_args(argv)

    cfg = load_config(a.env_file)
    if not cfg.publish:
        print("refusing: publishing is off (%s)" % cfg.publish_reason, file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = os.path.join(a.out, "%s-%s" % (cfg.runtime, stamp))
    os.makedirs(out)
    with open(os.path.join(out, "config.public.json"), "w") as f:
        json.dump(cfg.public(), f, indent=1, sort_keys=True)

    started_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())  # receipts use ISO ms "Z"; prefix compare
    t0 = time.time()
    log_path = os.path.join(out, "runtime.log")
    with open(log_path, "w") as logf:
        child = subprocess.Popen([sys.executable, "-m", "locki_edge.runtime", "--env-file", a.env_file,
                                  "--run-seconds", str(a.seconds)], cwd=HERE, stdout=logf,
                                 stderr=subprocess.STDOUT)
        try:
            if a.until_accepted:
                deadline = time.time() + a.seconds + 60
                while child.poll() is None and time.time() < deadline:
                    time.sleep(2.0)
                    if any(r["status"] == "accepted" for r in run_receipts(cfg.state_dir, started_iso)):
                        time.sleep(3.0)   # let the health report + any in-flight receipt land
                        child.terminate()
                        break
            code = child.wait(timeout=a.seconds + 60)
        except subprocess.TimeoutExpired:
            child.terminate()
            code = child.wait(timeout=15)
    wall_s = time.time() - t0

    receipts = run_receipts(cfg.state_dir, started_iso)
    accepted = [r for r in receipts if r["status"] == "accepted"]
    client = StdbClient(cfg.server, cfg.database, cfg.token)
    verified = []
    for r in accepted:
        uuid.UUID(r["event_id"])  # only a UUID is ever interpolated into SQL
        status, rows, err = client.sql_rows("SELECT * FROM my_device_events WHERE id = '%s'" % r["event_id"])
        verified.append({"event_id": r["event_id"], "http_status": status, "rows": rows, "error": err,
                         "checked_at_utc": utc()})
    health = client.sql_rows("SELECT * FROM my_device_health")
    devices = client.sql_rows("SELECT * FROM my_devices")
    audit = run_audit(cfg.state_dir, set(r["event_id"] for r in receipts))
    visible = [v for v in verified if v["http_status"] == 200 and v["rows"]]
    real_visible = [v for v in visible if not any(row.get("simulated") for row in v["rows"])]

    result = {
        "runtime": cfg.runtime, "platform": cfg.platform, "device_id": cfg.device_id,
        "workstation_id": cfg.workstation_id, "identity": cfg.identity, "database": cfg.database,
        "started_utc": utc(t0), "wall_s": round(wall_s, 1), "runtime_exit_code": code,
        "receipts": receipts, "verify_my_device_events": verified,
        "my_device_health": {"http_status": health[0], "rows": health[1], "error": health[2]},
        "my_devices": {"http_status": devices[0], "rows": devices[1], "error": devices[2]},
        "audit_events": audit,
        "verdict": "PASS" if real_visible else "FAIL",
        "verdict_reason": ("%d real event(s) accepted and visible in my_device_events" % len(real_visible))
        if real_visible else ("receipts=%d accepted=%d visible=%d" % (len(receipts), len(accepted), len(visible))),
    }
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump(result, f, indent=1, sort_keys=True, default=str)

    lines = ["# EDGE-06 live run %s (%s)" % (stamp, cfg.runtime), "",
             "**Verdict: %s**: %s" % (result["verdict"], result["verdict_reason"]), "",
             "Device `%s` · workstation `%s` · identity `%s` · runtime exit %s · %.1f s" % (
                 cfg.device_id, cfg.workstation_id, cfg.identity, code, wall_s), "",
             "| event_id | kind | seq | status | http | latency ms | attempted_at (UTC) | reducer said |",
             "|---|---|---|---|---|---|---|---|"]
    for r in receipts:
        lines.append("| `%s` | %s | %s | %s | %s | %s | %s | %s |" % (
            r["event_id"], r["kind"], r["seq"], r["status"], r["http_status"],
            None if r["latency_ms"] is None else round(r["latency_ms"]), r["attempted_at_utc"],
            (r["server_result"] or r["reason"] or "")[:80].replace("|", "/")))
    lines += ["", "## In my_device_events (queried as the device identity)", ""]
    for v in verified:
        for row in v["rows"]:
            lines.append("- `%s` kind=%s seq=%s observed_at_ms=%s received_at_ms=%s simulated=%s classification=%s" % (
                row.get("id"), row.get("kind"), row.get("seq"), row.get("observed_at_ms"),
                row.get("received_at_ms"), row.get("simulated"), row.get("classification")))
        if not v["rows"]:
            lines.append("- `%s` NOT visible (http %s %s)" % (v["event_id"], v["http_status"], v["error"][:120]))
    lines += ["", "Files: `result.json` (everything), `runtime.log`, `config.public.json` (no token)."]
    with open(os.path.join(out, "SUMMARY.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("\nevidence: %s" % out)
    return 0 if real_visible else 1


if __name__ == "__main__":
    sys.exit(main())
