# LOCKI AIRLOCK: Nemotron security investigator

This is a local, multi-step security investigation agent. NVIDIA **Nemotron** (`nemotron-3.5-bolt:e4b`, served by Ollama on the M5) decides which approved read-only evidence tools to call, in what order and with what arguments, and when to stop. Every proposal is validated, permission-checked, deadline-bound and traced before it runs.

The model then drafts a structured assessment under constrained JSON decoding. Deterministic code checks that every "observed" fact cites evidence that was actually retrieved. It also demotes overclaims, rebuilds the timeline only from retrieved evidence, and reports gaps honestly.

This is not a summarizer over a pre-assembled incident. The model starts with only an incident ID and has to fetch the evidence itself.

```
request(incident_id) ─► preflight authz (deterministic) ─► Nemotron tool-selection turns ─► finalize (constrained JSON)
                                                          │   ▲                                   │
                                                          ▼   │ tool results (untrusted data)     ▼
                                           validated, scoped, read-only tools          grounding + advisories
                                           (fixture adapter | live SpacetimeDB views)  timeline, metrics, trace
```

## Quick start (M5)

```bash
cd services/nemotron-investigator
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m nemotron_investigator health                  # Ollama + model status
.venv/bin/python -m nemotron_investigator run --incident incident:fixture-a-unknown-usb --out var/runs/A.json
.venv/bin/python -m nemotron_investigator run --live --incident incident:<32 hex> --out var/live/run.json
.venv/bin/python -m pytest -q                                     # deterministic tests (no network)
.venv/bin/python -m pytest -q -m live                             # real-model tests (uses local Ollama)
.venv/bin/python bench/run_bench.py --trials 5                    # real-model benchmark
.venv/bin/python bench/context_probe.py                           # verify what the runner retains from long prompts
.venv/bin/python bench/stdb_probe.py                              # read-only live SpacetimeDB auth probe (redacted copy)
.venv/bin/python -m nemotron_investigator create-key --requester dhruv-m4-backend --workstation '*' --out .secrets/handoff/dhruv.key
scripts/serve.sh --daemon                                         # API on 127.0.0.1:8787 (live default)
.venv/bin/python bench/live_e2e.py --key-file .secrets/handoff/mustafa-operator.key   # live end-to-end
```

## Evidence modes (never mixed, never substituted)

| mode | badge | incident ids | source |
|---|---|---|---|
| `live` (`NI_EVIDENCE_BACKEND=spacetimedb`, the default of `scripts/serve.sh`) | **LIVE** | `incident:<32 hex>` only | Sid's owner-granted evidence views `my_investigator_incidents`, `my_investigator_events` and `my_investigator_baselines`, read with the investigator's own non-owner identity. The workstation owner grants one incident at a time with `grant_incident_evidence`. The identity holds no membership and never calls a reducer. `NI_STDB_VIEWS=membership` reads the older membership-scoped `my_*` views. |
| `fixture` (`NI_EVIDENCE_BACKEND=fixture`, or `evidence_mode: "fixture"` with `NI_FIXTURE_ENABLED=1`) | **TEST DATA** | `incident:fixture-<slug>` only | `fixtures/scenarios/`: synthetic datasets in the exact live row shapes, with every row id carrying `fx-`. Scenario A is modeled on the team's iPhone USB test but reuses no live id or row. |

Every result carries `evidence_mode`, `evidence_badge` and `evidence_banner`, and every tool call carries `live_data`. Every response about an investigation (POST 202 and GET) carries the `X-Evidence-Mode` header; `/health` lists the evidence modes instead. A live-format id is refused on fixtures, and a fixture id never queries the live database. See INTEGRATION.md §3.

## Severity

`official_severity` is the incident record's severity from LOCKI's deterministic rule, verbatim and never modified. `risk_assessment` is Nemotron's own view. `effective_severity` = max(official, Nemotron), so the model can raise the review priority but never lower it. The headline and voice text always lead with the official severity. See INTEGRATION.md §5.

## Security model (enforced in code, not in the prompt)

- **Allowlist only.** There are 7 read-only tools. Any other tool name, such as `disarm_session` or `run_shell`, is rejected and logged, and the backend is never touched.
- **Strict arguments.** pydantic models forbid extra fields, bound every integer, and require incident IDs in the evidence mode's id space. A bare 32-hex or `fixture-<slug>` ID gets its `incident:` prefix back, and that is logged as `arguments_normalized`.
- **Scoped.** Every tool works only on the one incident authorized in preflight. Naming another incident returns `permission_denied` before any read happens.
- **Fixed queries.** The SpacetimeDB adapter uses fixed SQL templates. The only interpolated values are IDs that pass a strict character-set check, plus integers our own code computes. Model text never reaches SQL.
- **Bounded.** There are limits on per-step model time, per-tool time, total investigation time, tool-call count, calls per turn, model turns, result size and finalize tokens. A streaming guard aborts degenerate constrained-JSON output.
- **Untrusted data.** USB descriptors, app names, summaries and requester notes are cleaned, length-bounded and scanned for instruction-like text. Matches are reported in `untrusted_content_flags` and in advisories. Even text that fools the model cannot grant a capability, because none exists.
- **No actions.** The agent cannot lock, disarm, approve or write anything. Every recommended action carries `requires_human_approval: true` and `executed_by_agent: false`.
- **Honest failure.** Failed, rejected and timed-out investigations carry no assessment. Evidence failures show up as `degraded`, with `evidence_gaps` and `[system]` notes added to `missing_information`.
- **API.** Every endpoint requires a Bearer key; only SHA-256 hashes are stored, in a 0600 file. Requesters are isolated from each other, the service binds to loopback only, and there are no docs or OpenAPI endpoints.

## Optional: NVIDIA NeMo Relay policy gate (tested)

```bash
uv pip install --python .venv/bin/python --require-hashes -r requirements-relay.txt   # nemo-relay==0.11.1
NI_RELAY_GATE=1 .venv/bin/python -m nemotron_investigator run ...                     # or set for `serve`
.venv/bin/python bench/relay_gate_demo.py --live                                      # real blocked + allowed
```

- **What it does.** It registers a conditional-execution guardrail (`locki.usb_lookback_policy`) and runs the approved tool `get_recent_usb_events` through `nemo_relay.tools.execute`. Relay decides before the tool body runs.
- **The policy is deterministic data-minimization:** at most 600 s of USB lookback without human approval. That is stricter than the schema bound, so blocks come from Relay itself.
- **Verified on the M5:**
  - Nemotron's own `window_s=1800` request was blocked, with 0 backend reads.
  - Its `window_s=300` request was allowed.
  - The process opened no network sockets.
  - Only the in-process SDK is used: no plugin host, CLI, gateway or exporter.
- **If the package is missing:** with `NI_RELAY_GATE=1` set, the investigation fails closed (`policy_gate_unavailable`).

## Files

- `src/nemotron_investigator/agent.py`: bounded loop, preflight, finalize, statuses and metrics
- `tools.py`: tool registry, validation, scoping, execution and result shaping
- `assessment.py`: constrained-decoding schema, grounding and overclaim rules
- `evidence/fixture.py`: labeled fixture adapter
- `evidence/spacetime_http.py`: live read-only adapter
- `contracts.py`: row shapes from the live schema
- `api.py`, `auth.py`, `store.py`: integration API (see `INTEGRATION.md`)
- `relay_gate.py`: optional NeMo Relay policy gate
- `bench/`: benchmark harness, scenario expectations, live SpacetimeDB probe and results
- `CONTEXT.md`: running status, decisions, measured results and change log
