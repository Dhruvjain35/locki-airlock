"""Live end-to-end check, through the running API exactly as Dhruv's backend calls it:

  real SpacetimeDB incident -> Nemotron chooses and calls the read-only evidence tools
  -> structured, grounded assessment -> voice view text for LOCKI's voice

Nothing is simulated. The run uses evidence_mode="live", so the server refuses it
(409/503) rather than ever substituting fixture data. The API key is read from a
file and never printed. The full record contains private evidence and is written
only to var/live/ (mode 0600, gitignored); the terminal report prints the incident
id, the tool trace, latency, the official severity next to Nemotron's risk, every
unsupported or flagged claim, and the text the voice system would speak.

  .venv/bin/python bench/live_e2e.py --key-file .secrets/handoff/mustafa-operator.key
      [--incident incident:<32 hex>] [--allow-simulated] [--base http://127.0.0.1:8787]
Without --incident, the newest NON-simulated incident the investigator identity can read is
used (rows marked simulated=true are LOCKI test rows, not a real incident). The run is refused
unless the identity is least-privilege (read-only). Exit code 0 only when a live investigation
of a real (non-simulated) incident finished with an assessment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FINISHED = {"completed", "completed_with_warnings", "degraded", "failed", "timeout", "rejected", "interrupted"}


def _key(path: str) -> str:
    key = Path(path).read_text().strip()
    if not key:
        raise SystemExit(f"{path} is empty")
    return key


async def newest_incident(allow_simulated: bool) -> tuple[str | None, bool | None]:
    from nemotron_investigator.config import Settings
    from nemotron_investigator.evidence.spacetime_http import SpacetimeHttpEvidenceSource
    s = Settings.from_env()
    src = SpacetimeHttpEvidenceSource.from_token_file(s.stdb_host, s.stdb_database, s.stdb_token_file)
    rows = await src.list_recent_incidents(20)
    real = [r for r in rows if not r.simulated]
    if real:
        return real[0].id, False
    if rows and allow_simulated:
        return rows[0].id, True
    if rows:
        print(f"only simulated incidents are visible ({len(rows)}); pass --allow-simulated to use one")
    return None, None


def _private_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2, default=str))


def report(full: dict, voice: dict, header_mode: str | None, wall_s: float) -> list[str]:
    r = full.get("result") or {}
    out = [f"incident:            {full.get('incident_id')}  simulated={r.get('incident_simulated')}",
           f"investigation:       {full.get('investigation_id')}  status={full.get('status')}",
           f"evidence mode:       {r.get('evidence_mode')} (X-Evidence-Mode: {header_mode})  {r.get('evidence_banner')}"]
    off, eff, ra = r.get("official_severity") or {}, r.get("effective_severity") or {}, r.get("risk_assessment") or {}
    out.append(f"official severity:   {off.get('level') or off.get('raw')} (rule {off.get('rule_id')}; {off.get('source')})")
    out.append(f"Nemotron risk:       {ra.get('level')} / confidence {ra.get('confidence')}   "
               f"effective severity: {eff.get('level')} ({eff.get('basis')})")
    out.append("tool trace (chosen by Nemotron; executed read-only by the service):")
    for t in r.get("tool_execution_trace") or []:
        args = json.dumps(t.get("arguments") if t.get("arguments") is not None else t.get("arguments_raw"),
                          separators=(",", ":"))
        out.append(f"  step {t.get('step'):>2}.{t.get('call_index')} {t.get('tool'):<30} {t.get('status'):<18} "
                   f"{t.get('duration_ms', 0):>7.1f} ms  evidence={len(t.get('evidence_ids') or [])} "
                   f"live={t.get('live_data')}  args={args}")
        if t.get("result_summary") or t.get("error"):
            out.append(f"       -> {(t.get('result_summary') or t.get('error'))[:160]}")
    m = r.get("metrics") or {}
    out.append(f"latency:             first tool decision {r.get('time_to_first_tool_decision_ms')} ms | "
               f"model {r.get('model_latency_ms')} ms | tools {r.get('tool_latency_ms')} ms | "
               f"total {r.get('total_latency_ms')} ms | queue wait {r.get('queue_wait_ms')} ms | "
               f"client wall {wall_s * 1000:.0f} ms")
    out.append(f"model calls:         {m.get('model_calls')}  stop={m.get('stop_reason')}  "
               f"finish_reason={m.get('finish_reason')!r}  max prompt tokens={m.get('max_prompt_tokens')}  "
               f"context={((r.get('context_verification') or {}).get('status'))}")
    v = r.get("validation") or {}
    out.append(f"grounding:           facts {v.get('facts_grounded')}/{v.get('facts_proposed')} grounded, "
               f"{v.get('facts_rejected')} rejected; citation accuracy {v.get('citation_accuracy')}; "
               f"overclaim flags {v.get('overclaim_flag_count')}")
    unsupported = r.get("unsupported_claims") or []
    flags = [f for f in v.get("flags") or [] if f.get("code") not in ("model_omitted_field",)]
    if unsupported or flags:
        out.append("unsupported / flagged claims:")
        for u in unsupported:
            out.append(f"  REJECTED observed_fact: {str(u.get('claim'))[:160]} ({u.get('reason')}; "
                       f"cited {u.get('cited_ids')})")
        for f in flags:
            out.append(f"  FLAG {f.get('field')}: {f.get('code')}  {str(f.get('text') or '')[:120]}")
    else:
        out.append("unsupported / flagged claims: none")
    out.append(f"advisories:          {[a.get('code') for a in r.get('advisories') or []]}")
    out.append(f"headline:            {r.get('headline')}")
    out.append(f"voice (needs_review={voice.get('needs_review')}, source={voice.get('spoken_summary_source')}, "
               f"reasons={voice.get('review_reasons')}):")
    out.append(f"  \"{voice.get('spoken_summary')}\"")
    return out


async def main(a) -> int:
    key = _key(a.key_file)
    h = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(base_url=a.base, timeout=15) as c:
        health = (await c.get("/health", headers=h)).json()
        modes = health.get("evidence_modes") or {}
        live = (health.get("evidence_backends") or {}).get("live") or {}
        print(f"server: modes={modes} live_reachable={live.get('reachable')} memberships={live.get('memberships')} "
              f"degraded={health.get('degraded')}")
        if "live" not in (modes.get("available") or []):
            print("live evidence is not enabled on this server (start it with NI_EVIDENCE_BACKEND=spacetimedb)")
            return 2
        if not live.get("memberships"):
            print("the investigator identity has no workstation membership yet: waiting on the read-only grant")
            return 3
        if live.get("least_privilege_ok") is not True:
            print("the investigator identity is over-privileged (approval or owner rights): refusing; ask Sid "
                  "to grant reviewer with canApprove=false only")
            return 6
        simulated = None
        incident = a.incident
        if not incident:
            incident, simulated = await newest_incident(a.allow_simulated)
        if not incident:
            print("no usable incident is visible to the investigator identity")
            return 4
        t0 = time.perf_counter()
        r = await c.post("/investigations", headers=h, json={
            "incident_id": incident, "evidence_mode": "live",
            "requester": {"identity": "operator-live-e2e", "display_name": "live end-to-end check"}})
        if r.status_code != 202:
            print(f"create refused: HTTP {r.status_code} {r.text[:300]}")
            return 5
        inv = r.json()["investigation_id"]
        print(f"created {inv} for {incident} (X-Evidence-Mode: {r.headers.get('X-Evidence-Mode')})")
        full, header_mode = {}, None
        for _ in range(int(a.wait_s / 0.5)):
            g = await c.get(f"/investigations/{inv}", headers=h)
            full, header_mode = g.json(), g.headers.get("X-Evidence-Mode")
            if full.get("status") in FINISHED:
                break
            await asyncio.sleep(0.5)
        wall = time.perf_counter() - t0
        voice = (await c.get(f"/investigations/{inv}?view=voice", headers=h)).json()
    dest = ROOT / "var" / "live" / f"e2e_{inv}.json"
    _private_write(dest, {"full": full, "voice": voice, "client_wall_s": wall})
    print("\n".join(report(full, voice, header_mode, wall)))
    print(f"full record (private evidence, not for commit): {dest}")
    res = full.get("result") or {}
    if res.get("incident_simulated"):
        print("NOTE: the incident record is marked simulated=true (a LOCKI test row), not a real incident")
    return 0 if (res.get("evidence_mode") == "live" and res.get("risk_assessment") and not res.get("incident_simulated")
                 and full.get("status") in ("completed", "completed_with_warnings", "degraded")) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--incident", default=None)
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--wait-s", type=float, default=200.0)
    ap.add_argument("--allow-simulated", action="store_true", help="accept a simulated=true incident row")
    sys.exit(asyncio.run(main(ap.parse_args())))
