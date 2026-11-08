"""Wait until the owner grants the investigator an incident's evidence.

Read-only: one SELECT on my_memberships and one on my_investigator_incidents per
interval, through the investigator's own token (Sid's minimum authorization:
grant_incident_evidence by the workstation owner; no membership at all). Exit codes:
  0  at least one incident grant is visible and the identity holds no membership
  4  the identity holds a membership or approval/owner rights: over-privileged, ask to revoke
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
        granted = p.get("authorized_incidents") if "authorized_incidents" in p else p.get("memberships")
        if p.get("reachable") and p.get("least_privilege_ok") is False:
            print(f"{stamp} OVER-PRIVILEGED memberships={p.get('memberships')} "
                  f"over={p.get('over_privileged_workstations')}", flush=True)
            return 4
        if p.get("reachable") and granted:
            print(f"{stamp} GRANTED (read-only) authorized_incidents={p.get('authorized_incidents')} "
                  f"incidents={p.get('authorized_incident_ids')} memberships={p.get('memberships')}", flush=True)
            return 0
        print(f"{stamp} waiting: reachable={p.get('reachable')} authorized_incidents={p.get('authorized_incidents')} "
              f"memberships={p.get('memberships')}", flush=True)
        if time.monotonic() - t0 + interval_s > max_s:
            return 2
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-s", type=float, default=60.0)
    ap.add_argument("--max-s", type=float, default=7200.0)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.interval_s, a.max_s)))
