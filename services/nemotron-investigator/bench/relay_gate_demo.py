"""NVIDIA NeMo Relay policy-gate demo: a REAL blocked request and a REAL allowed request.

Part 1 (deterministic): call the approved tool get_recent_usb_events through the
service's normal execution path with the Relay gate enabled. One request inside
the policy is allowed; one beyond it is blocked by Relay before the tool body runs
(we prove the body did not run: the evidence backend read count does not change).

Part 2 (live model): run a real Nemotron investigation with the gate enabled and
record every Relay decision the model's own tool calls triggered.

Output: bench/results/relay_gate_demo.json
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from nemotron_investigator.agent import Requester, investigate
from nemotron_investigator.config import Settings
from nemotron_investigator.evidence.fixture import FixtureEvidenceSource
from nemotron_investigator.ollama_client import OllamaClient
from nemotron_investigator.relay_gate import GUARDRAIL_NAME, get_gate
from nemotron_investigator.tools import ToolContext, execute_tool_call

ROOT = Path(__file__).resolve().parents[1]
SCN = ROOT / "fixtures" / "scenarios"


async def part1() -> dict:
    gate = get_gate(600)
    path = SCN / "A_unknown_usb_armed.json"
    src = FixtureEvidenceSource(path)
    inc = await FixtureEvidenceSource(path).get_incident(json.loads(path.read_text())["tables"]["incidents"][0]["id"])
    ctx = ToolContext("inv_relay_demo", inc, src, relay_gate=gate)
    out = []
    for label, args in (("allowed (window_s=300 <= 600)", {"window_s": 300}),
                        ("blocked (window_s=1800 > 600)", {"window_s": 1800})):
        before = src.reads
        rec, content = await execute_tool_call(ctx, "get_recent_usb_events", args, step=1, call_index=len(out),
                                               timeout_s=5)
        out.append({"request": label, "arguments": args, "status": rec.status, "policy_gate": rec.policy_gate,
                    "error": rec.error, "evidence_ids": rec.evidence_ids, "duration_ms": rec.duration_ms,
                    "backend_reads_during_call": src.reads - before, "model_visible_result": json.loads(content)})
    sockets = subprocess.run(["lsof", "-a", "-p", str(os.getpid()), "-i"], capture_output=True, text=True).stdout
    return {"gate": gate.label, "guardrail": GUARDRAIL_NAME, "calls": out,
            "relay_decisions": list(gate.decisions), "open_network_sockets": sockets.strip() or "none"}


async def part2(scenario: str) -> dict:
    gate = get_gate(600)
    n0 = len(gate.decisions)
    s = replace(Settings.from_env(), relay_gate=True)
    path = SCN / f"{scenario}.json"
    iid = json.loads(path.read_text())["tables"]["incidents"][0]["id"]
    r = await investigate(iid, Requester("relay-demo", ("*",)), FixtureEvidenceSource(path),
                          OllamaClient(s.ollama_url, s.model), s)
    (ROOT / "bench" / "results" / f"relay_gate_live_{scenario}.json").write_text(json.dumps(r, indent=2, default=str))
    usb = [{"step": t["step"], "arguments": t["arguments"], "status": t["status"], "error": t["error"],
            "policy_gate": t["policy_gate"]} for t in r["tool_execution_trace"] if t["tool"] == "get_recent_usb_events"]
    return {"scenario": scenario, "investigation_id": r["investigation_id"], "status": r["investigation_status"],
            "policy_gate": r["policy_gate"], "usb_tool_calls": usb, "relay_decisions": gate.decisions[n0:],
            "tool_sequence": [f"{t['tool']}[{t['status']}]" for t in r["tool_execution_trace"]],
            "total_latency_ms": r["total_latency_ms"], "risk": (r.get("risk_assessment") or {}).get("level")}


async def main(live: bool):
    res = {"part1_deterministic": await part1()}
    if live:
        res["part2_live_nemotron"] = [await part2("B_unknown_person_then_usb"),
                                      await part2("C_suspicious_app_incomplete")]
    dest = ROOT / "bench" / "results" / "relay_gate_demo.json"
    dest.write_text(json.dumps(res, indent=2, default=str))
    for c in res["part1_deterministic"]["calls"]:
        print(f"[part1] {c['request']:<32} -> {c['status']:<14} backend reads during call: "
              f"{c['backend_reads_during_call']}  {c['error'] or ''}")
    print(f"[part1] process network sockets: {res['part1_deterministic']['open_network_sockets']}")
    if live:
        for p in res["part2_live_nemotron"]:
            print(f"[part2:{p['scenario'][:1]}] {p['status']}  {p['total_latency_ms']:.0f} ms  risk={p['risk']}")
            for u in p["usb_tool_calls"]:
                print(f"[part2:{p['scenario'][:1]}] Nemotron step {u['step']}: get_recent_usb_events"
                      f"{json.dumps(u['arguments'])} -> {u['status']} via {u['policy_gate']} {u['error'] or ''}")
    print(f"-> {dest}")


if __name__ == "__main__":
    import sys
    asyncio.run(main(live="--live" in sys.argv))
