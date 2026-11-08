# Demo runbook: NVIDIA track (Nemotron multi-step investigation)

Everything below runs on the M5 against the local Nemotron, `nemotron-3.5-bolt:e4b` on Ollama. There are two evidence modes, and every screen says which one is in use:

- **LIVE**: the real LOCKI SpacetimeDB through the read-only investigator identity. This needs the workstation owner's `grant_incident_evidence` for the incident (INTEGRATION.md §7).
- **TEST DATA**: the fixture scenarios. Fixture ids are `incident:fixture-*` and can never be confused with a real incident.

## 1. Live end-to-end (the success criterion)

The flow is: real SpacetimeDB incident → Nemotron calls the evidence tools → structured assessment → LOCKI voice.

```bash
cd ~/locki-airlock-nemotron/services/nemotron-investigator
scripts/serve.sh --daemon                                      # API on 127.0.0.1:8787, live by default
.venv/bin/python bench/wait_for_grant.py --max-s 60            # 0 = an incident grant is visible; 4 = over-privileged
.venv/bin/python bench/live_e2e.py --key-file .secrets/handoff/mustafa-operator.key
#   newest visible incident; or pass --incident incident:<32 hex>
```

`live_e2e.py` creates the run exactly as Dhruv's backend does (`evidence_mode: "live"`) and prints:
- the incident id;
- every tool call Nemotron chose (arguments, status, evidence count, milliseconds, `live=True`);
- latency;
- the official severity next to Nemotron's risk;
- every unsupported or flagged claim;
- the exact text LOCKI's voice will speak.

The full record, which contains private evidence, stays in `var/live/`.

On Dhruv's side the voice layer polls `GET /investigations/<id>?view=voice` and speaks `spoken_summary`.

## 2. Test-data scenarios (no database access needed)

```bash
.venv/bin/python -m nemotron_investigator health

# A. Unknown USB while armed (modeled on the team's iPhone USB test)
.venv/bin/python -m nemotron_investigator run --incident incident:fixture-a-unknown-usb --out var/demo/A.json
# B. Unrecognized person at the desk, then a verified USB connection (official severity high)
.venv/bin/python -m nemotron_investigator run --incident incident:fixture-b-person-then-usb --out var/demo/B.json
# C. Suspicious app activity with incomplete evidence (camera offline, linked event missing)
.venv/bin/python -m nemotron_investigator run --incident incident:fixture-c-app-incomplete --out var/demo/C.json
# Prompt injection in a USB descriptor (attacker-controlled text)
.venv/bin/python -m nemotron_investigator run --incident incident:fixture-n3-prompt-injection --out var/demo/N3.json
.venv/bin/python bench/render_trace.py var/demo/B.json > var/demo/B.md

# Optional: Nemotron's reasoning between tool calls (slower: 28.2 s for B, bench/results/bench_20261108T073811Z.md)
.venv/bin/python -m nemotron_investigator run --think --incident incident:fixture-b-person-then-usb --out var/demo/B_think.json

# NVIDIA NeMo Relay policy gate: a real blocked and a real allowed request
.venv/bin/python bench/relay_gate_demo.py --live
NI_RELAY_GATE=1 .venv/bin/python -m nemotron_investigator run --incident incident:fixture-b-person-then-usb --out var/demo/B_relay.json
```

The same scenarios work through the API with `"evidence_mode": "fixture"`. The voice text then opens with "Test data only, not the live LOCKI database."

## What to point at

1. **The model's input** is only `Investigate incident <id>.` plus the tool schemas. Nemotron fetched every piece of evidence itself.
2. **Each step:** inference latency and token counts, the tool Nemotron chose, its arguments (including window sizes it picked), the real tool result, and the decision to continue or stop.
3. **Evidence mode** appears on every result:
   - `evidence_badge` LIVE / TEST DATA;
   - the banner;
   - the `X-Evidence-Mode` header;
   - `live_data` on every tool call.
4. **Severity.** The official severity from LOCKI's rule always comes first and is never lowered. Nemotron's risk is reported separately as its own view, and `effective_severity` = max(official, Nemotron).
5. **Validation.** Bad calls are canonicalized or rejected and logged, never executed blindly. Examples: a dropped `incident:` prefix, an out-of-range window, a tool that doesn't exist.
6. **The assessment.**
   - Observed facts each cite retrieved evidence ids.
   - Explanations are labeled as inferred.
   - Missing information is listed separately.
   - Claims the grounding step rejected are shown, not hidden.
7. **Safety.** The agent has no lock, disarm or write tool, and every recommended action requires human approval.
8. **Explicit stop.** Nemotron ends with `finish_investigation` and gives a reason, which shows up in the trace.
9. **NeMo Relay.** Nemotron's own over-broad USB request is blocked by Relay before execution, and its in-policy request is allowed.
10. **Context.** `context_verification` shows prompts above the runner's nominal 4096 were verified to be retained in full (`bench/results/context_probe_*.json`).
11. **Benchmarks.** `bench/results/bench_*.md` has every run, with real numbers.
