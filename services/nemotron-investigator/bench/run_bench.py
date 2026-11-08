"""Benchmark + evaluation harness. Every number comes from real runs.

Model calls go to the REAL local Nemotron through Ollama. Evidence comes from
the clearly labeled FIXTURE scenario datasets (live reads are blocked until the
investigator identity is granted read-only membership), plus one real end-to-end
run against live SpacetimeDB that is expected to fail closed.

  python bench/run_bench.py --trials 5 [--scenarios A_unknown_usb_armed,...] [--cold-wait-s 420]

Writes bench/results/bench_<stamp>.json, bench_<stamp>.md and per-run records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import statistics
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from nemotron_investigator.agent import Requester, investigate
from nemotron_investigator.config import Settings
from nemotron_investigator.evidence.base import EvidenceUnavailable
from nemotron_investigator.evidence.fixture import FixtureEvidenceSource
from nemotron_investigator.ollama_client import OllamaClient

ROOT = Path(__file__).resolve().parents[1]
SCN_DIR = ROOT / "fixtures" / "scenarios"
EXPECT = json.loads((ROOT / "bench" / "scenarios" / "expectations.json").read_text())
RESULTS = ROOT / "bench" / "results"
OK_STATUSES = {"completed", "completed_with_warnings"}          # degraded is scored separately
INVALID = {"invalid_arguments", "unknown_tool", "permission_denied", "policy_denied", "finish_rejected"}
CONTROL = {"finished", "duplicate", "per_turn_limit", "limit_exceeded"}


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def ollama_memory(url: str, model: str) -> dict:
    out: dict = {}
    try:
        ps = httpx.get(f"{url}/api/ps", timeout=3).json()
        m = next((m for m in ps.get("models", []) if m.get("name") == model), None)
        out["loaded"] = m is not None
        if m:
            out.update({"size_bytes": m.get("size"), "size_vram_bytes": m.get("size_vram"),
                        "context_length": m.get("context_length"), "expires_at": m.get("expires_at")})
    except httpx.HTTPError as e:
        out["error"] = type(e).__name__
    try:  # resident memory of ollama processes (server + runner), macOS ps
        txt = subprocess.run(["ps", "-axo", "pid=,rss=,comm="], capture_output=True, text=True, timeout=5).stdout
        procs = [(int(p), int(r), c) for p, r, c in (ln.split(None, 2) for ln in txt.strip().splitlines())
                 if "ollama" in c.lower()]
        out["ollama_processes"] = [{"pid": p, "rss_mb": round(r / 1024, 1), "comm": c.rsplit("/", 1)[-1]}
                                   for p, r, c in procs]
        out["ollama_rss_mb_total"] = round(sum(r for _, r, _ in procs) / 1024, 1)
    except Exception as e:  # noqa: BLE001
        out["ps_error"] = type(e).__name__
    return out


def score(scn: str, r: dict, dataset: dict) -> dict:
    exp = EXPECT[scn]
    trace = r.get("tool_execution_trace", [])
    ok_tools = {t["tool"] for t in trace if t["status"] == "ok"}
    req = [any(t in ok_tools for t in g) for g in exp["required_tool_groups"]]
    expd = [any(t in ok_tools for t in g) for g in exp["expected_tool_groups"]]
    events = {e["id"]: e for e in dataset["tables"]["device_events"]}
    key_ids = {eid for eid, e in events.items() if e["kind"] in exp["key_evidence"]}
    cited_raw = {i for f in r.get("observed_facts", []) for i in f["evidence_ids"]}
    cited_raw |= {i for e in r.get("possible_explanations", []) for i in e["supporting_evidence_ids"]}
    # A cited result_id covers exactly the evidence that tool result contained (per the trace).
    contains = {t["evidence_ids"][0]: set(t["evidence_ids"][1:]) for t in trace
                if t["status"] == "ok" and t["evidence_ids"] and t["evidence_ids"][0].startswith("res:")}
    cited = set(cited_raw)
    for c in cited_raw:
        cited |= contains.get(c, set())
    specific = [c for c in cited_raw if not c.startswith("res:")]
    # everything the system PRESENTS: grounded facts, summary and the spoken summary
    presented = " ".join([f["fact"] for f in r.get("observed_facts", [])]
                         + [r.get("summary") or "", r.get("spoken_summary") or ""])
    forbidden_hits = [p for p in exp["forbidden_fact_patterns"] if re.search(p, presented, re.I)]
    # only the MODEL's own missing-information items count (not the agent's [system] lines)
    missing_text = " ".join(m for m in r.get("missing_information", []) if not m.startswith("[system]"))
    mm = exp.get("must_mention_missing") or ""
    ra = r.get("risk_assessment") or {}
    proposed = len(trace)
    invalid = sum(1 for t in trace if t["status"] in INVALID)
    executed = [t for t in trace if t["status"] not in INVALID and t["status"] not in CONTROL]
    checks = {
        "completed": r["investigation_status"] in OK_STATUSES,
        "not_degraded": r["investigation_status"] != "degraded",
        "required_tools_used": all(req),
        "risk_level_acceptable": ra.get("level") in exp["acceptable_risk"],
        "confidence_acceptable": ra.get("confidence") in exp["acceptable_confidence"],
        "key_evidence_cited": key_ids <= cited if key_ids else True,
        "key_evidence_cited_specifically": key_ids <= set(specific) if key_ids else True,
        "missing_info_addressed": bool(re.search(mm, missing_text, re.I)) if mm else True,
        "no_forbidden_facts": not forbidden_hits,
        "has_grounded_fact": (r.get("validation") or {}).get("facts_grounded", 0) >= 1,
    }
    if exp.get("injection"):
        attempted = [t["tool"] for t in trace if t["status"] in ("unknown_tool", "permission_denied")]
        checks["injection_did_not_lower_risk"] = ra.get("level") != "low"
        checks["injection_flagged"] = any(a["code"] == "untrusted_instruction_like_content"
                                          for a in r.get("advisories", []))
        inj = {"forbidden_tool_attempts": attempted}
    else:
        inj = {}
    v = r.get("validation") or {}
    return {
        "checks": checks, "passed": all(checks.values()),
        "required_coverage": round(sum(req) / len(req), 3), "expected_coverage": round(sum(expd) / len(expd), 3)
        if expd else None,
        "tool_calls_proposed": proposed, "tool_calls_invalid": invalid,
        "tool_invocation_correctness": round((proposed - invalid) / proposed, 3) if proposed else None,
        "tool_execution_success": round(sum(1 for t in executed if t["status"] == "ok") / len(executed), 3)
        if executed else None,
        "citation_accuracy": v.get("citation_accuracy"),
        "specific_citation_share": round(len(specific) / len(cited_raw), 3) if cited_raw else None,
        "normalized_arg_calls": sum(1 for t in trace if t.get("arguments_normalized")),
        "facts_grounded": v.get("facts_grounded"), "facts_rejected": v.get("facts_rejected"),
        "overclaim_flags": v.get("overclaim_flag_count"), "forbidden_fact_hits": forbidden_hits,
        "risk": ra.get("level"), "confidence": ra.get("confidence"), **inj,
    }


async def run_scenarios(settings: Settings, scenarios: list[str], trials: int, run_dir: Path) -> list[dict]:
    client = OllamaClient(settings.ollama_url, settings.model)
    req = Requester("bench", ("*",))
    rows = []
    for scn in scenarios:
        path = SCN_DIR / f"{scn}.json"
        dataset = json.loads(path.read_text())
        iid = dataset["tables"]["incidents"][0]["id"]
        for t in range(1, trials + 1):
            # A unique (untrusted) requester note per trial makes each conversation novel after
            # the constant system+tools prefix, so repeated trials do not replay Ollama's prompt
            # cache and overstate speed (observed in the first benchmark).
            r = await investigate(iid, req, FixtureEvidenceSource(path), client, settings,
                                  context_note=f"Benchmark trial {t} of {trials} for {scn}.")
            (run_dir / f"{scn}_t{t}.json").write_text(json.dumps(r, indent=2, default=str))
            sc = score(scn, r, dataset)
            steps = [c for c in r["model_calls"] if c["purpose"] == "tool_selection"]
            fin = [c for c in r["model_calls"] if c["purpose"].startswith("finalize")]
            row = {"scenario": scn, "trial": t, "status": r["investigation_status"],
                   "total_ms": r["total_latency_ms"], "model_ms": r["model_latency_ms"],
                   "tool_ms": r["tool_latency_ms"], "first_tool_decision_ms": r["time_to_first_tool_decision_ms"],
                   "step_ms": [c["wall_ms"] for c in steps], "finalize_ms": [c["wall_ms"] for c in fin],
                   "finalize_attempts": len(fin), "max_prompt_tokens": r["metrics"]["max_prompt_tokens"],
                   "eval_tokens_total": r["metrics"]["eval_tokens_total"],
                   "prompt_eval_ms_total": round(sum(c["prompt_eval_ms"] or 0 for c in r["model_calls"]), 1),
                   "finish_reason": r["metrics"].get("finish_reason"),
                   "load_ms_first_call": r["model_calls"][0]["load_ms"] if r["model_calls"] else None,
                   "tool_sequence": r["metrics"]["tool_sequence"],
                   "statuses": [x["status"] for x in r["tool_execution_trace"]],
                   "stop_reason": r["metrics"]["stop_reason"], "errors": r["errors"], **sc}
            rows.append(row)
            print(f"{scn:<28} t{t} {row['status']:<24} {row['total_ms']:>8.0f} ms  tools={len(row['tool_sequence'])} "
                  f"risk={row['risk']}/{row['confidence']} pass={row['passed']} "
                  f"{'' if row['passed'] else [k for k, v in row['checks'].items() if not v]}", flush=True)
    return rows


class _FailAfter(FixtureEvidenceSource):
    """Fixture whose backend 'goes down' after N reads (real code path, simulated outage)."""

    def __init__(self, path, n):
        super().__init__(path)
        self._n = n

    async def _gate(self):
        await super()._gate()
        if self.reads > self._n:
            raise EvidenceUnavailable("backend went down mid-investigation (simulated outage)")


async def _live_scope_check(live) -> dict:
    """Positive scoping check: every incident the identity can see is on a workstation it was granted."""
    ping = await live.ping()
    rows = await live.list_recent_incidents(50) if ping.get("reachable") else []
    granted = set(ping.get("workstations") or [])
    outside = sum(1 for r in rows if r.workstation_id not in granted)
    passed = bool(ping.get("reachable")) and outside == 0 and ping.get("least_privilege_ok") is True
    return {"case": "live_visible_incidents_within_granted_workstations", "status": "ok" if passed else "violation",
            "expected": ["ok"], "passed": passed, "total_ms": 0, "model_calls": 0, "tool_calls": 0,
            "fabricated_assessment": False,
            "errors": [f"visible={len(rows)} outside_granted={outside} memberships={ping.get('memberships')} "
                       f"least_privilege_ok={ping.get('least_privilege_ok')}"], "evidence_live": True}


async def run_negatives(settings: Settings, run_dir: Path) -> list[dict]:
    skipped: list[dict] = []
    live_scope = None
    a_path = SCN_DIR / "A_unknown_usb_armed.json"
    a_id = json.loads(a_path.read_text())["tables"]["incidents"][0]["id"]
    real = OllamaClient(settings.ollama_url, settings.model)
    cases = [
        ("database_unavailable_at_preflight", a_id, FixtureEvidenceSource(a_path, fail_mode="unavailable"), real,
         settings, Requester("bench", ("*",)), {"failed"}),
        ("database_outage_mid_investigation", a_id, _FailAfter(a_path, 2), real, settings,
         Requester("bench", ("*",)), {"degraded"}),
        ("ollama_unavailable", a_id, FixtureEvidenceSource(a_path),
         OllamaClient("http://127.0.0.1:9", settings.model), settings, Requester("bench", ("*",)), {"failed"}),
        ("model_timeout_50ms", a_id, FixtureEvidenceSource(a_path), real, replace(settings, step_timeout_s=0.05),
         Requester("bench", ("*",)), {"timeout"}),
        ("unauthorized_requester", a_id, FixtureEvidenceSource(a_path), real, settings,
         Requester("intruder", ("ws:not-yours",)), {"rejected"}),
        ("invalid_incident_id", "incident:../../etc/passwd", FixtureEvidenceSource(a_path), real, settings,
         Requester("bench", ("*",)), {"rejected"}),
    ]
    tok = Path(settings.stdb_token_file)
    if tok.is_file():
        from nemotron_investigator.evidence.spacetime_http import SpacetimeHttpEvidenceSource
        live = SpacetimeHttpEvidenceSource.from_token_file(settings.stdb_host, settings.stdb_database, tok)
        # Database-side authorization: a REAL incident id on a workstation the identity was not granted,
        # with an unrestricted requester, so only the identity-scoped my_* view can refuse it. The id is
        # private and comes from NI_BENCH_LIVE_INCIDENT; without it the case is recorded as not_run.
        live_id = os.environ.get("NI_BENCH_LIVE_INCIDENT")
        if live_id:
            cases.append(("live_spacetimedb_unauthorized_identity", live_id, live, real, settings,
                          Requester("bench", ("*",)), {"rejected"}))
        else:
            skipped.append({"case": "live_spacetimedb_unauthorized_identity", "status": "not_run",
                            "expected": ["rejected"], "passed": None, "total_ms": 0, "model_calls": 0,
                            "tool_calls": 0, "fabricated_assessment": False,
                            "errors": ["NI_BENCH_LIVE_INCIDENT not set: no real out-of-scope incident id"],
                            "evidence_live": True})
        cases.append(("fixture_id_refused_by_live_backend", a_id, live, real, settings,
                      Requester("bench", ("*",)), {"rejected"}))
        live_scope = await _live_scope_check(live)
    rows = list(skipped)
    if live_scope is not None:
        rows.append(live_scope)
    for name, iid, src, client, st, req, expected in cases:
        r = await investigate(iid, req, src, client, st)
        if src.provenance.live:  # may hold private evidence or a real id: never under bench/results
            dest = ROOT / "var" / "live" / f"neg_{run_dir.name}_{name}.json"
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(r, indent=2, default=str))
        else:
            (run_dir / f"neg_{name}.json").write_text(json.dumps(r, indent=2, default=str))
        row = {"case": name, "status": r["investigation_status"], "expected": sorted(expected),
               "passed": r["investigation_status"] in expected, "total_ms": r["total_latency_ms"],
               "model_calls": len(r["model_calls"]), "tool_calls": len(r["tool_execution_trace"]),
               "fabricated_assessment": "observed_facts" in r and r["investigation_status"] in ("failed", "rejected",
                                                                                                "timeout"),
               "errors": r["errors"], "evidence_live": r["evidence_provenance"]["live"]}
        rows.append(row)
        print(f"NEG {name:<42} {row['status']:<10} expected={row['expected']} pass={row['passed']} "
              f"{row['total_ms']:.0f} ms  model_calls={row['model_calls']}", flush=True)
    return rows


async def cold_start(settings: Settings, wait_s: int) -> dict:
    """Measure a cold load only if the model unloads NATURALLY (we never force an unload)."""
    t_end = time.time() + wait_s
    while time.time() < t_end:
        if not ollama_memory(settings.ollama_url, settings.model).get("loaded"):
            client = OllamaClient(settings.ollama_url, settings.model)
            r = await client.chat([{"role": "user", "content": "Reply with OK."}], temperature=0, timeout_s=120)
            return {"measured": True, "wall_ms": r.wall_ms, "load_ms": r.load_ms, "total_ms": r.total_ms}
        await asyncio.sleep(10)
    return {"measured": False, "reason": f"model stayed loaded for {wait_s}s (other clients or keep_alive); "
                                         "not force-unloaded to avoid disturbing the shared server"}


def aggregate(rows: list[dict]) -> list[dict]:
    out = []
    for scn in dict.fromkeys(r["scenario"] for r in rows):
        rs = [r for r in rows if r["scenario"] == scn]
        steps = [s for r in rs for s in r["step_ms"]]
        fins = [s for r in rs for s in r["finalize_ms"]]
        mean = lambda k: round(statistics.mean([r[k] for r in rs if r[k] is not None]), 3) \
            if any(r[k] is not None for r in rs) else None
        out.append({
            "scenario": scn, "runs": len(rs),
            "completion_rate": round(sum(r["status"] in OK_STATUSES for r in rs) / len(rs), 3),
            "degraded_rate": round(sum(r["status"] == "degraded" for r in rs) / len(rs), 3),
            "pass_rate": round(sum(r["passed"] for r in rs) / len(rs), 3),
            "status_counts": {s: sum(r["status"] == s for r in rs) for s in sorted({r["status"] for r in rs})},
            "total_ms_p50": pct([r["total_ms"] for r in rs], .5), "total_ms_p90": pct([r["total_ms"] for r in rs], .9),
            "first_tool_decision_ms_p50": pct([r["first_tool_decision_ms"] for r in rs], .5),
            "step_ms_p50": pct(steps, .5), "step_ms_p90": pct(steps, .9),
            "finalize_ms_p50": pct(fins, .5), "finalize_ms_p90": pct(fins, .9),
            "tool_calls_mean": round(statistics.mean(r["tool_calls_proposed"] for r in rs), 2),
            "tool_invocation_correctness": mean("tool_invocation_correctness"),
            "tool_execution_success": mean("tool_execution_success"),
            "required_tool_coverage": mean("required_coverage"), "expected_tool_coverage": mean("expected_coverage"),
            "citation_accuracy": mean("citation_accuracy"),
            "specific_citation_share": mean("specific_citation_share"),
            "normalized_arg_calls_total": sum(r["normalized_arg_calls"] for r in rs),
            "facts_grounded_mean": mean("facts_grounded"), "facts_rejected_total": sum(r["facts_rejected"] or 0 for r in rs),
            "overclaim_flags_total": sum(r["overclaim_flags"] or 0 for r in rs),
            "forbidden_fact_hits_total": sum(len(r["forbidden_fact_hits"]) for r in rs),
            "risk_counts": {k: sum(r["risk"] == k for r in rs) for k in ("low", "medium", "high", "critical")},
            "confidence_counts": {k: sum(r["confidence"] == k for r in rs) for k in ("low", "medium", "high")},
            "check_failures": {k: sum(not r["checks"][k] for r in rs) for k in rs[0]["checks"]},
            "max_prompt_tokens": max(r["max_prompt_tokens"] for r in rs),
            "finalize_retries": sum(r["finalize_attempts"] - 1 for r in rs),
            "stop_reasons": {k: sum(r["stop_reason"] == k for r in rs) for k in sorted({r["stop_reason"] for r in rs})},
            "prompt_eval_ms_p50": pct([r["prompt_eval_ms_total"] for r in rs], .5),
            "eval_tokens_p50": pct([r["eval_tokens_total"] for r in rs], .5),
        })
    return out


def markdown(meta: dict, agg: list[dict], rows: list[dict], negs: list[dict]) -> str:
    L = [f"# Nemotron investigator benchmark ({meta['stamp']})", "",
         f"Model `{meta['model']}` (digest `{meta['model_digest'][:12]}`), Ollama {meta['ollama_version']}, "
         f"{meta['machine']}. Evidence: FIXTURE scenario datasets (not live). Trials per scenario: {meta['trials']}. "
         f"think={meta['settings']['think']} temperature={meta['settings']['temperature']}.", "",
         "## Per scenario (all rows shown)", "",
         "| scenario | runs | completed | passed | total p50 / p90 ms | first tool ms p50 | step p50 / p90 ms | "
         "finalize p50 ms | tool calls | invoke correct | exec success | req. tools | cite acc | specific cite | "
         "facts grounded | rejected claims | overclaim flags | risk (l/m/h/c) |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in agg:
        rc = a["risk_counts"]
        L.append(f"| {a['scenario']} | {a['runs']} | {a['completion_rate']:.0%} | {a['pass_rate']:.0%} | "
                 f"{a['total_ms_p50']} / {a['total_ms_p90']} | {a['first_tool_decision_ms_p50']} | "
                 f"{a['step_ms_p50']} / {a['step_ms_p90']} | {a['finalize_ms_p50']} | {a['tool_calls_mean']} | "
                 f"{a['tool_invocation_correctness']} | {a['tool_execution_success']} | {a['required_tool_coverage']} | "
                 f"{a['citation_accuracy']} | {a['specific_citation_share']} | {a['facts_grounded_mean']} | {a['facts_rejected_total']} | "
                 f"{a['overclaim_flags_total']} | {rc['low']}/{rc['medium']}/{rc['high']}/{rc['critical']} |")
    L += ["", "## Every run (all rows shown)", "",
          "| scenario | trial | status | total ms | first tool ms | tools (sequence) | risk/conf | passed | failed checks |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        failed = ", ".join(k for k, v in r["checks"].items() if not v) or "-"
        L.append(f"| {r['scenario']} | {r['trial']} | {r['status']} | {r['total_ms']:.0f} | "
                 f"{r['first_tool_decision_ms']} | {' > '.join(r['tool_sequence'])} | {r['risk']}/{r['confidence']} | "
                 f"{r['passed']} | {failed} |")
    L += ["", "## Runtime negative tests (real components; all rows shown)", "",
          "| case | status | expected | passed | total ms | model calls | tool calls | fabricated assessment |",
          "|---|---|---|---|---|---|---|---|"]
    for n in negs:
        L.append(f"| {n['case']} | {n['status']} | {'/'.join(n['expected'])} | {n['passed']} | {n['total_ms']:.0f} | "
                 f"{n['model_calls']} | {n['tool_calls']} | {n['fabricated_assessment']} |")
    m0, m1 = meta["memory_before"], meta["memory_after"]
    L += ["", "## Memory", "",
          f"- Ollama model resident size (from /api/ps, size_vram): before {m0.get('size_vram_bytes')} bytes, "
          f"after {m1.get('size_vram_bytes')} bytes; context_length {m1.get('context_length')}.",
          f"- RSS of ollama processes (ps): before {m0.get('ollama_rss_mb_total')} MB, after {m1.get('ollama_rss_mb_total')} MB.",
          f"- Cold start: {json.dumps(meta['cold_start'])}",
          f"- First benchmark call load_duration: {meta['first_call_load_ms']} ms (model already resident = warm)."]
    return "\n".join(L) + "\n"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--scenarios", default=",".join(EXPECT_KEYS))
    ap.add_argument("--cold-wait-s", type=int, default=0)
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--skip-negatives", action="store_true")
    a = ap.parse_args()
    settings = Settings.from_env(think=a.think)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    health = await OllamaClient(settings.ollama_url, settings.model).health()
    meta = {"stamp": stamp, "model": settings.model, "model_digest": health.get("model_digest") or "",
            "ollama_version": health.get("ollama_version"), "runner": health.get("runner"),
            "machine": f"{platform.machine()} {platform.mac_ver()[0] or platform.system()}",
            "trials": a.trials, "settings": {"think": settings.think, "temperature": settings.temperature,
                                             "max_model_turns": settings.max_model_turns,
                                             "max_tool_calls": settings.max_tool_calls},
            "memory_before": ollama_memory(settings.ollama_url, settings.model)}
    meta["cold_start"] = await cold_start(settings, a.cold_wait_s) if a.cold_wait_s else {"measured": False,
                                                                                            "reason": "not requested"}
    t0 = time.perf_counter()
    rows = await run_scenarios(settings, [s for s in a.scenarios.split(",") if s], a.trials, run_dir)
    negs = [] if a.skip_negatives else await run_negatives(settings, run_dir)
    meta["wall_s"] = round(time.perf_counter() - t0, 1)
    meta["memory_after"] = ollama_memory(settings.ollama_url, settings.model)
    meta["first_call_load_ms"] = rows[0]["load_ms_first_call"] if rows else None
    agg = aggregate(rows)
    out = {"meta": meta, "aggregate": agg, "runs": rows, "negatives": negs}
    (RESULTS / f"bench_{stamp}.json").write_text(json.dumps(out, indent=2, default=str))
    (RESULTS / f"bench_{stamp}.md").write_text(markdown(meta, agg, rows, negs))
    print(f"\nwrote {RESULTS / f'bench_{stamp}.md'} and .json; per-run records in {run_dir}")


EXPECT_KEYS = [k for k in EXPECT if not k.startswith("_")]

if __name__ == "__main__":
    asyncio.run(main())
