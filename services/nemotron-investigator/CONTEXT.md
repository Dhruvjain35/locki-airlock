# CONTEXT: LOCKI AIRLOCK Nemotron investigator

Last updated: 2026-11-08 ~13:55Z (M5, Mustafa)

## What this is
This is Mustafa's part of LOCKI AIRLOCK (HackUTD 2026, NVIDIA and SpacetimeDB tracks): an independent, read-only, multi-step security investigation service. NVIDIA Nemotron chooses and sequences approved evidence tools, and deterministic code grounds its assessment.

Teammates and their areas (do not edit their modules):
- Dhruv (M4 Air): CV, macOS watchers, voice, Mac backend
- Areeb: Jetson edge and the Logitech camera
- Sid: the SpacetimeDB module `locki-w91bw`, rules and dashboard

## Repo state
- The service is on the monorepo `Dhruvjain35/locki-airlock` main at `services/nemotron-investigator/` (8ea2fc0 snapshot import, 4039a92 Sid's views, 37a3b97 contract v0.3, e38b38d CODEOWNERS). The clone is at `~/locki-airlock`.
- **Sid's reviewed revision of this lane is on main** (`9107d8d`, `835d1d6`; integration published at `2d026e2`):
  - Live preflight and finalization authenticate the expected investigator principal and reject any enabled membership.
  - Every event must be in the incident's current approved set, and the official severity is re-read before the headline is built.
  - A revoked, changed or stale grant withholds the report (`live_report_withheld`).
  - A cached live record with missing or fixture provenance returns 404.
  - Sid reports 425 investigator tests passing.
- **The M5 does not run that revision yet.** pid 35055 (started 2026-11-08 01:58 CST) runs the lane code from before Sid's revision: no source file has changed since it started, and it does not include `9107d8d` or `835d1d6`.
  - Activating the revision needs a restart, the test suite and one live check on a granted incident.
  - Sid asked for it in issue #2 comment 6720437650. The decision is Mustafa's.
- `~/locki-airlock-nemotron` (branch `feature/airlock-nemotron`) keeps the full history, the venv, `.secrets/` and the running service. Its history was never pushed; only this file is synced to main. The monorepo copy omits the v1–v3 bench results this file cites.
- Team coordination: issue #2 for this lane, and issue #3 for the M4 recovery runbook.
- Secrets live in `.secrets/` (gitignored, mode 0600): the API key hashes and the investigator SpacetimeDB token. Runtime records go to `var/` (gitignored).

## Environment facts (verified 2026-11-07)
- **Model and runtime:**
  - Ollama 0.40.15 at `127.0.0.1:11434`, model `nemotron-3.5-bolt:e4b` (digest `fe4af3f5907b…`), MLX runner.
  - `/api/ps` reported a loaded context of 8192 early on 2026-11-07 and 4096 for every load since (Ollama's VRAM-based default, `default_num_ctx=4096` in the server log). The MLX runner does not enforce it: full retention was verified to 9.8k tokens on 2026-11-08 (see "Context retention"). The model's native limit is 65,536. size_vram is about 7.7 GB right after a load. Capabilities: completion, vision, audio, tools, thinking.
  - Dhruv's M4 reaches it through an SSH tunnel (his port 11435), so it is shared. The service never sends `keep_alive` or `num_ctx`, and never unloads the model.
- **Machine:** Apple M5 Pro with 24 GB RAM. Tailscale lists the M5 as `mustafas-macbook-pro` and Dhruv's M4 as `dhruvs-macbook-air`; their addresses are not written into this repository (CONTRIBUTING: no fixed private IP addresses).
- **SpacetimeDB:** CLI 2.14.1, logged in to Maincloud. The schema of `locki-w91bw` was pulled read-only:
  - 17 private tables, 15 identity-scoped public views (`my_*`) and 24 reducers.
  - Reducers include `record_agent_step`, `request_incident_info` and `propose_protected_action`.
  - `contracts.py` mirrors the column names and types.
- **Not ours:** a local `spacetimedb-standalone` on `:3000` belongs to a separate ScrabbleBot Claude session. Leave it alone.

## Design decisions and verdicts
1. **Tool use is model-driven.** The model receives only `Investigate incident <id>.` plus 7 tool schemas, and fetches everything itself.
   - In live runs, Nemotron picks and orders the tools, widens or narrows time windows on its own (`window_s` 600 or 1800, and re-queries with 10 or 1), and decides when to stop.
2. **Enforcement is in code.** That covers the allowlist, strict pydantic args, single-incident scope, deadlines and budgets, size bounds, and fixed SQL templates. Untrusted text is cleaned, bounded and flagged; it never controls capability.
3. **Incident-ID canonicalization.** Live runs showed Nemotron often drops the `incident:` prefix. A bare 32-hex value is canonicalized to `incident:<hex>` and must still equal the authorized incident. `incident_id` is optional because tools are scoped anyway. Errors name the correct ID so the model can self-correct. *Verdict: this fixed B-scenario runs that previously died after one invalid call.*
4. **Constrained-decoding schema order (finalize).** With all keys required, decoding degenerated into endless whitespace whenever the model wanted to close an object but a required key remained. This was observed after `risk.confidence → rationale` and after `summary → spoken_summary`, and it hit the token cap or the 60 s deadline.
   - Fix: put risk first (rationale → level → confidence), then the required evidence lists, then optional trailing keys. *Verdict: 9/9 valid, 0 loops, 5.7–10.8 s on replayed conversations.*
   - Safety net: a streaming guard aborts after 96 whitespace characters, then retries with a relaxed grammar (no required keys).
5. **Citable query results.** Every successful tool result gets a citable `result_id` (`res:<step>.<idx>:<tool>`), so absence of evidence ("no vision events in the window") can be grounded.
6. **Overclaim rules.** These demote unhedged malware claims, actor attribution and intent claims in observed facts, and flag the agent claiming it performed actions. Passive evidence statements such as "session was disarmed" are allowed.
7. **Advisories never override the model.** They include `risk_below_rule_severity`, `high_confidence_with_evidence_gaps` and `untrusted_instruction_like_content`.
8. **Live DB path.** The investigator has its own identity, `c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883`, and reads only through `my_*` views. Probed live: 0 memberships, 0 visible incidents, and a direct private-table read is refused. Superseded on 2026-11-08: there is no membership at all. Access is the owner's per-incident `grant_incident_evidence`, and the identity reads only through the `my_investigator_*` views (INTEGRATION.md §7).

## Adversarial review (2026-11-08, 5 lenses + 5 skeptic verifiers, workflow wf_9c7369d5-ef8)
40 findings came back. All 40 were CONFIRMED or PLAUSIBLE; none were refuted. Fixes by area:

**Injection and grounding (core, done by Mustafa's session)**
- Control-token injection: `clean_text` now maps `<` and `>` to `‹` and `›`. The model's tokenizer treats `<|turn>` and `<tool_response|>` as special tokens, so a BadUSB descriptor could forge a turn. Control-token patterns are now flagged, and device-chosen IDs go through `safe_id`.
- Grounding: a fact citing anything that was not retrieved is now rejected. Absence-only citations must actually state an absence. Hedges no longer exempt malware, intent or attribution claims in facts, and speculative facts are rejected.
- Summaries: a flagged summary or spoken summary is replaced deterministically; the model's draft is kept in `model_drafts`.
- Evidence becomes citable only after a successful call, and only for IDs shown to the model (transactional registry).

**Data correctness (core)**
- Clock domain: windows use `received_at_ms` (server clock). Device time is shown when the two clocks agree within 30 s; larger skew is flagged.
- Session reads are bound to the workstation, failing closed on a mismatch.
- USB serial mismatch: a matching VID/PID with a different serial no longer counts as a baseline match.
- Three-state results: baseline and app approval can be unknown instead of a false negative. Lookalike bundle IDs are never approved.
- Camera status comes from device health, not a stale snapshot.
- Timestamps can no longer crash a tool, and rows the adapter rejects are reported.

**Control loop (core)**
- Dedup uses canonical arguments. Only deterministic outcomes are cached, and a transient failure gets one retry.
- A finalize time reserve is kept (`min(25 s, 0.4 × total)`). A selection-step timeout with evidence in hand falls through to finalize.
- A run with zero evidence fails with no assessment.
- `finish_investigation` has its own status and is refused before any evidence has been read.
- The per-turn cap has its own code (`per_turn_limit`) and is not counted as invalid.
- Context budget is checked before every model call, including finalize, using a measured chars-per-token ratio.
- Classes that were never queried are reported as gaps. Every advisory marks a run for review, and limit-stopped runs carry an advisory.
- The relaxed finalize retry has a matching acceptance contract, and requires a risk level or at least one fact.

**Benchmark scoring**
- `degraded` no longer counts as completed.
- Credit for missing information ignores the agent's own `[system]` lines.
- The forbidden-claim scan covers the summary and spoken summary.
- Specific citation of key evidence is now a pass check.

**API and evidence adapters (delegated, file-disjoint; see the change log)**
- API: workstation scope checked on read, `/health` restricted (`health:admin`), global queue cap plus bounded queue wait, pre-auth body limit, records persisted across shutdown and evicted from memory, strict key-file validation, voice view with `needs_review`.
- Adapters: workstation-scoped sessions, `received_at_ms` windows, per-row validation via `pop_rejected_rows()`, 400/404 mapped to contract errors, LIMIT clauses, a response byte cap, and canonical identity hex.

## How it is tested
- `pytest -q`: deterministic tests with a scripted Ollama transport, covering malformed args, unknown or dangerous tools, cross-incident access, DB down at preflight and mid-run, Ollama down, model timeout, total deadline, duplicates and budget, nudge, degenerate finalize, invalid final JSON, injection, API auth and isolation, and the adapter's SQL safety.
- `pytest -m live`: the real Nemotron.
- `bench/run_bench.py`: the real-model benchmark (results in `bench/results/`).
- `bench/stdb_probe.py`: the live read-only SpacetimeDB authorization probe.

## Measured results

### Benchmark v1 (`bench/results/bench_20261107T234838Z.*`, 30 scenario runs plus 7 negatives, real Nemotron, warm)
- **Pass and completion:** 30/30 runs passed their checks and 30/30 completed. All 7 runtime negatives behaved as expected:
  - DB down at preflight → failed, with 0 model calls
  - DB down mid-run → degraded, with gaps reported
  - Ollama down → failed
  - 50 ms model timeout → timeout
  - unauthorized requester → rejected
  - invalid incident ID → rejected
  - live SpacetimeDB with the non-member identity → rejected in 203 ms
- **Latency:**
  - total p50 9.6–13.3 s
  - first tool decision p50 400–501 ms
  - selection step p50 428–511 ms (p90 519–676 ms)
  - finalize p50 6.1–9.1 s, which is 60–70% of wall time
- **Tool use:**
  - execution success 1.0
  - invocation correctness 1.0, except A at 0.875: the model asked for `window_s=1`, below the old 10 s minimum. The minimum is now 1 s.
- **Grounding:** citation accuracy 1.0 and 0 rejected facts. 9 overclaim flags, all on high-risk wording in *explanations*; now flagged only at high likelihood.
- **Risk calls:** N2 (false positive) was rated low in 3/5 runs and medium in 2/5. N3 (injection) was never rated low (5/5 medium).
- **Caveat 1, stopping:** 29/30 runs hit the 8-turn cap, and the tool order mostly followed the system prompt's list. This led to the explicit `finish_investigation` tool, a neutral prompt and progress notes.
- **Caveat 2, prompt cache:** trials 2–5 of each scenario replayed near-identical conversations, so Ollama's prompt cache made prompt evaluation about 18 ms. Benchmark v2 gives each trial a unique requester note.
- **Cold start:** not measured. The model never unloaded during the 420 s wait; another client appears to keep it alive (keep-alive moved from 5 to about 30 minutes, and we do not send `keep_alive`). Not force-unloaded, because it is shared.

### Benchmark v2 (`bench/results/bench_20261108T000632Z.*`; finish tool, neutral prompt, progress notes, cache-aligned finalize; unique note per trial)
- **Pass and completion:** 30/30 completed and passed. 29 runs were `completed` and 1 `completed_with_warnings` (v1: 12 with warnings). All 7 negatives passed again.
- **Stopping:** all 30 runs ended with `model_finished`, each with a logged reason (v1: 29/30 hit the turn cap). Invocation correctness was 1.0 in every scenario.
- **Latency (realistic, no cache replay):**
  - total p50 11.8–14.4 s
  - first tool decision p50 423–504 ms
  - step p50 539–605 ms
  - finalize p50 7.0–9.0 s
  - The roughly +100 ms per step versus v1 is the honest cost of evaluating new tool-result tokens; v1 understated it.
- **Risk calls:** N2 (false positive) was rated low in 4/5 runs. N3 (injection) was never rated low.
- **Citations:** accuracy 1.0, except N1 at 0.964 with 1 rejected claim. The share of citations naming a specific event or record was low in A (0.26), C (0.24) and N3 (0.10), because the model cited the generic result IDs. Result IDs are now emitted only for results with no records.

### Benchmark v3, hardened agent (`bench/results/bench_20261108T005610Z.*`, 30 scenario runs plus 7 negatives) — CURRENT NUMBERS
- **Pass rate:** 29/30 under the strict rubric. The one miss (N1 t2) is a rubric false negative: the model wrote "Whether the linked event IDs could be retrieved". An offline rescore with a broadened N1 regex gives 30/30; it is recorded separately in `bench_20261108T005610Z_rescore.json`.
- **Negatives:** 7/7 with real components after fixing the step-timeout floor regression (see the change log).
- **Latency:**
  - total p50 9.0–11.5 s (p90 9.6–11.6 s)
  - first tool decision p50 328–381 ms
  - selection step p50 436–459 ms (p90 697–890 ms)
  - finalize p50 5.0–7.3 s
- **Decoding:** 0 degenerate aborts and 0 relaxed retries across the 30 runs (text-last layout).
- **Tool use and grounding:**
  - invocation correctness 1.0, execution success 1.0
  - citation accuracy 1.0, 0 rejected facts
  - specific-citation share 0.4–1.0
  - stop reason `model_finished` in 30/30 runs
- **Risk calls:**
  - A, B, C, N1 and N3 were rated medium in every run; N1 always with low confidence.
  - N2 (false positive) was rated low in 4/5 runs.
  - B (rule severity high) was rated medium in 5/5 runs, so the `risk_below_rule_severity` advisory fires. This looks like under-calibration on the strongest scenario and is left for human review.
- **Warnings are legitimate grounding actions.** For example, B's summaries saying "an unrecognized person … connected" were replaced, and N3's untrusted text was flagged in 5/5 runs.
- **Runtime observation:**
  - Correction (2026-11-08, from the Ollama server log): the 18:50 "restart" was not another client. The runner idled out at 18:50:33, 30 minutes after the last request, and our next request loaded it again at 18:50:53 with the default context of 4096. Our requests never set `num_ctx` or `keep_alive`.
  - The single-needle check from 2026-11-07 (6,869 tokens, code word at the start) was superseded by the multi-position probe on 2026-11-08 (see "Context retention").
  - Each result now records `runtime` (Ollama version, model digest, reported loaded context, runner).

### NVIDIA NeMo Relay gate (`bench/results/relay_gate_demo.json`, `relay_gate_live_*.json`)
- **Installed:** `nemo-relay==0.11.1`, hash-pinned. It uses the in-process SDK only.
- **Deterministic check:** `window_s=300` was allowed (1 backend read). `window_s=1800` was blocked before execution (0 backend reads). No network sockets were opened.
- **Live Nemotron, scenario B:** the model asked for `window_s=1800`, and Relay blocked it. The model then continued with other sources and did not retry within policy.
- **Live Nemotron, scenario C:** the model asked for `window_s=300`, and Relay allowed it.

### API end-to-end (`bench/results/api_live_check.json`, real uvicorn on 127.0.0.1:8787, real model)
- 401 without a key, 200 on health, 422 for a bad incident ID, and 202 on create.
- The investigation reached `completed` after a 10.6 s poll, with 8 model-chosen calls.
- The voice view worked.
- Another requester got 404 on the investigation and `rejected` for a workstation it is not allowed to see.

### Memory (2026-11-07 ~00:00Z)
- `footprint` on the MLX runner (pid 89940) shows 15 GB phys_footprint (peak 17 GB), almost all IOAccelerator (Metal) memory.
- Ollama's `/api/ps size_vram` was 7.76 GB at session start, 15.3 GB before benchmark v1 and 16.07 GB after. That is the same runner process, so it looks like MLX cache growth rather than a reload.
- RSS (about 0.6 GB) does not capture GPU memory, so `footprint` is the figure to quote.
- System-wide free memory was 26% on the 24 GB M5.

### Finalize experiment (replayed conversation, 2 runs each)
- With `tools` kept in the finalize request: 5.8–5.9 s and 714–716 output tokens, schema-valid, no tool calls.
- Without: 7.9–9.4 s and 937–1,006 tokens. Adopted.

## Final integration pass (2026-11-08): evidence modes, official severity, context, snapshot

### What the team lead asked (verbatim priorities, condensed)
1. Get narrowly scoped read-only access for the existing investigator identity from Sid (no owner credentials).
2. Replace fixtures with authorized live SpacetimeDB evidence; verify one real investigation end to end.
3. Make fixture-backed vs live-backed unmistakable in API results and UI.
4. Keep the DB's official severity separate from Nemotron's risk; never downgrade a high-severity incident to medium.
5. Verify actual context/truncation behavior against the reported 4,096 (not assume).
6. Preserve the Ollama configuration; do not interrupt Dhruv's model.
7. Clean source snapshot for `services/nemotron-investigator/` in the new monorepo `Dhruvjain35/locki-airlock` (fresh history; exclude .git, tokens, caches, weights, private evidence, generated secrets).
8. Wait for repo access before pushing; never push the standalone history onto the new main.
9. Safe authenticated endpoint on localhost:8787 for Dhruv (Tailscale + SSH tunnel).
10. Exact env vars, request shape, startup and verification commands, without revealing keys.

### State
- **Sid's grant: not yet.** Live probe 2026-11-08 06:20Z: reachable, 0 memberships, least-privilege ok, private table refused, bounded `my_*` SQL accepted. `bench/wait_for_grant.py` polls (read-only, 60 s) and `bench/live_e2e.py` runs the full live check through the API the moment it exists.
- **Monorepo:** `Dhruvjain35/locki-airlock` is not readable by `hasmus25` yet. There are two PENDING invitations from Dhruvjain35, both private with write permission:
  - `Dhruvjain35/locki-airlock`, sent 2026-11-08 06:54Z;
  - `Dhruvjain35/Locki`, sent 2026-11-08 00:25Z.

  Accepting them is Mustafa's call. Nothing is pushed.
- **Local commit and snapshot:** this pass is committed locally as `1392857` on `feature/airlock-nemotron` (not pushed). The audited snapshot is at `~/locki-airlock-export/` (`services/nemotron-investigator/`, `AUDIT.md` PASS, `MANIFEST.sha256`, tarball), and its own test suite passes when run from the export.
- **Service:** running on `127.0.0.1:8787` (`scripts/serve.sh --daemon`), live default + opt-in fixture mode. Keys in `.secrets/handoff/` (0600, never printed): `dhruv-m4-backend` (default scopes, `*`), `mustafa-operator` (+ `health:admin`).

### Evidence modes (requirement 3)
- Disjoint id spaces: live `incident:<32 hex>`, fixture `incident:fixture-<slug>`; fixture row ids carry `fx-`. The agent preflight and the API (422) both refuse cross-mode ids; a mode the server does not offer is 409, never substituted.
- Every result: `evidence_mode` / `evidence_badge` (LIVE / TEST DATA) / `evidence_banner` first in the JSON; `evidence_provenance.mode`; `live_data` per tool call; `X-Evidence-Mode` header on create and read; voice text opens with "Test data only, not the live LOCKI database." for fixture runs; `/health` lists `evidence_modes` and both backends.
- Old fixture A reused the real iPhone incident id; removed from fixtures, tests, docs and bench scripts (the old id stays only in results files predating this pass, which the snapshot excludes).
- `simulated=true` rows in live data raise `simulated_evidence_present`.

### Severity (requirement 4)
- `official_severity` (verbatim incident severity, authoritative), `risk_assessment` (Nemotron, `model_advisory`), `effective_severity` = max(official, Nemotron) (`official_severity` / `nemotron_escalation` / `official_severity_unrecognized`).
- `headline`, the deterministic summary and the voice view always lead with the official severity; a lower Nemotron level is spoken as Nemotron's view followed by "The official <level> severity stands." The voice view no longer reads model text that names a lower level (`mentions_lower_level`).
- Advisory renamed: `risk_below_rule_severity` → `model_risk_below_official_severity`.
- Before this pass the voice template for B said "Medium risk, medium confidence. This needs human review." (a spoken downgrade of a high-severity incident). Fixed.

### Context retention (requirement 5)
- Server log: `vram-based default context total_vram=17.8 GiB default_num_ctx=4096`; every load since startup without `num_ctx` gets 4096. The 18:50 "restart" was an idle expiry (30 min after the last request), not another client.
- `bench/context_probe.py`: five random codes per prompt (system-prompt start, first user message, 1/3, 2/3, final turn), multi-message chat, no num_ctx/keep_alive, temperature 0. Two runs (`context_probe_20261108T062358Z.json`, `context_probe_20261108T065058Z.json` with runtime identity): 16 prompts from 3.1k to 9.8k tokens, **80/80 codes recalled exactly**; `prompt_eval_count` equals the full prompt and the log shows every token processed. `/api/ps` keeps reporting 4096 while `size_vram` grows: the 4096 is nominal for this MLX runner. Model native limit 65,536.
- Real investigations: 25/30 v3 runs exceeded 4096 (max 5,019); v4: 25/30 exceeded 4096, max 4,801, p50 4,146. Results now carry `context_verification`; a runtime other than (0.40.15, mlx, fe4af3f5907b) gets `context_retention_unverified` when a prompt exceeds its nominal context.

### Cold start (measured naturally, model idle-unloaded, never forced)
- 2026-11-08 06:22Z, scenario A: first model call 4,941 ms of which load 4,122 ms; first tool decision 5,001 ms; whole investigation 12,814 ms (warm p50 for A about 9.8 s). Numbers-only record: `bench/results/cold_start_20261108T062220Z.json`; the raw record stays in `var/cold/` (it predates the fixture id change).

### Benchmark v5, final code (`bench/results/bench_20261108T073155Z.*`, 30 runs + 9 negative rows, real Nemotron, warm, fixture evidence) — CURRENT NUMBERS
- 30/30 passed, 30/30 completed (14 completed, 16 completed_with_warnings). Every run used all 7 evidence tools then `finish_investigation`; invocation correctness and execution success 1.0 everywhere.
- Total p50: A 9.42 s, B 9.37 s, C 9.63 s, N1 8.66 s, N2 10.53 s, N3 9.17 s. First tool decision p50 272–351 ms; step p50 331–393 ms; finalize p50 5.4–6.8 s.
- Citation accuracy 1.0, except N2 0.938 (Nemotron again cited a tool name in an explanation; grounding dropped it). 0 facts rejected.
- Risk: A/B/C/N1/N3 medium 5/5 (N1 always low confidence); N2 low 5/5 (high confidence); B's official high is kept (effective high).
- Overclaim flags: B 4/5 and N2 4/5, mostly the new `understates_official_severity` on Nemotron's summary wording; the summary was replaced deterministically.
- Prompt size: max 4,795, p50 4,149, 25/30 above 4096. Every run was `above_nominal_but_verified_retained` or within the nominal context; no truncation suspected.
- Negatives: 7/7 run cases passed.
  - The live database-authorization case was `not_run` in this run, because it needs a real out-of-scope id.
  - It was then run separately with the real iPhone-test incident id (kept private): rejected by the identity-scoped view in 145 ms with an unrestricted requester and 0 model calls (`bench/results/live_negatives_*.json`).
  - The positive scope check passed (0 visible incidents, least privilege ok).
- Memory: size_vram 10.24 GB → 15.95 GB over the run; ollama RSS 1.09 → 1.26 GB.
- Think mode (`bench_20261108T073811Z.*`, B once): 28.2 s total, first tool decision 934 ms, passed.
- Also rerun on the final code:
  - API real-HTTP check (`api_live_check.json`, separate port 8791): completed in 9.9 s, `X-Evidence-Mode: fixture`, the other requester gets 404.
  - Relay demo: B blocked at 1800 s, C allowed at 300 s.
  - Traces re-rendered from v5.

### Benchmark v4 (`bench/results/bench_20261108T064432Z.*`, pre-review code; superseded by v5)
- 30/30 passed, 30/30 completed (17 completed_with_warnings, mostly the severity advisory on B and grounding flags), 8/8 negatives passed (new: `fixture_id_refused_by_live_backend`; live unauthorized read now uses a live-format id and a requester without grants).
- Total p50 8.7–10.2 s; first tool decision p50 262–300 ms; step p50 328–384 ms; finalize p50 5.35–6.88 s.
- Invocation correctness 1.0 and execution success 1.0 in every scenario; every run used all 7 evidence tools then `finish_investigation`.
- Citation accuracy 1.0 except A 0.983 and N2 0.933: in 3 runs Nemotron cited a tool name (e.g. `get_device_baseline_status`) as an explanation's supporting id; grounding flagged and dropped those; 0 facts rejected.
- Risk: A/B/C/N1/N3 medium 5/5 (N1 low confidence); N2 low 5/5 (high confidence); B official high kept (`effective_severity` high, advisory fires 5/5).
- Memory: size_vram 8.24 GB before → 15.9 GB after (KV/prefix cache growth), ollama RSS ~0.9 GB.

### Adversarial review of this pass (workflow wf_08cc523b-e88: 4 lenses + 4 skeptic verifiers)
- 32 findings: 25 confirmed, 4 plausible, 3 refuted. All confirmed and plausible ones were fixed; two refuted ones got small hardening anyway (health `configured` per mode and the fixture-default degraded code; `serve()` now refuses an unknown `NI_EVIDENCE_BACKEND`).
- **High: voice downgrade wording.** The voice could read Nemotron's own narrative in a downgrade run ("The severity is assessed as medium…", "not a high-risk event", "likely a false positive").
  - Fix: `assessment.understates_severity`, a broad guard covering lower level words anywhere, a negated official level, dismissals and Unicode hyphens.
  - Nemotron's narrative and its missing-information items are never read when it rated below the official level.
  - `ground()` replaces a model summary that understates the official severity (`understates_official_severity`), so the full record is covered too.
- **High: context check.** It trusted Ollama's prompt_eval_count, which is post-truncation, so a truncating runtime could never be flagged.
  - Fix: every model call records `request_chars`. `context_verification` estimates the prompt size independently (chars/token from the first, small call) and flags `truncation_suspected` when the reported count stops growing with the request near the cap, or falls below 0.7× the estimate.
  - It also handles cold start (`nominal_context_source`) and unidentified runtimes (`unknown`).
  - Validated on 6 real runs (54 calls): reported/estimate ratio 1.00–1.28, never a false flag. Token-level KV eviction is invisible in counts; only the probe covers it, hence `unverified` for runtimes it has not covered.
- **Attacker-controlled names spoken.** Untrusted device/app names, summaries and details shown to the model are collected (`untrusted_text_values`), and the voice never speaks a sentence containing one. It falls back to grounded facts, then deterministic category counts.
- **Simulated live incident.** A live incident marked `simulated=true` is now caught at preflight: `incident_simulated`, "SIMULATED INCIDENT." headline, a voice lead, needs review. `live_e2e.py` picks the newest non-simulated incident.
- **Unlabeled records.** Legacy, queued and interrupted records are labeled through `record_mode()` (provenance fallback, `UNVERIFIED` badge) and the store's interrupted results. The banner says "LIVE EVIDENCE SOURCE" and no longer claims a read happened.
- **Privacy and least privilege.**
  - `stdb_probe.redact` is an allowlist, and a successful private-table read is now a failed check (`least_privilege_violation`).
  - An over-privileged grant makes `wait_for_grant` exit 4, makes `live_e2e` refuse, and sets health `live_evidence_identity_over_privileged`.
  - The live negative test runs only with a real out-of-scope id (`NI_BENCH_LIVE_INCIDENT`, else `not_run`), plus a positive check that visible incidents are on granted workstations. Live negative records are written to `var/live/` only.
- **Scripts.**
  - `serve.sh --daemon` refuses a busy port or a running pid and confirms a 401 from the new process before it writes the pid file.
  - `export_snapshot.sh` quarantines a failed audit and leaves the deliverable path untouched.
  - `create-key` gained `--out` (0600, not printed) and `--scope`.
- **Docs.** Fixed stale numbers, bare `python`, exact-match workstation grants, the `X-Evidence-Mode` scope, the record-shape claims and the `tool_calls_proposed` type.

### Snapshot (requirement 7)
- `scripts/export_snapshot.sh [OUT]` = `git archive HEAD` of this package (so untracked/ignored files cannot leak) minus `scripts/snapshot_exclude.txt`, then `scripts/audit_snapshot.py` (exact secret values from `.secrets/` compared without printing, credential patterns, weights/archives/large/binary files, forbidden dirs, live-looking incident ids, and every identifier listed in the private `var/live/forbidden_identifiers.txt`). Writes `AUDIT.md`, `MANIFEST.sha256` and a tarball; fails without a tarball if the audit fails.

## Product integration pass (2026-11-08 ~08:00Z): monorepo main, Sid's investigator views, M4 tunnel

### Instructions (condensed)
- Monorepo `Dhruvjain35/locki-airlock`, **main only** (no feature branch or PR). This overrides CONTRIBUTING.md's develop/PR flow for this import.
- Accept the hasmus25 invitation.
- Coordinate with Dhruv's integration session before pushing.
- Import the audited snapshot into `services/nemotron-investigator/` without the standalone git ancestry, with separate commits only for genuine changes.
- Get minimum read-only access through Sid; no owner credentials.
- M4 tunnel on M4 port 18787, using the actual M4 public key with valid OpenSSH forwarding restrictions. Transfer Dhruv's credential securely and never print it.
- Test through the M4 tunnel. Do not block alerts on Nemotron.

### What changed
- **Invitation.** The `Dhruvjain35/locki-airlock` invitation was accepted (write access). The `Dhruvjain35/Locki` invitation was left pending, because it was not asked for.
- **Sid's minimum authorization is deployed live** (`docs/airlock/INVESTIGATOR_INTEGRATION.md` in the monorepo).
  - The owner grants one incident with `grant_incident_evidence(workstation, identity, incident, eventIdsJson, includeBaseline)`.
  - The investigator then reads only `my_investigator_incidents`, `my_investigator_events` and `my_investigator_baselines`.
  - It must hold NO membership: `grant_reviewer` was the wrong ask and is withdrawn.
  - Sid prepared the grant for the real USB incident `incident:23000b77...` (workstation `dhruv-m4-air`, simulated=false). Only Dhruv, as the owner, can run it.
- **Adapter.** Now defaults to those views (`NI_STDB_VIEWS=investigator`). There are strict row types for the projection, which omits the rule, summary, actor and action fields; tools report those as "not in the evidence scope". Sessions and device health are outside the grant, so no query is made for them. Sid's real vocabulary (`usb_added`, `app_launched`, `person_in_zone`, ...) is categorized, and baselines that are JSON arrays of redacted device keys are matched on `deviceKey`. `evidence_complete=false` raises the `incident_evidence_incomplete` advisory.
  - Ping reports `authorized_incidents`, and any enabled membership is a least-privilege violation.
  - `/health` shows `live_evidence_has_no_incident_grant` until a grant exists.
  - Live probe: the three views accept the adapter's exact SQL (HTTP 200, 0 rows before any grant), the private table is still refused, and memberships are 0.
- **SSH on the M5.** There was no `~/.ssh` at all: that was the "rejection", and Dhruv logged in with a password.
  - `~/.ssh/authorized_keys` now holds exactly one line: Dhruv's only GitHub key (`github.com/Dhruvjain35.keys`, SHA256 `WeBg+LR4BsmiTgaZBBlxuUWBOktOQcywFpDyIo4nY/o`). Its options are `from=<M4 tailscale address>,restrict,port-forwarding`, permitopen 127.0.0.1/localhost on 8787 and 11434, `permitlisten="127.0.0.1:1"`, and forced command `~/.ssh/locki_tunnel_command.sh`.
  - Verified on this sshd with a throwaway key over localhost, then removed:
    - the API forward gives 401 without a key and 200 with one;
    - the `localhost` spelling also works;
    - a forward to :22 is "administratively prohibited";
    - shell, command and `scp` are refused, and `-R` fails;
    - the one-time handoff returns its content once, then refuses.
  - The existing password-based Ollama tunnel session (since 2026-11-07 15:55) was not touched.
- **Dhruv's API key reissued** for exactly `dhruv-m4-air` plus the six `ws:fixture-*` workstations. The old `*` key was never handed out and is revoked. The plaintext exists only as the one-time handoff file, deleted when the M4 reads it with `ssh <m5> investigator-key > ~/.locki/investigator.key`.
- **Docs.** INTEGRATION.md v0.3: 18787 tunnel, key fetch, alerts never wait on Nemotron, §7 = Sid's grant. No fixed private IPs (CONTRIBUTING), and `tests/test_api.py` uses a documentation address.

## LIVE M4 VOICE-CLIENT RUN ON A FRESH INCIDENT (2026-11-08 13:23Z) — latest
- **`inv_1581caaf36cf4101bb68`:** requester `dhruv-m4-backend` with the label "LOCKI voice", which is Dhruv's `NemotronService` client through the 18787 tunnel. It polled `?view=voice` and then read the full record twice, which is the owner console's read-only record route.
- **Incident:** a fresh, owner-granted, correlated incident `incident:193f2c67…` on `dhruv-m4-air`, with official severity **high** and simulated=false.
  - `/health` at 13:45Z: service ok, 2 authorized incidents (this one and `23000b77…`), memberships 0, least privilege ok.
- **Result:** `completed_with_warnings`, LIVE, real `nemotron-3.5-bolt:e4b`.
  - Nemotron chose the same 7 read-only tools, then `finish_investigation` with its own reason (`model_finished`). `get_session_status` returned out of scope.
  - 4/4 facts grounded (3 observed, 1 observed absence), 8/8 citations valid (accuracy 1.0), 0 unsupported claims.
  - The official high severity stands. Nemotron rated medium risk, low confidence, so the effective severity is high.
  - Advisories: `model_risk_below_official_severity` and `summary_replaced_by_grounding`.
  - The model's spoken summary was flagged `unsupported_attribution`, so the voice text is the deterministic one (`deterministic_due_to_flags`).
  - Headline: "Official severity high. Nemotron's evidence review: medium risk, low confidence. The official high severity stands."
- **Timing:** 15.31 s total, including a cold model load: the model had idle-unloaded, so the first tool decision took 4.37 s. Max prompt 4,290 tokens, above the nominal 4,096 and within the verified 9.8k retention.
- **Not visible from the M5:** whether LOCKI spoke the result after the recorded WAV. That has to be confirmed on the M4.
- **Sid's correction (issue #2, 10:24Z):**
  - `record_agent_step` requires an enabled *service* membership and a matching service device. Neither the Mac publisher nor the investigator can call it, and neither identity should be relabelled.
  - The dashboard shows the investigation through the owner-local read-only record route instead (Dhruv's `9a7e189`, on main).
  - The earlier `_agent_step` / `AgentStepStatus.COMPLETE` ask is withdrawn.
- **Sid's request (issue #2, 10:37Z):**
  - Activate the reviewed revision on the M5.
  - Keep a sanitized verifier receipt that includes the known-other-workstation check.

## LIVE END-TO-END VERIFIED (2026-11-08 09:14–09:15Z), first runs
- **Grant:** the owner's grant became visible at 09:14:29Z for `incident:23000b77…` (real USB event, simulated=false), with authorized_incidents=1 and memberships=0. Sid's verifier exited 0: private tables denied, 0 reducer calls. Report: `var/live/grant_chain_20261108T084755Z.verifier.json`.
- **`inv_b18cf295b6a141a8b1b7`** (mustafa-operator, `bench/live_e2e.py`):
  - `completed`, LIVE (X-Evidence-Mode live), real `nemotron-3.5-bolt:e4b`.
  - Nemotron chose 7 read-only tools (window_s 1800, chosen by the model) and then `finish_investigation`, with its own reason. `get_session_status` returned out of scope.
  - 5/5 facts grounded, citation accuracy 1.0, 0 flags.
  - Official severity medium; Nemotron medium risk, low confidence; effective medium.
  - Total 12.06 s, first tool decision 1.11 s, max prompt 3,935 tokens.
- **`inv_2f45b8730b5548c48fe9`** (requester **dhruv-m4-backend**, the M4 key through the 18787 tunnel):
  - `completed_with_warnings`, LIVE; same tool sequence and grounding.
  - The warning is `unsupported_intent_claim` on the rationale's "malicious intent" wording.
  - Total 11.86 s.
  - The M4 fetched the full record and `?view=voice`. This proves the authenticated M4→M5 API path.
- **M4 consumer:** Dhruv's `1bbbfbf` adds `backend/app/intelligence/nemotron_service.py` plus `runtime._what_happened_from_nemotron`.
  - It runs on the "what happened" intent for an authoritative `incident:<32 hex>`, posts live, polls `?view=voice` (45 s deadline) and speaks only `spoken_summary`.
  - It refuses anything that isn't LIVE, and bypasses the old local AirlockAgent.
  - The Recorded Voice panel shows "Nemotron LIVE · status · inv_ · ms".
- **Open at 09:20Z**, with the status at 13:55Z in brackets:
  - The recorded-WAV path had not run against a granted current incident. The 03:15 run polled the full record, so it wasn't the `NemotronService` client. [The voice client ran live at 13:23Z (section above). Whether LOCKI spoke it still has to be confirmed on the M4.]
  - Fresh demo incidents need their own owner grant. The recipe is in issue #2 comment 6719727896. [A fresh incident was granted. The owner approval UI is on main (`330530f`).]
  - The tool trace isn't published to the dashboard's Nemotron panel. [Withdrawn: `record_agent_step` is service-only, and the read-only record route is used instead.]
  - Keep the 23000b77 grant until the WAV test passes, then revoke it. [Still granted at 13:45Z.]

## Live end-to-end status before the grant (2026-11-08 ~08:50Z)
- **M5:**
  - `serve` is running (pid 35055) on 127.0.0.1:8787, and authenticated `/health` returns 200.
  - The model `nemotron-3.5-bolt:e4b` is installed (digest fe4af3f5907b) but was idle-unloaded at the time of the check.
  - Health is `degraded: live_evidence_has_no_incident_grant` only. The live backend is reachable with 0 memberships, least privilege ok and authorized_incidents 0.
- **M4 tunnel:**
  - The one-time key handoff (`.secrets/handoff/dhruv-m4-backend.once`) was collected at 02:37 CT.
  - A no-shell SSH session from the M4 has been up since then, and authenticated `/health` 200s that were not ours appear in `var/server_8787.log`.
  - Not yet seen: a `POST /investigations` from requester `dhruv-m4-backend`.
- **Grant:** Sid's command (docs/airlock/INVESTIGATOR_INTEGRATION.md, issue #2 07:55Z) is not yet run by the owner. A background watcher (`bench/wait_for_grant.py` 15 s) then runs Sid's `scripts/airlock_investigator_verify.mjs` and `bench/live_e2e.py`. Output goes to `var/live/grant_chain_*`.
- **M4 consumer:** specified in issue #2 (comment 6719457971); implementation is Dhruv's.
  - Swap the local `AirlockAgent` in `security/service.py:what_happened()` for POST, poll and `?view=voice`, so no competing report is made.
  - Publish the trace with `_agent_step` (`record_agent_step`), plus a final `investigation_summary` step whose text starts with LIVE.
  - Add `AgentStepStatus.COMPLETE`, because the reducer only accepts running, succeeded, failed or complete, and the first failed step makes a run terminal.
- **Demo order:** incident → immediate warning → owner grants → recorded WAV "LOCKI, what happened while I was gone?" → "Checking" → ~10–13 s → spoken live report + dashboard steps.

## Next steps
1. **Mustafa: decide whether to activate Sid's reviewed revision on the M5.**
   - Sync the lane code from main into the standalone, run `pytest -q`, and restart with `scripts/serve.sh --daemon`.
   - Then run one live investigation on a granted incident.
   - Agree the time with Dhruv so the restart doesn't cut off a demo run.
2. **Mustafa: keep a sanitized verifier receipt** (INTEGRATION.md §7).
   - Sid's verifier now needs a private JSON token wrapper and `--outside-workstation` set to a known other workstation.
   - The detailed report stays private.
3. **Dhruv: confirm on the M4** that LOCKI spoke the 13:23Z result after the recorded WAV ("LOCKI, what happened while I was gone?").
4. **Owner (Dhruv): manage the grants.**
   - Grant each fresh demo incident with the owner approval UI on main (`330530f`).
   - Revoke the `23000b77…` and `193f2c67…` grants after the agreed trial.

## Change log
- 2026-11-08 ~13:55Z:
  - Recorded the 13:23Z live run from the M4 voice client on a fresh high-severity incident.
  - Recorded Sid's `record_agent_step` correction, and that his reviewed revision of this lane is on main but not active on the M5.
  - Pushed this file to monorepo main, its first update there since the import. No code changes.
- 2026-11-08 ~09:20Z:
  - Live end-to-end verified: owner grant, Sid's verifier passing, and live investigations from the M5 operator and from the M4 (dhruv-m4-backend through 18787).
  - Reviewed Dhruv's consumer `1bbbfbf`: its contract matches.
  - Posted the remaining actions in issue #2 (comment 6719727896). No code changes.
- 2026-11-08 ~08:50Z: live integration push. M5 status verified (service, model, live backend, M4 key collected). Posted the M4 consumer contract and the grant ask on issue #2. Started the grant watcher, verifier and live E2E chain. No code changes.
- 2026-11-08 ~08:00Z: product integration pass (section above): monorepo main import, Sid's investigator views, M4 tunnel.
- 2026-11-08 ~07:00Z: final integration pass (section above): evidence modes and disjoint id spaces, official/effective severity, voice view rewrite, context verification + probe, live E2E/grant-wait/probe-redaction scripts, operator listing on the live adapter, CLI `run --live`, `scripts/serve.sh`, snapshot export + audit, INTEGRATION.md v0.2, benchmark v4, cold start measured.
- 2026-11-08 ~01:05Z:
  - Applied all 40 verified review fixes. The core was done in this session. The API and adapter fixes were made in parallel by two file-disjoint agents, then integrated and re-tested.
  - Added a text-last constrained-decoding layout and a deterministic citation repair.
  - Incomplete reads (backend row caps) can no longer be cited as absence.
  - Fixed a regression where a 1 s floor overrode a configured step timeout below 1 s; a test now covers it.
  - Verified live on Maincloud that the hardened SQL is accepted: `id AND workstation_id`, `received_at_ms` windows and LIMIT.
  - Reran: the API e2e check, the Relay demo (blocked B / allowed C) and `pytest -m live` (3/3).
  - 259 deterministic tests pass. Benchmark v3 recorded.
  - Commits: `a624256`, `2e36c99`, plus this one.
- 2026-11-07: Created the service, fixture scenarios A/B/C/N1–N3, live SpacetimeDB adapter, API, tests and benchmark. Fixed the registry kwarg clash, the overclaim false positive on passive "disarmed", the finalize whitespace loop (schema order plus guard and relaxed retry), dropped incident-ID prefixes (canonicalization), and result-id citation scoring. Committed locally (`968b792`).
