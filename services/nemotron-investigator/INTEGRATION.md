# Integration contract: Nemotron investigator

Owner: Mustafa (M5 Pro). Consumers: Dhruv's Mac backend and LOCKI voice, Sid's dashboard.
Status: **v0.2 (2026-11-08), to be confirmed with Dhruv and Sid.** It replaces v0.1. The breaking changes are listed in §9.

## 0. Quick start

### On Mustafa's M5 (service host)

```bash
cd ~/locki-airlock-nemotron/services/nemotron-investigator
scripts/serve.sh --daemon          # background; log var/server_8787.log, pid var/server_8787.pid
# foreground instead:  scripts/serve.sh
```

`scripts/serve.sh` sets the following, and each can be overridden from the environment:

| variable | value used | meaning |
|---|---|---|
| `NI_EVIDENCE_BACKEND` | `spacetimedb` | Default evidence mode is **live**: LOCKI SpacetimeDB through the read-only investigator identity. Set it to `fixture` for a test-data-only server. |
| `NI_FIXTURE_ENABLED` | `1` | Also accept `"evidence_mode": "fixture"` requests (TEST DATA) for integration testing. Set it to `0` to refuse them. |
| `NI_BIND_HOST` / `NI_BIND_PORT` | `127.0.0.1` / `8787` | Loopback only. A non-loopback bind is refused unless `NI_ALLOW_NON_LOOPBACK=1`. |

Defaults that normally need no change:

