"""Detect drift between the code's pinned reducer signatures and Sid's DEPLOYED module (read-only).

    python3 tools/contract_check.py            # exit 0 = matches, 1 = drift (prints the diff), 2 = can't check

Runs `spacetime describe --json -s <server> <db>`; never publishes or calls reducers.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from locki_edge.contract import check_pinned_signature  # noqa: E402


def nm(x):
    return x.get("some") if isinstance(x, dict) else x


def prim(t):
    k = list(t.keys())[0]
    v = t[k]
    if k == "Product" and len(v["elements"]) == 1 and nm(v["elements"][0].get("name")) == "__identity__":
        return "Identity"
    return k


def reducers_from_describe(text):
    doc = json.loads(text[text.index("{"):])
    sections = {list(s.keys())[0]: list(s.values())[0] for s in doc["sections"]}
    out = {}
    for r in sections["Reducers"]:
        name = r["source_name"]
        out[name] = [[nm(p["name"]), prim(p["algebraic_type"])] for p in r["params"]["elements"]]
    return {"reducers": out}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="maincloud")
    p.add_argument("--database", default="locki-w91bw")
    p.add_argument("--describe-json", help="use a saved describe output instead of calling the CLI")
    a = p.parse_args(argv)
    if a.describe_json:
        with open(a.describe_json) as f:
            text = f.read()
    else:
        cli = shutil.which("spacetime") or os.path.expanduser("~/.local/bin/spacetime")
        if not os.path.exists(cli):
            print("spacetime CLI not found")
            return 2
        r = subprocess.run([cli, "describe", "--json", "-s", a.server, a.database], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        if r.returncode != 0:
            print("describe failed: %s" % r.stderr.decode("utf-8", "replace")[:300])
            return 2
        text = r.stdout.decode("utf-8", "replace")
    diffs = check_pinned_signature(reducers_from_describe(text))
    if diffs:
        print("CONTRACT DRIFT (stop and re-pin with Sid):")
        print(json.dumps(diffs, indent=1))
        return 1
    print("OK: ingest_device_event + report_health match the deployed %s" % a.database)
    return 0


if __name__ == "__main__":
    sys.exit(main())
