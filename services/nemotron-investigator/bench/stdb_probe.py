"""Real, read-only probe of the live SpacetimeDB authorization path.

Uses the investigator's OWN identity (.secrets/stdb_investigator.token), never
the owner's CLI token. Records exactly what the server returned; nothing here
is simulated. Every check is a SELECT on an identity-scoped my_* view (plus one
expected-refusal read of a private table); no reducer is ever called.

Output (live rows are private evidence, so they never go to a tracked path):
  var/live/stdb_probe_<ts>.json      full result, mode 0600 (gitignored)
  bench/results/stdb_live_probe.json redacted: ok flags, counts and timings only

  python bench/stdb_probe.py [--incident incident:<32 hex>] [--workstation ws:...]
Without --incident, the newest incident visible to the identity (if any) is used.

The bounded-read checks verify that the live SQL subset accepts the LIMIT
clauses and the received_at_ms windows the adapter now sends. With no
membership they return 0 rows; what matters is ok=True rather than a
contract_error mentioning LIMIT. If LIMIT is rejected, set
nemotron_investigator.evidence.spacetime_http.SQL_LIMIT_CLAUSES = False.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from nemotron_investigator.config import Settings
from nemotron_investigator.evidence.base import EvidenceContractError, EvidenceUnavailable
from nemotron_investigator.evidence.spacetime_http import SpacetimeHttpEvidenceSource

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_WORKSTATION = "ws:probe-no-membership"
PLACEHOLDER_SESSION = "sess:probe-no-membership"


async def main(incident_id: str, workstation: str | None) -> dict:
    s = Settings.from_env()
    src = SpacetimeHttpEvidenceSource.from_token_file(s.stdb_host, s.stdb_database, s.stdb_token_file)
    now_ms = int(time.time() * 1000)
    out = {"probed_at_ms": now_ms, "host": s.stdb_host, "database": s.stdb_database,
           "investigator_identity": src.provenance.detail.get("identity"), "checks": []}

    async def check(name, coro):
        t = time.perf_counter()
        try:
            val = await coro
            res = {"ok": True, "result": val}
        except EvidenceUnavailable as e:
            res = {"ok": False, "error_type": "evidence_unavailable", "error": str(e)}
        except EvidenceContractError as e:
            res = {"ok": False, "error_type": "contract_error", "error": str(e)}
        rejected = src.pop_rejected_rows()
        if rejected:
            res["rejected_rows"] = rejected
        res.update({"check": name, "ms": round((time.perf_counter() - t) * 1000, 1)})
        out["checks"].append(res)
        return res

    await check("ping_and_least_privilege", src.ping())
    listed = await check(f"list_recent_incidents via {src._v_incidents} (operator listing)", _listing(src))
    if not incident_id and listed.get("ok") and listed["result"]["newest"]:
        incident_id = listed["result"]["newest"][0]["id"]
    out["incident_probed"] = incident_id
    inc = (await check(f"get_incident via {src._v_incidents}", _row_summary(src.get_incident(incident_id)))
           if incident_id else {"ok": True, "result": None})
    private = await check("direct private table read (expected refusal: HTTP 400 -> contract_error)",
                          _row_count(src._sql("SELECT * FROM incidents")))
    if private.get("ok"):  # the identity could read a private table: that is a least-privilege violation
        private.update({"ok": False, "error_type": "least_privilege_violation",
                        "error": "the investigator identity read the private incidents table"})

    found = inc.get("result") or {}
    ws = workstation or found.get("workstation_id") or PLACEHOLDER_WORKSTATION
    sid = found.get("session_id") or PLACEHOLDER_SESSION
    out["bounded_read_scope"] = {"workstation_id": ws, "session_id": sid}
    await check("session read scoped to workstation (id AND workstation_id)",
                _row_count(src.get_session(ws, sid)))
    await check(f"event window on {src._v_events}.received_at_ms with LIMIT 500 (syntax check)",
                _row_count(src.list_device_events(ws, now_ms - 300_000, now_ms)))
    await check(f"baseline read on {src._v_baselines}, 24 h lower bound, LIMIT 200 (syntax check)",
                _row_count(src.list_baseline_snapshots(ws, now_ms)))
    await check("device health read with LIMIT 500 (syntax check)",
                _row_count(src.list_device_health(ws)))
    limit_checks = [c for c in out["checks"] if "LIMIT" in c["check"]]
    out["limit_clause_supported"] = (all(c["ok"] for c in limit_checks)
                                     if not any(c.get("error_type") == "evidence_unavailable" for c in limit_checks)
                                     else None)
    return out


async def _listing(src):
    rows = await src.list_recent_incidents(5)
    return {"visible": len(rows), "newest": [{"id": r.id, "rule_id": r.rule_id, "severity": r.severity,
                                              "workstation_id": r.workstation_id, "created_at_ms": r.created_at_ms,
                                              "simulated": r.simulated} for r in rows]}


def _redact_result(r):
    """Allowlist: only shapes known to carry no row content survive; everything else is withheld."""
    if r is None:
        return None
    if isinstance(r, list):
        return {"rows": len(r)}
    if not isinstance(r, dict):
        return {"redacted": True}
    if "newest" in r:
        return {"visible": r.get("visible")}
    if "memberships" in r:
        return {k: r[k] for k in ("reachable", "live", "memberships", "authorized_for_any_workstation",
                                  "least_privilege_ok") if k in r}
    if set(r) == {"rows"} and isinstance(r["rows"], int):
        return {"rows": r["rows"]}
    if "id" in r:
        return {"found": True}
    return {"redacted": True}


def redact(res: dict) -> dict:
    """Keep what proves the authorization path (ok flags, counts, timings); drop row content."""
    out = {k: res[k] for k in ("probed_at_ms", "host", "database", "investigator_identity",
                               "limit_clause_supported") if k in res}
    out["checks"] = []
    for c in res["checks"]:
        out["checks"].append({"check": c["check"], "ok": c["ok"], "ms": c["ms"],
                              "result": _redact_result(c.get("result")),
                              **({"error_type": c["error_type"]} if "error_type" in c else {})})
    out["note"] = "redacted copy; the full probe (with row content) is under var/live/ and is never committed"
    return out


async def _row_summary(coro):
    row = await coro
    return None if row is None else {"id": row.id, "rule_id": row.rule_id, "severity": row.severity,
                                     "workstation_id": row.workstation_id, "session_id": row.session_id,
                                     "evidence_complete": getattr(row, "evidence_complete", None)}


async def _row_count(coro):
    rows = await coro
    if rows is None:
        return {"rows": 0}
    return {"rows": len(rows) if isinstance(rows, list) else 1}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--incident", default=None, help="live incident id (default: newest visible incident)")
    ap.add_argument("--workstation", default=None,
                    help="workstation for the bounded-read checks (default: the incident's, if visible)")
    a = ap.parse_args()
    res = asyncio.run(main(a.incident, a.workstation))
    private = ROOT / "var" / "live" / f"stdb_probe_{res['probed_at_ms']}.json"
    private.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(res, indent=2))
    dest = ROOT / "bench" / "results" / "stdb_live_probe.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(redact(res), indent=2))
    for c in res["checks"]:
        print(f"{c['check']:<72} ok={c['ok']!s:<5} {c['ms']:>6} ms  {json.dumps(c.get('result', c.get('error')))[:150]}")
    if res["limit_clause_supported"] is False:
        print("!! LIMIT looks unsupported by this server: set spacetime_http.SQL_LIMIT_CLAUSES = False")
    print(f"-> {dest} (redacted)\n-> {private} (full, private)")
