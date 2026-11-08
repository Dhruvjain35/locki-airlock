# Integration decisions and Sid review

This snapshot joins two development lanes that diverged from the same base:

The initial migration tables below retain their original source-specific intent
and byte-identity claims. Current integration decisions and check scope are recorded
after those historical tables and in [RELEASE_ACCEPTANCE.md](airlock/RELEASE_ACCEPTANCE.md).

| Side | Branch | Commit |
|---|---|---|
| base | merge base | `6078e56` |
| ours | `feature/airlock-dhruv`: Mac backend, CV, security modes, demo, and all voice work | `294c1ff` |
| theirs | `feature/airlock-sid`: SpacetimeDB module, dashboard, bridges, CI | `00f3c44` |

The commit IDs belong to the original `Dhruvjain35/locked-in` repository. None of its history is in this repository.

As Sid asked in `docs/airlock/M4_INTEGRATION.md`, each conflict below records the intent of both sides, the selected resolution, who owns the
edit, and the test that shows both requirements survive. **The resolutions are proposals.** The rows owned by Sid, or by Sid and Dhruv, are not
agreed until Sid approves them in review. Apart from the one-line docstring in row 4, neither side was taken wholesale, and the SpacetimeDB bindings were not regenerated:
`spacetime/src/index.ts` and `frontend/src/airlock-bindings/` are exactly Sid's `00f3c44`.

## The seven textual conflicts

