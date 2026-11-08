"""Read-only checks against the team's SpacetimeDB module with this Mac agent's own identity: the reducer contract, the views it subscribes to, and
whether a given event reached the database. It writes nothing. Real events are sent by the running backend (make airlock-mac).

    .venv/bin/python scripts/airlock_spacetime_check.py
    .venv/bin/python scripts/airlock_spacetime_check.py --event EVENT_ID     # did this event reach device_events? (needs the agent to be enrolled)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA = Path(__file__).resolve().parents[2] / "data" / "security"


async def run(args) -> int:
    from app.security.identity import load_identity
    from app.security.spacetime_adapter import (
        SpacetimeConfig,
        SpacetimeSubscriber,
        SpacetimeTransport,
        agent_identity,
        load_or_create_token,
    )

    stored = json.loads((DATA / "spacetime.json").read_text())
    url = stored["url"]
    config = SpacetimeConfig(url=url, database=stored["database"], workstation_id=stored["workstation_id"], device_id=load_identity(DATA).device_id, token=load_or_create_token(url, DATA))
    print(f"database {config.database} at {url}, workstation {config.workstation_id}, device {config.device_id}, agent identity {agent_identity(url, DATA)}")
    problems = SpacetimeTransport(config).check()
    print("[ok]  " if not problems else "[FAIL]", "reducer contract", problems or "matches")
    sub = SpacetimeSubscriber(config, on_incident=lambda u: None, on_lock_request=lambda r: None, on_session=lambda s: print("      session:", s.session_id, s.state, "active" if s.active() else "not active"))
    sub.start()
    for _ in range(100):
        if sub.connected:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)
    snap = sub.snapshot()
    print("[ok]  " if snap["connected"] else "[FAIL]", "subscribed to the views", {k: snap[k] for k in ("rows_received", "rows_rejected", "rows_ignored", "last_error")})
    await sub.stop()
    if args.event:
        headers = {"Authorization": f"Bearer {config.token}", "Content-Type": "text/plain"}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{url}/v1/database/{config.database}/sql", headers=headers, content=f"SELECT * FROM my_device_events WHERE id = '{args.event}'")
        rows = r.json()[0]["rows"] if r.status_code == 200 else []
        print("[ok]  " if rows else "[FAIL]", f"event {args.event} in my_device_events", f"{len(rows)} row" if r.status_code == 200 else r.text[:200])
        for row in rows:
            print("      ", json.dumps(row)[:400])
    return 0 if not problems and snap["connected"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", default=None)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