| variable | default |
|---|---|
| `NI_OLLAMA_URL` | `http://127.0.0.1:11434` |
| `NI_MODEL` | `nemotron-3.5-bolt:e4b` |
| `NI_STDB_HOST` | `https://maincloud.spacetimedb.com` |
| `NI_STDB_DATABASE` | `locki-w91bw` |
| `NI_STDB_TOKEN_FILE` | `.secrets/stdb_investigator.token` (the investigator identity's token, mode 0600) |
| `NI_API_KEYS_FILE` | `.secrets/api_keys.json` (SHA-256 digests only, mode 0600) |
| `NI_STORE_DIR` | `var/investigations` (records are mode 0600; live records hold private evidence) |
| `NI_RELAY_GATE` | `0` (`1` routes `get_recent_usb_events` through the NVIDIA NeMo Relay policy gate) |

The service never sends `keep_alive` or `num_ctx` to Ollama, so it never reconfigures or reloads the shared model.

### On Dhruv's M4 (client)

1. Open a *separate* SSH local forward. It is a new process; the existing `11435` Ollama tunnel is not touched or restarted:

   ```bash
   ssh -N -o ExitOnForwardFailure=yes -L 8787:127.0.0.1:8787 mustafah@<m5-tailscale-name>
   ```

   If local port 8787 is taken, use `-L 8788:127.0.0.1:8787` and call port 8788 instead.

2. Store the API key you receive out of band in a file only you can read, for example `~/.locki/investigator.key` with mode 0600. Mustafa's copy is `.secrets/handoff/dhruv-m4-backend.key`. Keys are never pasted into chat, code or the repo.

3. Suggested client settings (the names are yours to choose):

   ```bash
   LOCKI_INVESTIGATOR_URL=http://127.0.0.1:8787
   LOCKI_INVESTIGATOR_KEY_FILE=~/.locki/investigator.key
   ```

4. Verify. These commands pass the key without echoing it or putting it in the process list:

   ```bash
   AUTH() { printf 'Authorization: Bearer %s' "$(cat ~/.locki/investigator.key)"; }
   curl -s -H @<(AUTH) http://127.0.0.1:8787/health | python3 -m json.tool
   # expect evidence_modes.default = "live"; until Sid's grant, evidence_backends.live.memberships = 0

   # a TEST DATA run (works today):
   curl -s -H @<(AUTH) -H 'Content-Type: application/json' \
     -d '{"incident_id":"incident:fixture-b-person-then-usb","evidence_mode":"fixture"}' \
     http://127.0.0.1:8787/investigations
   curl -s -H @<(AUTH) "http://127.0.0.1:8787/investigations/<investigation_id>?view=voice" | python3 -m json.tool

   # a LIVE run (after Sid's grant), with a real id from LOCKI:
   curl -s -H @<(AUTH) -H 'Content-Type: application/json' \
     -d '{"incident_id":"incident:<32 hex>","evidence_mode":"live"}' http://127.0.0.1:8787/investigations
   ```

Operator end-to-end check on the M5, through the same API: `.venv/bin/python bench/live_e2e.py --key-file .secrets/handoff/mustafa-operator.key`. It prints the incident id, tool trace, latency and unsupported claims, and stores the full record only under `var/live/`.

It refuses to run when:
- the investigator identity is over-privileged;
- the only visible incidents are rows marked `simulated=true` (unless you pass `--allow-simulated`).

## 1. Where it runs

- The service runs on Mustafa's M5 at `http://127.0.0.1:8787`, bound to loopback only. Remote access is only through Dhruv's SSH forward over Tailscale (§0).
- It calls the local Ollama at `127.0.0.1:11434`, model `nemotron-3.5-bolt:e4b` (MLX runner).

## 2. Authentication

- **Every endpoint requires `Authorization: Bearer <api-key>`.** A request without a valid key gets 401 before its body is read. Only SHA-256 digests are stored.
- **Creating a key.** Run `.venv/bin/python -m nemotron_investigator create-key --requester <id> --workstation <ws> [--workstation <ws> ...] [--scope <scope> ...] --out .secrets/handoff/<id>.key`.
  - With `--out`, the key is written to a new file (key only, mode 0600) and is not printed. Without it, the key is printed once.
  - Scopes default to `investigations:create`, `investigations:read` and `health:read`. `--scope health:admin` must be named explicitly.
- **Current keys** (in `.secrets/handoff/`, mode 0600, never printed):

  | key | scopes | workstations |
  |---|---|---|
  | `dhruv-m4-backend` | `investigations:create`, `investigations:read`, `health:read` | `*` |
  | `mustafa-operator` | the default scopes plus `health:admin` | `*` |

  `*` is still bounded by the investigator identity's SpacetimeDB memberships, which Sid controls: the service can only see the workstations Sid grants.
- **Narrowing Dhruv's key.** Once the demo workstation id is known, the key is reissued for exactly that workstation plus the six fixture workstations. Workstation grants are exact matches; there are no prefix globs.

  ```bash
  .venv/bin/python -m nemotron_investigator create-key --requester dhruv-m4-backend --workstation <demo ws> \
    --workstation ws:fixture-a --workstation ws:fixture-b --workstation ws:fixture-c \
    --workstation ws:fixture-n1 --workstation ws:fixture-n2 --workstation ws:fixture-n3 \
    --out .secrets/handoff/dhruv-m4-backend.v2.key
  ```

  Then revoke the old entry (set `"revoked": true` on it in `.secrets/api_keys.json`).
- **Reading investigations:** a requester reads only its own investigations, and only for workstations its current key still covers. Any other id returns 404.
- **The key file is validated strictly:** malformed entries are skipped, a revocation always wins, and a file writable by group or others is refused.

## 3. Evidence modes: live vs test data

Every investigation runs on exactly one evidence source, fixed when it is created.

| | live | fixture |
|---|---|---|
| source | LOCKI SpacetimeDB `locki-w91bw`, identity-scoped `my_*` views, read-only investigator identity | `fixtures/scenarios/*.json` (synthetic) |
| incident ids accepted | `incident:<32 lowercase hex>` only | `incident:fixture-<slug>` only |
| row ids | as minted by LOCKI | carry an `fx-` marker (`evt:fx-...`) |
| `evidence_mode` / `evidence_badge` | `live` / `LIVE` | `fixture` / `TEST DATA` |
| `evidence_banner` | `LIVE EVIDENCE SOURCE: SpacetimeDB database locki-w91bw via the read-only investigator identity c200b5b1...e88883`, plus a simulated-row note when the incident is marked `simulated=true` | `TEST DATA: fixture evidence from fixtures/scenarios, NOT read from the live LOCKI database` |
| `X-Evidence-Mode` response header (POST 202 and GET of a run) | `live` | `fixture` (and `unknown` for an old record with no recorded mode) |
| spoken text opens with | the official severity, after "LOCKI marked this incident as simulated." for a simulated incident | `Test data only, not the live LOCKI database.` |

The banner names the evidence source; whether anything was actually read is in `tool_execution_trace`.

Older records:
- **Mode recovered from provenance.** Records written before evidence modes existed get their mode from `evidence_provenance.live`.
- **No provenance.** A record with no provenance at all is served with the badge `UNVERIFIED`, and its voice text opens with "The evidence source of this record is not recorded."
- **Full record labels.** The full record carries `evidence_mode` and `evidence_badge` at the top level, including while it is queued or running and after an interruption.

Guarantees:
- **No silent substitution.** The request's `evidence_mode` (or the server default) decides the source. A mode the server does not offer gets 409; an unavailable backend gets 503. The service never falls back from live to fixture.
- **A real incident is never investigated on fixtures.** A live-format id with `evidence_mode: "fixture"` gets 422, and a fixture id never queries the live database. The agent enforces the same check in its preflight.
- **Every tool result** carries `backend` and `live_data`.
- **Simulated rows.** A live incident record marked `simulated=true` is detected at preflight. The result gets `incident_simulated: true`, the headline starts with `SIMULATED INCIDENT.`, the `simulated_evidence_present` advisory is set, and the run is marked for review. Simulated event rows raise the same advisory.

**UI rule** for Sid's dashboard and Dhruv's app (the investigator ships no UI of its own):
- Render `evidence_badge` on every investigation view: LIVE in one color, TEST DATA in a warning color.
- Show `headline` first.
- Never show a TEST DATA result without its badge.

## 4. Endpoints

### POST /investigations → 202

```json
{
  "incident_id": "incident:<32 hex>",
  "evidence_mode": "live",
  "requester": {"identity": "dhruv-m4-backend", "display_name": "Mac backend"},
  "context": {"note": "owner asked by voice: what happened?"}
}
```

- `incident_id` is required. Live runs take `incident:<32 lowercase hex>`; fixture runs take `incident:fixture-<slug>`.
- `evidence_mode` is optional: `"live"` or `"fixture"`, defaulting to the server's default (`live` here). **Send `"live"` explicitly in production.**
- `requester` is an informational label; the API key is the authority.
- `context.note` is at most 500 characters. It is **untrusted**: the model sees it only as data.
- Unknown fields get 422.

Response, with header `X-Evidence-Mode`:

```json
{"investigation_id": "inv_1f0c...", "status": "queued", "evidence_mode": "live", "evidence_badge": "LIVE",
 "links": {"self": "/investigations/inv_1f0c...", "voice": "/investigations/inv_1f0c...?view=voice"}}
```

Errors:

| status | cause |
|---|---|
| 401 | no or invalid key |
| 403 | key lacks scope |
| 409 | evidence mode not enabled |
| 413 | body over 8 KiB |
| 422 | invalid body, or id not valid for the evidence mode |
| 429 | admission limits: 8 active in total, 4 per requester, 30 creations per requester per minute; with `Retry-After` |
| 503 | backend unavailable, or shutting down |

Investigations run one at a time against the shared model:
- A queued run waits at most min(120 s, `total_timeout_s`), then ends `timeout` with `queue_timeout`.
- A run is bounded at `total_timeout_s` + 15 s.

### GET /investigations/{id} → 200

Poll every 0.5–1 s. Measured latency, warm (benchmark v5, `bench/results/`):
- fixture runs: total p50 8.7–10.5 s, first tool decision p50 272–351 ms (`bench/results/bench_20261108T073155Z.md`);
- cold (model not resident): first tool decision 5.0 s (model load 4.1 s), total 12.8 s (`bench/results/cold_start_20261108T062220Z.json`).

Record shape, abridged; key order is not significant:

```json
{
  "investigation_id": "inv_...", "incident_id": "incident:...", "requester_id": "dhruv-m4-backend",
  "evidence_mode": "live", "status": "queued | running | completed | completed_with_warnings | degraded | failed | timeout | rejected | interrupted",
  "progress": [{"kind": "preflight_ok", "workstation_id": "ws:..."}, {"kind": "model_turn", "step": 1, "tool_calls": ["get_incident"]}],
  "result": {
    "evidence_mode": "live", "evidence_badge": "LIVE", "evidence_banner": "LIVE EVIDENCE: ...",
    "investigation_status": "completed_with_warnings", "model_id": "nemotron-3.5-bolt:e4b",
    "evidence_provenance": {"mode": "live", "backend": "spacetimedb", "live": true, "label": "SpacetimeDB locki-w91bw (identity-scoped my_* views)"},
    "official_severity": {"level": "high", "raw": "high", "rule_id": "...", "source": "incident record in LOCKI SpacetimeDB (my_incidents.severity)", "authoritative": true},
    "effective_severity": {"level": "high", "basis": "official_severity | nemotron_escalation | official_severity_unrecognized", "rule": "max(official severity, Nemotron risk): ..."},
    "risk_assessment": {"level": "low|medium|high|critical", "confidence": "low|medium|high", "rationale": "...", "determined_by": "model_advisory"},
    "headline": "Official severity high (rule ...). Nemotron's evidence review: medium risk, medium confidence. The official high severity stands.",
    "summary": "...", "spoken_summary": "...", "summary_source": "model | model_spoken_summary | deterministic | deterministic_due_to_flags",
    "observed_facts": [{"fact": "...", "evidence_ids": ["evt:..."], "basis": "observed | observed_absence | incident_record"}],
    "possible_explanations": [{"explanation": "...", "likelihood": "low|medium|high", "supporting_evidence_ids": ["..."], "basis": "inferred"}],
    "missing_information": ["...", "[system] camera/vision events were not checked by the investigator ..."],
    "recommended_actions": [{"action": "...", "rationale": "...", "requires_human_approval": true, "executed_by_agent": false}],
    "unsupported_claims": [{"claim": "...", "reason": "...", "cited_ids": ["..."]}],
    "advisories": [{"code": "...", "detail": "..."}],
    "evidence_gaps": [{"tool": "...", "status": "not_queried | evidence_unavailable | ...", "error": null}],
    "timeline": [{"t_ms": 0, "time": "ISO-8601", "t_rel_s": -0.3, "evidence_id": "evt:...", "type": "device_event", "label": "usb: usb_device_connected (iPhone)"}],
    "tool_execution_trace": [{"step": 1, "tool": "get_incident", "arguments": {"incident_id": "..."}, "status": "ok",
                              "duration_ms": 1.1, "evidence_ids": ["incident:..."], "result_summary": "...", "backend": "spacetimedb", "live_data": true}],
    "model_calls": [{"step": 1, "purpose": "tool_selection", "wall_ms": 457.2, "prompt_tokens": 1023, "request_chars": 4700,
                     "tool_calls_proposed": [{"name": "get_incident", "arguments": {"incident_id": "..."}}]}],
    "model_latency_ms": 0, "tool_latency_ms": 0, "total_latency_ms": 0, "time_to_first_tool_decision_ms": 0, "queue_wait_ms": 0,
    "validation": {"facts_proposed": 0, "facts_grounded": 0, "facts_rejected": 0, "citation_accuracy": 1.0, "flags": []},
    "context_verification": {"max_prompt_tokens": 4767, "max_estimated_prompt_tokens": 3859, "nominal_context_length": 4096,
                             "nominal_context_source": "api_ps_preflight | api_ps_after_load | assumed_default",
                             "verified_retention_tokens": 9800, "truncation_suspects": [],
                             "status": "within_nominal_context | above_nominal_but_verified_retained | unverified | unknown | truncation_suspected"},
    "incident_simulated": false, "untrusted_text_values": ["device/app names shown to the model (never spoken)"],
    "runtime": {"ollama_version": "0.40.15", "runner": "mlx", "model_digest": "fe4af3f5...", "loaded_context_length": 4096},
    "conversation": ["the full Nemotron message history: the actual inference requests"]
  }
}
```

| status | meaning |
|---|---|
| completed | Assessment produced. Every tool call was valid and successful, and there were no validation flags or advisories. |
| completed_with_warnings | Assessment produced, with an advisory, a flagged or rejected claim, or an invalid or blocked tool call. |
| degraded | Assessment produced, but at least one evidence read failed. See `evidence_gaps`. |
| failed | No assessment: the model or evidence backend was unavailable, or the model output was invalid. Nothing is fabricated. |
| timeout | A model, queue or total deadline was hit. There is no assessment. |
| rejected | Bad id, an id not valid for the evidence mode, an incident not found or not authorized (deliberately indistinguishable), or a revoked key. The model is never called. |
| interrupted | The service stopped before the run finished. |

Advisory codes:

| code | meaning |
|---|---|
| `model_risk_below_official_severity` | Nemotron's risk is below the official severity. The official severity stands. |
| `official_severity_unrecognized` | The official severity is outside low/medium/high/critical. |
| `high_confidence_with_evidence_gaps` | Nemotron reported high confidence while key evidence is missing. |
| `untrusted_instruction_like_content` | Evidence text looked like instructions. It was treated as data. |
| `investigation_stopped_by_limit` | A limit stopped the investigation, not Nemotron's own decision. |
| `summary_replaced_by_grounding` | Nemotron's summary tripped a grounding rule and was replaced. |
| `model_omitted_fields` | Nemotron left out required assessment fields. |
| `incomplete_evidence_reads` | A read hit a backend row or id limit, so that window is incomplete. |
| `simulated_evidence_present` | Some retrieved rows are marked `simulated=true`. |
| `context_retention_unverified` | The context check came out `unverified` (prompt above the nominal context on a runtime not verified by the probe), `unknown` (runtime or token counts not identified) or `truncation_suspected` (the reported prompt size stopped growing with the request, or fell far below the independent estimate). |

### GET /investigations/{id}?view=voice → 200 (for LOCKI's voice)

```json
{"investigation_id": "inv_...", "status": "completed_with_warnings", "incident_id": "incident:...",
 "evidence_mode": "live", "evidence_badge": "LIVE", "evidence_banner": "LIVE EVIDENCE: ...",
 "spoken_summary": "LOCKI rated this incident high severity. A hand was observed near a USB port. An unknown USB mass storage device was connected. The purpose of the unknown USB device is not established. Nemotron's evidence review: medium risk, medium confidence. The official high severity stands. This needs human review.",
 "spoken_summary_source": "model | grounded_facts | counts_only | status_template", "spoken_summary_flagged": false,
 "headline": "...", "official_severity": "high", "effective_severity": "high",
 "nemotron_risk_level": "medium", "nemotron_risk_confidence": "medium",
 "needs_review": true, "review_reasons": ["status_not_completed", "advisories"], "advisory_codes": ["model_risk_below_official_severity"],
 "requires_human_approval": true, "evidence_live": true, "evidence_label": "SpacetimeDB locki-w91bw (identity-scoped my_* views)"}
```

How `spoken_summary` is built. It is deterministic, and this service plays no audio:
1. **Lead.**
   - Fixture runs open with "Test data only, not the live LOCKI database."
   - Records with no recorded mode open with "The evidence source of this record is not recorded."
   - Simulated incidents add "LOCKI marked this incident as simulated."
2. **Official severity.** "LOCKI rated this incident <official> severity." This always comes before Nemotron's view.
3. **Findings, at most three sentences.**
   - **Counts only.** If the run has untrusted content, a claimed action or missing validation, only counts are read ("Nemotron reviewed N evidence records ...").
   - **Nemotron's spoken summary** is read only if all of these hold:
     - Nemotron did **not** rate the risk below the official severity;
     - the summary is unflagged;
     - it passes the overclaim checks;
     - it passes the broad official-severity guard (`assessment.understates_severity`: lower level words, a negated official level, or dismissals such as "benign" or "false positive");
     - it contains none of the attacker-controllable strings in `untrusted_text_values`.
   - **Otherwise** up to two grounded observed facts that pass the same checks are read. If none pass, a deterministic category count is read ("The retrieved evidence includes 1 USB event and 2 camera events.").
   - **Last sentence.** One unknown: Nemotron's missing-information item, read only under the same conditions and only if it is a real unknown ("unknown", "not established", "who/whether ..."). Otherwise the deterministic evidence gaps are read ("Not available to the investigation: ...").
4. **Nemotron's view.** "Nemotron's evidence review: <level> risk, <confidence> confidence." If that is lower, add "The official <official> severity stands."; if higher, add "Nemotron rates it above the official severity."
5. **Review.** "This needs human review." whenever `needs_review` is true.

## 5. Severity: official vs Nemotron

- **`official_severity`** is the incident record's `severity` from LOCKI's deterministic correlation rule. It is copied verbatim (`raw`) and normalized (`level`), and it is never modified.
- **`risk_assessment`** is Nemotron's own judgment of the evidence (`determined_by: model_advisory`). It is reported as Nemotron produced it after grounding.
- **`effective_severity`** = max(official, Nemotron). Nemotron can raise the review priority (`nemotron_escalation`) but never lower it. If the official value is not a standard level, `effective_severity.level` is null; the model's level never stands in for it.
- A high-severity incident is never presented as medium.
  - The headline and voice text always lead with the official severity, and a lower Nemotron rating is labelled as Nemotron's view, followed by "the official severity stands".
  - If Nemotron's own summary wording understates the official severity, grounding replaces it with the deterministic summary (flag `understates_official_severity`, advisory `summary_replaced_by_grounding`), so the full record does not carry that wording either.

## 6. What the agent can and cannot do

- **Read-only tools.** It can call only these 7 tools, each scoped to the one authorized incident:
  - `get_incident`
  - `get_session_status`
  - `get_incident_evidence`
  - `get_recent_usb_events`
  - `get_recent_vision_events`
  - `get_recent_application_events`
  - `get_device_baseline_status`

  It can also call `finish_investigation`, which only ends the run.
- **No actions.** It cannot lock, unlock, arm, disarm, approve, acknowledge, resolve or write anything; no such tool exists, and the SpacetimeDB adapter only issues fixed SELECT templates on `my_*` views. Recommended actions are suggestions for an authenticated human.
- **Context window.** `/api/ps` reports a nominal 4096 for the shared runner. `bench/context_probe.py` verified full retention up to 9.8k tokens on Ollama 0.40.15 / MLX: random codes planted at the start of the system prompt and the first user message were recalled exactly. Real investigation prompts peak around 5k tokens.
  - **Per-run detection.** Each run measures its prompt size independently of Ollama's own count, because that count is reported after any truncation. It flags `truncation_suspected` when the reported size stops growing with the request or falls far below the estimate.
  - **Limit.** Token-level eviction inside a runner cannot be seen in token counts at all. Only the probe verifies it, which is why any runtime the probe has not covered is `unverified` above its nominal context.

## 7. Request to Sid (SpacetimeDB access): needs your action

- **Investigator identity, created 2026-11-07:** `c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883`. It is not the owner, and its token stays on the M5 at mode 0600. No owner credential is used anywhere.
- **Probed live 2026-11-08, read-only:**
  - 0 memberships and 0 visible incidents;
  - a direct `SELECT * FROM incidents` is refused ("no such table ... may be marked private");
  - the bounded `my_*` queries with `LIMIT` are accepted.

  The redacted record is in `bench/results/stdb_live_probe.json`.
- **Ask:** grant read-only membership on the demo workstation, without approval rights:

  ```bash
  spacetime call --server maincloud locki-w91bw grant_reviewer '"<workstation_id>"' '"c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883"' false
  ```

  (`grant_reviewer(workstationId, identityHex, canApprove=false)`.) The grant is reversible with `revoke_member`. An over-privileged grant is never treated as success:
  - every health check reports a `can_approve` or owner membership as a least-privilege violation (`live_evidence_identity_over_privileged`, service `degraded`);
  - `bench/wait_for_grant.py` exits 4 instead of 0;
  - `bench/live_e2e.py` refuses to run.
- **Please confirm:**
  1. That a reviewer with `canApprove=false` cannot call write reducers such as `acknowledge_incident`, `resolve_incident`, `request_lock` or `decide_action`. If it can, a view-only role would be safer. The investigator never calls any reducer, but the token should not carry more than reads.
  2. That reviewer members see rows in `my_incidents`, `my_sessions`, `my_device_events`, `my_baseline_snapshots` and `my_device_health`.
  3. The real `device_events.source` and `kind` vocabularies. The current guesses are in `categories.py`.
  4. The shape of `sessions.baseline_json`. The parser expects `usb_devices: [{vendor_id, product_id, serial_hash, name}]`.
- **Not done on purpose:** `record_agent_step` exists, but it is a write. If you want traces in the dashboard, we can add a deterministic (non-model) trace sink after you approve it.

## 8. Monorepo placement

The package is self-contained and goes to `services/nemotron-investigator/` in `Dhruvjain35/locki-airlock` as one fresh commit; the standalone git history is not pushed. The clean snapshot is produced by `scripts/export_snapshot.sh`, which excludes `.git`, `.venv`, `.secrets`, `var/`, caches and any live evidence. Its audit report is next to the snapshot.

## 9. Changes from v0.1 (breaking for clients)

- **Voice view renames.** `risk_level` and `risk_confidence` are now `nemotron_risk_level` and `nemotron_risk_confidence`, alongside the new `official_severity`, `effective_severity`, `headline`, `evidence_mode`, `evidence_badge` and `evidence_banner`.
- **Fixture incident ids** are now `incident:fixture-<slug>`. The old fixture id that reused a real incident id is gone.
- **New request field.** `evidence_mode` is accepted on POST, and the server default is now `live`.
- **Advisory rename.** `risk_below_rule_severity` is now `model_risk_below_official_severity`.