| # | File | Ours (intent) | Theirs (intent) | Proposed resolution | Owner | Test that both survive |
|---|---|---|---|---|---|---|
| 1 | `backend/app/intelligence/airlock_agent.py` (add/add) | The Python investigator: 10 gated tools, NeMo Relay, deterministic report checks. `security/service.py` imports it | A bounded Nemotron HTTP transport, called by `scripts/airlock_nemotron.ts`. The TS adapter owns the tools and publishing | Keep ours at this path. Sid's file moves unchanged to `nemotron_transport.py`, and its two callers (`scripts/airlock_nemotron.ts`, `scripts/airlock_nemotron_test.py`) and `docs/airlock/NVIDIA.md` point there. **Open: which investigator consumes incidents in the demo.** Only one may | Sid and Dhruv | `backend/tests/test_airlock_agent.py`; `scripts/airlock_nemotron_test.py` (Sid's transport at its new path); `scripts/airlock_nemotron_tools.test.ts` |
| 2 | `Makefile` | `airlock` starts the Mac backend with security, camera and voice | `airlock` starts SpacetimeDB and the dashboard; also `airlock-cloud`, `airlock-build`, `airlock-verify` | Sid keeps `airlock` and his three targets. Ours becomes `airlock-mac` (same flags: `VISION=1`, `QUIET=1`, `LOCK=`), with `.PHONY` a union. `setup`/`preflight` stay ours (`scripts/setup_mac.sh` installs the same deps as Sid's `uv sync` line). Every doc and script that said `make airlock` for the Mac backend now says `make airlock-mac` | Sid and Dhruv | `make -n airlock`, `make -n airlock-cloud`, `make -n airlock-mac` each expand to their own side's command (see MIGRATION.md) |
| 3 | `frontend/src/App.tsx` | `#demo` and a Demo button open the live demo inside the Mac view | `App` routes to `AirlockApp` (his dashboard) by default and to `LegacyApp` with `?product=legacy` | Keep Sid's router and default. The Mac view is `LegacyApp`, which still opens the demo on `#demo`. The router also sends `#demo` to `LegacyApp` and follows hash changes, so a "Demo" link from the dashboard works without `?product=legacy` | Sid | `npm run build` (type check) and `npm test` in `frontend/`; by hand: `/` shows the security dashboard, `/#demo` the demo, `/?product=legacy` the Mac view |
| 4 | `backend/app/security/__init__.py` (add/add) | Package docstring: the Mac adapter, detection only | One-line docstring: workstation security is separate from robot motion safety | Ours. Sid's sentence ("Workstation security is separate from robot motion safety.") was tried and fails our guard `test_nothing_in_the_security_package_can_move_the_robot_or_touch_firmware`, which forbids the package from mentioning robot safety. Its meaning is that guard, so the test keeps it | Dhruv | `backend/tests/test_security_watchers.py` |
| 5 | `docs/airlock/CONTRACTS.md` (add/add) | Mac and camera messages, camera kind mapping, security modes and visual state, voice-arming proposal | Sid's answers: Mac subscriptions, exact metadata keys, health statuses and components, verification boundary | Both texts whole. Sid's comes first because his module is the authoritative contract; ours follows as "The Mac side". Where they differ, the module wins | Both | Both full texts are present unchanged (diff-checked in MIGRATION.md) |
| 6 | `README.md` | LOCKED IN description; "Built by Dhruv and Mustafa" | AIRLOCK introduction and dashboard quick start; all four builders | A new README for this repository. Both previous texts are kept whole in `docs/legacy/README_BEFORE_MIGRATION.md`. Sid's dashboard quick start is linked from `docs/airlock/README.md` | Dhruv | Doc only |
| 7 | `firmware/README.md` | Warning: do not flash patch v2 | AIRLOCK: archived, no flash, no motor commands | Both warnings kept in the merge. `firmware/` itself is **not shipped** here, because AIRLOCK uses no robot firmware | Dhruv | Doc only |

## Semantic conflicts that merged without a marker

| # | What | Why it matters | Proposed resolution | Owner |
|---|---|---|---|---|
| 8 | `Makefile`: Sid's new top-level `SPACETIME` variable is the path of the `spacetime` CLI. Our Mac target read `$(SPACETIME)` as the database URL | Unchanged, `make airlock-mac` would have pointed the Mac backend at the CLI binary's path as its server | Our variable is renamed `SPACETIME_URL` (`make airlock-mac SPACETIME_URL=... SPACETIME_DB=...`) | Dhruv |
| 9 | Sid's `backend/app/security/voice_service.py` (standalone loopback speech service), `announcements.py`, and `mac_observer.py` (USB snapshot poller) sit beside our voice stack and IOKit watcher | They are a second incident-to-speech path and a second USB monitor. `mac_observer.py` doesn't take our `usb-monitor.lock`, so it can run beside the backend. Nothing of ours imports them, and only Sid's `scripts/airlock_mac_bridge.ts`, `airlock_voice_bridge.ts` and `airlock_observer_smoke.ts` start them | Both kept and neither wired up. **Rule: one voice consumer and one USB monitor per workstation.** For the demo that's the Mac backend (`make airlock-mac`), so don't start Sid's voice or observer bridges beside it. Which one is the long-term path is for Sid and Dhruv | Sid and Dhruv |
| 10 | Sid's ARM delegation (`authorize_arm`, `arm_session_delegated`, `revoke_arm_authorization`), **deployed to `locki-w91bw` on 2026-11-08** | Sid asked that it be wired into Dhruv's voice path explicitly, not through his generic speech handler | **Wired on the Mac** (`backend/app/security/arm_delegation.py`). The Mac follows `my_arm_authorizations`. A spoken ARM intent with a current grant for this device calls only `arm_session_delegated(workstation, authorization)`, and LOCKI says armed only when the armed session comes back through `my_sessions`. Voice disarm stays denied. The owner's grant must come from the dashboard's persistent SDK connection | Dhruv (Mac), Sid (owner grant UI) |
| 11 | Old checkout paths in `docs/airlock/DEMO.md` (`~/locked-in`) | They would point at the old repository | Now `~/locki-airlock` | Dhruv |

## Requirements both sides keep

These are acceptance requirements, whichever side of a conflict they came from:

- device-scoped views and identities
- original capture timestamps
- sanitized camera metadata
- durable queues
- fresh-baseline checks before arming
- reviewed owner policy before any lock
- dry-run locks
- owner-only arming and disarming
- person and desk-interaction observations without biometric identification in the default release

## Added on `integration/voice-cv-sid`

| What | Decision |
|---|---|
| Jetson person events | Sid's module keeps a jetson `person_in_zone` as a soft observation and opens no incident. So the Mac follows `my_device_events` for the cameras the owner lists (`LOCKEDIN_SPACETIME_REMOTE_CAMERAS=jetson-01`; none by default). A listed camera's real rows feed the same mode engine and presence warning as the Mac's camera, once each and only for the armed session. Age is judged on the server's `received_at_ms`, never on the camera's clock |
| Natural arm and disarm phrases | More of the owner's own words reach the arm and disarm intents. "I'm back" while armed asks to stand down. No phrase arms or disarms by itself |
| Sid's `43b40dd` | Added as a snapshot (investigator evidence access, the live ARM deployment record). The module source is Sid's exactly |


## Sid review — 2026-11-08

Reviewed the new repository snapshot at `2212251` without importing the original
Git history. The seven textual resolutions above preserve both lanes' intended
interfaces. Sid's transport remains separately named and tested, the M4 Python
investigator remains in place, Make targets and routes remain distinct, the server
contract is authoritative, and no robot firmware ships. This records Sid's technical
review; backend/CV ownership and M4 runtime restart remain with Dhruv.

The integration review additionally corrected concrete voice authorization issues:
whole original utterances are checked before truncation/echo handling; a single
request owns each in-flight consume; cached grant/session state is not authority;
and exact fresh server readback precedes confirmation. Maincloud accepts one SQL
statement per HTTP request, so the five readback views are queried separately with
explicit cross-checks, not described as an atomic snapshot. Owner UI consent binds
the target Mac, active app, baseline, policy, apps, duration and issuing connection.

The original PRD excludes face identification. The release defaults that legacy
path off, preserves primary person/hand-zone detection, skips stored templates and
face model construction, and removes enrollment/recognition requirements from the
default demo. No running M4 setting was changed by this source review.

Software and isolated cloud results are in
[RELEASE_ACCEPTANCE.md](airlock/RELEASE_ACCEPTANCE.md). M4 Claude must review/pull the
PR and coordinate any restart; live microphone/audio, actual Mustafa evidence and
held-out volunteer trials remain separate acceptance work.

## Pinned-main voice and delivery reconciliation

The review of upstream main `0a55fdae999abf767dcc54ed1034a6fc067db0a8` preserves
M4's adaptive turn endings, continuation joining, quiet mode, barge-in and
notification receipt states. The personality conflict keeps those new categories
alongside the integration's neutral ARM/DISARM request wording and non-biometric
demo start/end lines. A refused or unconfirmed request does not assert that an
already-armed workstation became disarmed.

Capture generations bind input queues, immutable STT worker arguments, held
segments and utterance callbacks. Stop clears held text; a stopped or replaced
capture cannot relabel a late result as a new command. The runtime independently
checks active capture identity and exact epoch for protected voice requests.
Microphone cleanup attempts close even after stop/start failure; one capture's
failure does not block output reinitialization, other captures or warning playback.
The snapshot reports failed capture state instead of claiming it is listening.

Conversation fallback admits an installed local model only after resource checks;
parsed loopback origins reject credential/host substitutions. These are source and
fixture checks, not evidence that M5 served the requested model. Optional phone
notifications distinguish queued/accepted from delivered; dispatch hardening has
separate fake-worker/provider tests. No real notification was sent by these checks.

The 1,699-pass/20-skip full backend baseline preceded final dispatch hardening;
the separate post-fix dispatch run passed 40 tests. Final acceptance remains bound
to its recorded source/log scope, without asserting a final merge commit or a
running-M4 restart. No physical, spoken, model-trace or volunteer gate is closed.

## Imported v0.3 investigator handoff

Pinned main `e38b38d610ef31b3276b672f1c834bf04cd09458` adds Mustafa's service at
`services/nemotron-investigator/` and its source-specific contract. This closes
the missing-source gap; it does not verify the running M5 revision, current API
key, database credential/grant, model response or native tracing. Preserve the
existing M4 voice runtime and let the host owner coordinate any restart.

The service's live database principal remains evidence-only: no workstation
membership/device role and no owner/admin credential substitution. Fresh server
identity and no-membership checks precede model use; approved incident IDs,
workstation/session, frozen baseline and fresh authoritative severity constrain
the report. Revoked, unavailable or changed scope withholds sensitive results.
Serving a cached live report requires a new scope check. Optional API-key scopes
never grant database authority, and an anonymous schema match is not own-token
authorization proof. The actual-token verifier must still check empty privileged
views and private-table denials on the owning host.

Service credentials can be plain or JSON; Sid's Node verifier needs private JSON
containing `token`. Operators wrap their own existing token locally when needed;
credentials and detailed live reports remain private. The contract and acceptance
manifest keep fixture software results separate from actual grant receipts and
M5/model evidence.

## Authorized main delivery and final software checks

The recorded-input source handoff is pinned at upstream
`529885c40f3bf00c55a4e38b35a3b9f022298431`. The first user-authorized application
batch reached main as `204401941a0af5c58b7e7726555812ec5a0469d8`, matching integration
application source `3b9263458c67ee1beee9e8f248cffb038d5ef28a` except the workflow.
The workflow then reached main separately as
`7328c4e8788cd9985816e77a2f506a8b88f1fac4`, using the connected GitHub capability.
The earlier OAuth scope rejection is historical; there is no current credential
blocker or need for additional user credentials. User authorization covers these
main pushes. Host restarts and actual incident-evidence grants remain separately
coordinated operations.

Final local suites passed 1,772 backend tests/20 skipped, 387 investigator-service
tests/one skipped/three live-model tests deselected, and the recorded 54 frontend
tests. Final frontend typecheck/build passed after the last copy/Send-enable edit.
The acceptance manifest binds the current reviewed source and logs; it does not
invent the next investigator/docs delivery commit. Actions run 42464179187 last
reported startup failure with zero jobs, which is distinct from successful source
or workflow delivery and has no established cause here. Pre-existing investigator
style findings remain; critical Python checks passed. Actual M4/M5 restart,
own-token grant verification, native model traces and physical trials remain open.

## M4 API client source handoff

Main `1bbbfbf8282d7430d8395a188dff2ea20776fe89` adds
`backend/app/intelligence/nemotron_service.py` and routes “what happened?” in the
existing runtime to Mustafa's API for an authoritative live
`incident:<32 lowercase hex>` when the M4 requester key is configured. Local cases
retain `AirlockAgent` and its Ollama/fallback path. This closes the earlier
missing-client source gap without adding another voice/backend consumer.

The M4 API key and M5 SpacetimeDB investigator token remain separate; neither
grants owner authority. Independent review is hardening the client and runtime's
credential/origin, report/provenance and cancellation boundaries. The prior 1,772
backend and 387 service results keep their checkpoint scope and do not certify
this new route. Actual reviewed-host activation, incident evidence grant/receipt,
own-token verification and a live model/spoken result remain unverified. The
original PRD and its physical/native-model acceptance gates are unchanged.
