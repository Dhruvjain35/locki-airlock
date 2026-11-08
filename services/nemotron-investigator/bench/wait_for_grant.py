"""Wait until the investigator identity receives a workstation membership.

Read-only: one SELECT on my_memberships per interval, through the investigator's
own token. Exit codes:
  0  a read-only (least-privilege) membership exists
  4  a membership exists but carries approval or owner rights: ask Sid to fix it
  2  timed out
Prints no row content beyond the workstation ids and the least-privilege verdict.

  .venv/bin/python bench/wait_for_grant.py [--interval-s 60] [--max-s 7200]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from nemotron_investigator.config import Settings
from nemotron_investigator.evidence.spacetime_http import SpacetimeHttpEvidenceSource


async def main(interval_s: float, max_s: float) -> int:
    s = Settings.from_env()
    src = SpacetimeHttpEvidenceSource.from_token_file(s.stdb_host, s.stdb_database, s.stdb_token_file)
    t0 = time.monotonic()
    while True:
        p = await src.ping()
        stamp = time.strftime("%H:%M:%S")
        if p.get("reachable") and p.get("memberships"):
            ok = p.get("least_privilege_ok") is True
            print(f"{stamp} {'GRANTED (read-only)' if ok else 'GRANTED BUT OVER-PRIVILEGED'} "
                  f"memberships={p['memberships']} workstations={p.get('workstations')} "
                  f"least_privilege_ok={p.get('least_privilege_ok')} over={p.get('over_privileged_workstations')}",
                  flush=True)
            return 0 if ok else 4
        print(f"{stamp} waiting: reachable={p.get('reachable')} memberships={p.get('memberships')}", flush=True)
        if time.monotonic() - t0 + interval_s > max_s:
            return 2
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-s", type=float, default=60.0)
    ap.add_argument("--max-s", type=float, default=7200.0)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.interval_s, a.max_s)))
