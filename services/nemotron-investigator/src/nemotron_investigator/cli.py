"""Command line entry points.

  python -m nemotron_investigator run --dataset fixtures/scenarios --incident incident:fixture-a-unknown-usb
  python -m nemotron_investigator run --live --incident incident:<32 hex> [--out var/live/run.json]
  python -m nemotron_investigator health
  python -m nemotron_investigator serve
  python -m nemotron_investigator create-key --requester dhruv-m4-backend --workstation '*' \
      [--scope health:admin ...] [--out .secrets/handoff/dhruv-m4-backend.key]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent import Requester, investigate
from .config import Settings
from .ollama_client import OllamaClient


def _print_run(r: dict) -> None:
    print(f"[{r.get('evidence_badge')}] investigation {r['investigation_id']}  status={r['investigation_status']}  "
          f"source={r['evidence_provenance']['label']}")
    if r.get("official_severity"):
        off, eff = r["official_severity"], r.get("effective_severity") or {}
        print(f"  official severity={off.get('level') or off.get('raw')} (rule {off.get('rule_id')})  "
              f"effective={eff.get('level')} ({eff.get('basis')})")
    for c in r["model_calls"]:
        names = ", ".join(f"{p['name']}({json.dumps(p['arguments'], separators=(',', ':'))})"
                          for p in c["tool_calls_proposed"]) or (c["content_preview"][:60] or "-")
        print(f"  model step {c['step']:>2} {c['purpose']:<15} {c['wall_ms']:>8.1f} ms  "
              f"prompt={c['prompt_tokens']} out={c['eval_tokens']}  -> {names}")
    for t in r["tool_execution_trace"]:
        print(f"  tool  step {t['step']:>2} {t['tool']:<30} {t['status']:<18} {t['duration_ms']:>7.2f} ms  "
              f"{t['result_summary'] or t['error'] or ''}")
    v = r.get("validation") or {}
    print(f"  model_latency_ms={r['model_latency_ms']}  total_latency_ms={r['total_latency_ms']}  "
          f"first_tool_decision_ms={r['time_to_first_tool_decision_ms']}")
    if r.get("risk_assessment"):
        ra = r["risk_assessment"]
        print(f"  risk={ra['level']} confidence={ra['confidence']}  facts={v.get('facts_grounded')}/"
              f"{v.get('facts_proposed')} grounded  citation_accuracy={v.get('citation_accuracy')}  "
              f"flags={len(v.get('flags', []))}")
        print(f"  summary: {r.get('summary')}")
    if r["errors"]:
        print(f"  errors: {r['errors']}")


async def _run(args) -> int:
    settings = Settings.from_env()
    if args.think:
        from dataclasses import replace
        settings = replace(settings, think=True)
    if args.live:
        from .evidence.spacetime_http import SpacetimeHttpEvidenceSource
        source = SpacetimeHttpEvidenceSource.from_token_file(settings.stdb_host, settings.stdb_database,
                                                             settings.stdb_token_file,
                                                             timeout_s=settings.tool_timeout_s)
    else:
        from .evidence.fixture import FixtureEvidenceSource
        source = FixtureEvidenceSource(args.dataset or settings.fixture_path)
    client = OllamaClient(settings.ollama_url, settings.model)
    req = Requester(args.requester, tuple(args.workstation))
    r = await investigate(args.incident, req, source, client, settings, context_note=args.note)
    _print_run(r)
    if args.out:
        import os
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # live records hold private evidence
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(r, indent=2, default=str))
        print(f"  full record -> {args.out}")
    return 0 if r["investigation_status"] in ("completed", "completed_with_warnings") else 1


async def _health() -> int:
    s = Settings.from_env()
    h = await OllamaClient(s.ollama_url, s.model).health()
    print(json.dumps(h, indent=2))
    return 0 if h.get("reachable") and h.get("model_installed") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nemotron_investigator")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one investigation (fixture dataset, or --live SpacetimeDB)")
    r.add_argument("--dataset", default=None, help="fixture file or directory (default: fixtures/scenarios)")
    r.add_argument("--live", action="store_true", help="read live evidence with the investigator identity")
    r.add_argument("--incident", required=True)
    r.add_argument("--requester", default="cli-local")
    r.add_argument("--workstation", action="append", default=None)
    r.add_argument("--note", default=None)
    r.add_argument("--think", action="store_true")
    r.add_argument("--out", default=None)
    sub.add_parser("health")
    sub.add_parser("serve")
    k = sub.add_parser("create-key", help="create an API key for a requester (printed once, or --out file)")
    k.add_argument("--requester", required=True)
    k.add_argument("--workstation", action="append", required=True)
    k.add_argument("--scope", action="append", default=None,
                   help="repeatable; default: investigations:create, investigations:read, health:read")
    k.add_argument("--out", default=None, help="write the key to this new file (mode 0600) instead of printing")
    args = p.parse_args(argv)
    if args.cmd == "run":
        args.workstation = args.workstation or ["*"]
        return asyncio.run(_run(args))
    if args.cmd == "health":
        return asyncio.run(_health())
    if args.cmd == "serve":
        from .api import serve
        return serve()
    if args.cmd == "create-key":
        from .auth import create_key
        return create_key(args.requester, args.workstation, scopes=args.scope, out=args.out)
    return 2


if __name__ == "__main__":
    sys.exit(main())
