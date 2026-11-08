"""Live checks against the REAL SpacetimeDB module (default: maincloud / locki-w91bw).

    python3 tools/stdb_live.py mint     --out ~/.config/locki-edge/device_token   # device identity for enroll_device
    python3 tools/stdb_live.py whoami   --token-file ~/.config/locki-edge/device_token
    python3 tools/stdb_live.py denied   [--workstation-id ID]   # EDGE-05: throwaway identity must be rejected
    python3 tools/stdb_live.py publish  --token-file F --workstation-id W --device-id D   # simulated=true event
    python3 tools/stdb_live.py verify   --token-file F --event-id E   # read my_device_events as that identity
    python3 tools/stdb_live.py wait-enrolled --token-file F [--timeout-s 7200]  # poll my_devices until enrolled

Every event this tool sends is simulated=true with source=fixture. It never republishes or
alters the module, and never reads the spacetime CLI's own login token.
"""
import argparse
import json
import os
import stat
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from locki_edge.contract import Contract, make_event  # noqa: E402
from locki_edge.stdb import SpacetimePublisher, StdbClient, token_identity  # noqa: E402


class _Seq(object):
    def __init__(self, start):
        self.value = start

    def __call__(self):
        self.value += 1
        return self.value


def read_token(path):
    path = os.path.expanduser(path)
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit("refusing %s: token file must be chmod 600 (is %o)" % (path, mode & 0o777))
    with open(path) as f:
        return f.read().strip()


def fixture_event(args, seq_start):
    md = {"zone_id": "desk_01", "observation": "presence", "provider": "edge_live_check",
          "capture_source": "fixture"}
    return make_event(Contract(), _Seq(seq_start), args.workstation_id, args.session_id, args.device_id,
                      "fixture", args.kind, int(time.time() * 1000), None, md, True)


def show(receipt):
    print(json.dumps(receipt._asdict(), indent=1, sort_keys=True))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["mint", "whoami", "denied", "publish", "verify", "wait-enrolled"])
    p.add_argument("--server", default=os.environ.get("AIRLOCK_SERVER", "maincloud"))
    p.add_argument("--database", default=os.environ.get("AIRLOCK_DATABASE", "locki-w91bw"))
    p.add_argument("--token-file")
    p.add_argument("--out", default="~/.config/locki-edge/device_token")
    p.add_argument("--force", action="store_true")
    p.add_argument("--workstation-id", default="edge-authz-probe")
    p.add_argument("--session-id", default="")
    p.add_argument("--device-id", default="edge-authz-probe")
    p.add_argument("--kind", default="person_in_zone")
    p.add_argument("--seq-start", type=int, default=None, help="default: unix seconds (monotonic across runs)")
    p.add_argument("--event-id")
    p.add_argument("--timeout-s", type=float, default=7200.0)
    p.add_argument("--interval-s", type=float, default=15.0)
    a = p.parse_args(argv)
    seq_start = a.seq_start if a.seq_start is not None else int(time.time())

    if a.cmd == "mint":
        out = os.path.expanduser(a.out)
        if os.path.exists(out) and not a.force:
            sys.exit("%s exists (use --force to mint a NEW identity; the old one stays enrolled)" % out)
        identity, token = StdbClient.mint_identity(a.server)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        print(json.dumps({"identity_hex": identity, "token_file": out, "mode": "0600",
                          "next": "owner calls enroll_device(workstation_id, device_id, identity_hex, role, label)"}))
        return 0

    if a.cmd == "whoami":
        print(json.dumps({"identity_hex": token_identity(read_token(a.token_file))}))
        return 0

    if a.cmd == "denied":
        identity, token = StdbClient.mint_identity(a.server)
        pub = SpacetimePublisher(StdbClient(a.server, a.database, token), Contract())
        event = fixture_event(a, seq_start)
        receipt = pub.publish(event)
        print("throwaway identity (never enrolled): %s" % identity)
        show(receipt)
        ok = not receipt.delivered
        print("EDGE-05 %s: unenrolled identity was %s" % ("PASS" if ok else "FAIL",
                                                          "rejected" if ok else "ACCEPTED (tell Sid now)"))
        return 0 if ok else 1

    token = read_token(a.token_file)
    client = StdbClient(a.server, a.database, token)
    if a.cmd == "publish":
        event = fixture_event(a, seq_start)
        print("identity: %s  event_id: %s  seq: %d" % (token_identity(token), event.event_id, event.seq))
        show(SpacetimePublisher(client, Contract()).publish(event))
        return 0

    if a.cmd == "verify":
        if not a.event_id:
            sys.exit("--event-id required")
        uuid.UUID(a.event_id)  # also blocks SQL injection: only a UUID is interpolated
        status, rows, err = client.sql_rows("SELECT * FROM my_device_events WHERE id = '%s'" % a.event_id)
        print(json.dumps({"view": "my_device_events", "identity": token_identity(token), "http_status": status,
                          "rows": rows, "error": err,
                          "checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1))
        return 0 if status == 200 and rows else 1

    if a.cmd == "wait-enrolled":
        me = token_identity(token)
        deadline = time.time() + a.timeout_s
        polls = 0
        while True:
            polls += 1
            status, rows, err = client.sql_rows("SELECT * FROM my_devices")
            mine = [r for r in rows if str(r.get("identity", "")).lower().replace("0x", "") == me]
            if mine:
                print(json.dumps({"enrolled": True, "identity": me, "polls": polls, "devices": mine,
                                  "seen_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1))
                return 0
            if rows and not mine:
                print("my_devices shows %d row(s) but none with identity %s: %s" % (len(rows), me, rows),
                      file=sys.stderr)
            if time.time() >= deadline:
                print(json.dumps({"enrolled": False, "identity": me, "polls": polls, "last_http": status,
                                  "last_error": err}), file=sys.stderr)
                return 1
            time.sleep(a.interval_s)


if __name__ == "__main__":
    sys.exit(main())
