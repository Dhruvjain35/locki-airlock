"""Render a REAL investigation record (JSON written by the CLI/benchmark/API) as Markdown.

Pure formatting of recorded data; nothing is re-run or invented.

  python bench/render_trace.py bench/results/runs/<stamp>/A_unknown_usb_armed_t1.json > A_trace.md
"""

from __future__ import annotations

import json
import sys


def _args(a) -> str:
    return json.dumps(a, separators=(",", ":")) if a is not None else "{}"


def render(r: dict) -> str:
    prov = r.get("evidence_provenance", {})
    off = r.get("official_severity") or {}
    eff = r.get("effective_severity") or {}
    L = [f"# Investigation `{r['investigation_id']}`  [{r.get('evidence_badge') or 'UNVERIFIED'}]", "",
         f"> {r.get('evidence_banner') or 'evidence mode not recorded'}", "",
         f"**{r.get('headline') or 'no headline (no assessment)'}**", "",
         f"- Incident: `{r['incident_id']}`" + (" (record marked simulated=true)" if r.get("incident_simulated") else ""),
         f"- Official severity (LOCKI rule, authoritative): **{off.get('level') or off.get('raw')}** "
         f"(rule `{off.get('rule_id')}`); effective severity **{eff.get('level')}** ({eff.get('basis')})",
         f"- Model: `{r['model_id']}` (local Ollama on the M5)",
         f"- Status: **{r['investigation_status']}**",
         f"- Evidence source: {prov.get('label')} (live={prov.get('live')})",
         f"- Total {r['total_latency_ms']:.0f} ms; model {r['model_latency_ms']:.0f} ms; tools "
         f"{r['tool_latency_ms']:.1f} ms; first tool decision {r['time_to_first_tool_decision_ms']} ms", ""]
    sys_prompt = next((m["content"] for m in r.get("conversation", []) if m["role"] == "system"), "")
    first_user = next((m["content"] for m in r.get("conversation", []) if m["role"] == "user"), "")
    L += ["## What Nemotron was given", "", "System prompt (abridged to first line):",
          f"> {sys_prompt.splitlines()[0] if sys_prompt else ''}", "", "User message (the entire task input):",
          f"> {first_user}", "", "Plus the JSON schemas of 7 read-only tools. No evidence was pre-assembled.", "",
          "## Step-by-step", ""]
    trace = r.get("tool_execution_trace", [])
    for c in r.get("model_calls", []):
        if c["purpose"] == "tool_selection":
            props = c["tool_calls_proposed"]
            L.append(f"### Step {c['step']}: Nemotron inference {c['wall_ms']:.0f} ms "
                     f"(prompt {c['prompt_tokens']} tok, output {c['eval_tokens']} tok)")
            if c.get("thinking"):
                L.append(f"> Nemotron reasoning: {c['thinking'][:600]}")
            if not props:
                L.append(f"Decision: **stop gathering**. Model replied `{c['content_preview'][:40]}`.")
            for p in props:
                L.append(f"- Selected `{p['name']}` with arguments `{_args(p['arguments'])}`")
            for t in [t for t in trace if t["step"] == c["step"]]:
                extra = " (arguments canonicalized)" if t.get("arguments_normalized") else ""
                L.append(f"  - Executed `{t['tool']}`: **{t['status']}** in {t['duration_ms']:.2f} ms{extra}: "
                         f"{t['result_summary'] or t.get('error') or ''}")
                if t["evidence_ids"]:
                    L.append(f"    - evidence: {', '.join('`' + e + '`' for e in t['evidence_ids'][:6])}")
            L.append("")
        else:
            L.append(f"### Final assessment call ({c['purpose']}): {c['wall_ms']:.0f} ms, "
                     f"{c['eval_tokens']} output tokens, constrained JSON decoding")
            L.append("")
    if "risk_assessment" in r:
        ra = r["risk_assessment"]
        try:  # the exact text LOCKI's voice would speak (same function the API serves)
            from nemotron_investigator.api import voice_view
            spoken = voice_view({"investigation_id": r["investigation_id"], "status": r["investigation_status"],
                                 "incident_id": r["incident_id"], "result": r})["spoken_summary"]
        except Exception:  # noqa: BLE001 - formatting only
            spoken = None
        L += ["## Assessment (after deterministic grounding)", "",
              f"**Official severity:** {off.get('level') or off.get('raw')} (LOCKI rule; never changed by Nemotron)", "",
              f"**Nemotron's risk (its own view):** {ra['level']} (confidence {ra['confidence']}). {ra['rationale']}", "",
              f"**Voice view text (what LOCKI speaks):** {spoken}", "",
              f"**Summary ({r.get('summary_source')}):** {r.get('summary')}", "",
              "**Observed facts (each cites retrieved evidence):**"]
        L += [f"- {f['fact']} [{', '.join(f['evidence_ids'])}]" for f in r.get("observed_facts", [])] or ["- (none)"]
        L += ["", "**Possible explanations (inferred):**"]
        L += [f"- ({e['likelihood']}) {e['explanation']}" for e in r.get("possible_explanations", [])] or ["- (none)"]
        L += ["", "**Missing information (unknown):**"]
        L += [f"- {m}" for m in r.get("missing_information", [])] or ["- (none)"]
        L += ["", "**Recommended actions (all require human approval; none executed by the agent):**"]
        L += [f"- {a['action']}: {a['rationale']}" for a in r.get("recommended_actions", [])] or ["- (none)"]
        if r.get("unsupported_claims"):
            L += ["", "**Claims rejected by grounding:**"]
            L += [f"- {u['claim']} ({u['reason']})" for u in r["unsupported_claims"]]
        if r.get("advisories"):
            L += ["", "**Advisories:**"] + [f"- `{a['code']}`: {a['detail']}" for a in r["advisories"]]
        v = r.get("validation") or {}
        L += ["", f"Validation: {v.get('facts_grounded')}/{v.get('facts_proposed')} facts grounded, citation accuracy "
                  f"{v.get('citation_accuracy')}, {len(v.get('flags', []))} flags."]
    else:
        L += ["## No assessment", "", f"Errors: {r.get('errors')}"]
    if r.get("timeline"):
        L += ["", "## Evidence timeline (rebuilt deterministically from retrieved evidence)", "",
              "| t (s from incident) | time (UTC) | evidence | event |", "|---|---|---|---|"]
        L += [f"| {e['t_rel_s']:+.1f} | {e['time']} | `{e['evidence_id']}` | {e['label']} |" for e in r["timeline"]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    print(render(json.load(open(sys.argv[1]))))
