# LOCKI AIRLOCK project map

This document maps the original requirements to the current implementation. It
organizes the project without moving the working voice, CV or runtime code.
Read the [original PRD](PRD.md) for scope, [contracts](CONTRACTS.md) for actual wire
names, and [release acceptance](RELEASE_ACCEPTANCE.md) for proof and open gates.
It also holds the reference material moved out of the root README:
[repository layout](#repository-layout), [machines and ports](#machines-ports-and-connections),
[manual runs](#running-the-mac-by-hand), [offline behaviour](#offline-and-degraded-behaviour),
[safety rules](#safety-rules) and the full [software checks](#software-checks).

The PRD is a historical engineering plan, not a description of completed features.
Its older repository references and proposed reducer/state names do not override
the current `Dhruvjain35/locki-airlock` source and deployed contract.

Status on 2026-11-08: `main` is `a89cb7c`. On top of the `afd008e` handoff (the M4
API client, the recorded-turn investigation display and Areeb's `e1c1a7b` edge
snapshot with follow-ups, reconciled with the reviewed `29d0dde` safeguards) it adds
the command center (Start protection, Explain incident, Stop protection) and the
one-action launcher. On 2026-11-08 Dhruv's M4 produced one real correlated incident
(Jetson presence plus an unknown USB device), an owner evidence grant and a LIVE
Nemotron explanation that LOCKI spoke; the M4 now runs code identical to `a89cb7c`. The root
[README](../../README.md#verification-and-limitations) holds the verified results and
open gates; [release acceptance](RELEASE_ACCEPTANCE.md) predates this merge. Physical
accuracy trials, the move to Areeb's Mac and any real screen lock remain open.

## Product and success case

AIRLOCK is a workstation security companion. The owner explicitly arms a Mac,
reviews its peripheral/app baseline and chooses a response policy. Independent
camera observations and actual Mac USB/app events support a deterministic incident.
LOCKI warns immediately, authorized people review the case, and Nemotron explains
approved evidence through bounded tools. A screen lock is a separate, consented
OS action whose request, execution and confirmation must remain distinct.

The core demonstration is a real camera observation plus a real unknown USB event,
a reducer-created incident on two authenticated clients, an audible warning,
an actual multi-step Nemotron investigation, and authenticated recovery. A second
owned-workflow scenario proves a forbidden tool callback never executes.

Unknown peripherals, proximity and app launches do not prove malicious intent.
There is no face identification in P0, arbitrary malware prevention, control of
unrelated agents, motor movement or automatic emergency-services dispatch.

## Components, ownership and authority

| Component | Source | Owner | Responsibility and boundary |
|---|---|---|---|
| COORDINATE / GUARD | `spacetime/src/index.ts`, `spacetime/src/policy.ts` | Sid | Authoritative identities, sessions, incident severity/lifecycle, approvals, lock requests and scoped views. Models cannot replace these decisions. |
| WATCH: Mac | `backend/app/security/`, `backend/app/vision/airlock/` | Dhruv | IOKit USB, visible app launches, local camera/zone observations and transport. Local warnings continue without model inference. |
| WATCH: edge | `edge/jetson/locki_edge/`; retained `edge/jetson/airlock_camera.py` | Areeb; retained adapter by Sid | Separate enrolled publisher with capture timestamps, durable metadata outbox, server sequence synchronization and bounded presence reassertion. Explicit MobileNet-SSD/OpenCV DNN, detectNet and motion-only providers have distinct capabilities. Presence establishes neither identity nor interaction/intent; hardware acceptance remains open. |
| SPEAK / runtime | `backend/app/runtime.py`, `backend/app/voice/` | Dhruv | Existing ASR, deterministic intents, conversation and priority speech. One backend, one voice consumer and one monitor per device. |
| Retained local investigator | `backend/app/intelligence/airlock_agent.py`, `backend/app/security/tools.py` | Dhruv | In-process path for local cases, with gated tools and Ollama/fallback model selection. Its action-capable tools do not belong to the evidence-only M5 principal. |
| M4 → M5 API client | `backend/app/intelligence/nemotron_service.py`, `backend/app/runtime.py`, `backend/app/api/routes.py` | Dhruv; hardened by Sid | Question-driven route for authoritative live incident IDs when the M4 API requester key is configured; read-only record display for the console. Incoming changes must retain reviewed origin, authorization, report-binding and stale-result guards. It ran LIVE on the M4 on 2026-11-08; Areeb's Mac is not yet verified. |
| Restricted M5 investigator | `services/nemotron-investigator/` | Mustafa | Separate authenticated API with evidence-only read tools, grounding and advisory risk. No membership, approvals, arm/disarm, acknowledgement, resolution or lock authority. |
| Owner/reviewer interface | `frontend/src/airlock/`, `frontend/src/airlock-bindings/` | Sid; command center by Dhruv | Reads actual server state, obtains owner consent, presents policy/evidence and submits permitted human actions. The default command center (Protection, Investigation and History; Start protection, Explain incident, Stop protection) builds on Sid's consent policy. Human reviewers and the investigator have different permissions. |
| Managed tool boundary | `backend/app/missions/`, `backend/app/security/tool_gate.py` | Dhruv | Limits the owned Code Quest workflow; native Relay evidence and actual allowed/denied callbacks require separate proof. |
| One-action launcher | `scripts/airlock_launcher/` | Dhruv | Reuses or starts the backend and dashboard, keeps the M5 tunnel agent up and runs real readiness checks. It never arms, grants evidence, turns on the camera or microphone, or enables a real lock. |

## One incident through the system

1. **Authorize.** Owner direct ARM or a reviewed single-use delegation freezes the
   workstation, target Mac, USB baseline, active app, approved apps, duration and
   policy. Delegated ARM requires automatic locking off and a connected owner issuer.
2. **Observe.** Mac and edge publishers use their own enrolled identities. Capture
   time and server receipt time remain separate; frames/face embeddings never enter
   SpacetimeDB. Real and simulated provenance must never be mixed.
3. **Warn locally.** The existing Mac pipeline can queue a cached P0 warning without
   waiting for a network reducer or model. Local CV/security delivery modes are not
   the database's authoritative incident severity.
4. **Decide transactionally.** Unknown USB while armed opens a medium incident;
   matching person evidence within the inclusive 10-second window raises it to high.
   Matching requires the same workstation, session and simulation classification.
   Approved USB is informational; person presence alone remains a soft observation.
5. **Coordinate.** Scoped subscriptions update the Mac and dashboards. Session state,
   incident lifecycle, lock delivery and local delivery mode are separate facts,
   not one invented global state enum. The PRD's state diagram is conceptual.
6. **Investigate.** The new source route sends an authoritative live incident ID
   to Mustafa's API when the owner asks what happened and the M4 requester key is
   configured; local cases retain the in-process agent. The M5 reads only its
   owner-granted evidence. Server severity remains authoritative; model risk is
   advisory. Missing or withheld evidence must not become an invented or fixture result.
7. **Recover.** An authorized human acknowledges/reviews; the owner controls disarm.
   A model, camera, recording or unauthenticated phrase cannot grant those rights.

## Active paths versus retained alternatives

| Concern | Current source path | Retained alternative / integration gap |
|---|---|---|
| Browser voice grant | `AirlockApp` → `VoiceArmAuthorization` → `VoiceArm.tsx`; explicitly `ttlMs: 30000n` | `armConsent.ts` also retains an older 60-second builder, but the current rendered grant UI does not call it. Server ceiling is 60 seconds. |
| Voice consume | Existing runtime → deterministic full-utterance intent → `SecurityService.request_arm_by_voice` → exact delegated reducer → fresh readback | No second voice service. Stopped/replaced/held/stale capture and uploaded recordings cannot consume a grant. |
| Owner with CLI authentication only | Optional local consent window maintains the M4's persistent authenticated issuer connection | A short-lived CLI reducer connection cannot stand in for the persistent voice-grant issuer. No owner token enters voice. |
| Authoritative live incident question | Existing runtime → `NemotronService` → M4 loopback `18787` → M5 API, with `evidence_mode: "live"`, when the requester key is configured | Ran LIVE on the M4 on 2026-11-08 and was spoken. Without the key, the tunnel or a valid answer, LOCKI says Nemotron couldn't review the incident and reads the local record; no local model substitutes for a database incident. IDs that are not `incident:<32 hex>`, such as owner-panic incidents, are refused the same way. |
| Investigation record display | Recorded-turn console → M4 read-only investigation route → authenticated M5 record API | Incoming record support is being reconciled with strict live provenance, exact incident/run binding and fresh scope checks. A previous turn's result cannot stand in for the current turn. |
| Local case question | `SecurityService.agent()` builds `AirlockAgent(AirlockTools(...))`; `_agent_model()` selects raw Ollama/fallback | Retained local behavior, not an API response or evidence that Mustafa's model ran. |
| M5 service | M5 loopback `8787`, M4 SSH forward `18787`, separate API bearer key | Raw Ollama `11434` / M4 forward `11435` is a different connection and permission surface. |
| Camera reads | Enrolled `jetson` publisher sends observations | Empty `my_device_events` for the camera is expected under its publish-only role; use accepted receipts and authorized owner/Mac readback. |
| Edge publisher | Areeb's `locki_edge.runtime` with explicit provider selection, outbox and local `:8765` health/metrics/receipts | Sid's retained adapter uses `:8787` and separate state. Run only one process per camera/device; source tests do not establish on-device performance. |

## Evidence-only investigator contract

The owner grants one selected incident, an explicit linked-event subset, and
optionally its frozen `session:<sessionId>` USB baseline. The principal receives
only `my_investigator_incidents`, `my_investigator_events` and
`my_investigator_baselines`; no workstation membership is created.

The service verifies its exact server-authenticated identity and rejects enabled
memberships before inference and final reporting. Tool results stay within the
approved incident/session. Final severity comes from a fresh authorized incident;
saved reports are checked against current authorization and snapshot before display.
Failed/running/withheld live records expose only safe job status. Private local
audit files do not become a new authorization source.

See [the full contract and grant handoff](INVESTIGATOR_INTEGRATION.md).
An owner grant and a genuine LIVE M5 investigation took place on 2026-11-08 (see the
root README). Existing-identity negative tests remain required; source code and
mocked tests do not close them.
The M4 requester API key is not the M5 investigator's SpacetimeDB token, and neither
is an owner credential. The owner grants incident evidence separately.

## Work order and completion rules

1. Preserve the existing runtime and source ownership; agree contracts before shared
   edits. New repository only, no force pushes or database resets.
2. Close the owner-local incident grant and M5 own-identity read/denial proof.
3. Reconcile the pinned incoming M4 client/record changes with the reviewed safeguards, then coordinate activation of
   the reviewed M5 service and existing M4 runtime route. The source handoff exists;
   it needs actual-host receipts, not another backend or competing consumer.
4. Execute real owner-consent → microphone → delegated ARM → server state → audible
   confirmation on the actual M4, including expiry/replay/revocation negatives.
5. Run real camera/USB/two-client recovery and native-model/Relay acceptance trials.
6. Measure pass-by, standing and interaction false alerts with held-out consenting
   volunteers before tuning or adding detectors. Follow [ACCURACY_TRIALS.md](ACCURACY_TRIALS.md).
7. Publish only measured claims. Complete the [acceptance matrix](RELEASE_ACCEPTANCE.md),
   review any public source export, and keep optional features behind stable P0 work.

Passing software tests, deployment of a reducer, delivery to `main`, host restart,
an accepted device event and complete physical acceptance are different milestones.

## Repository layout

Owners follow the git history and the lane docs. Some lanes arrived as snapshot
imports, so `git blame` can show the importer rather than the original author;
[MIGRATION.md](../MIGRATION.md) records the sources.

| Path | What | Owner |
|---|---|---|
| `backend/app/security/` | Mac USB (IOKit) and app (NSWorkspace) watchers, security service, mode engine, SpacetimeDB adapter and publisher, dry-run lock adapter, dispatch, demo mode, tool gate. Sid's standalone `voice_service.py`, `announcements.py` and `mac_observer.py` are retained but not wired into the runtime | Dhruv; Sid |
| `backend/app/vision/airlock/` | The Mac's optional desk camera: person tracking and interaction zones. Legacy face code is off by default | Dhruv |
| `backend/app/voice/` | Speech queue, Kokoro and Fish Audio, Bluetooth tail fix, per-mode delivery (`delivery.py`), personality lines | Dhruv |
| `backend/app/intelligence/` | Conversation, the local investigator (`airlock_agent.py`), the investigator API client (`nemotron_service.py`), Sid's transport (`nemotron_transport.py`) | Dhruv, Sid |
| `backend/app/api/` | HTTP and WebSocket routes; `local_only.py` makes the backend answer only this Mac's own dashboard | Dhruv |
| `backend/app/` (other) | FastAPI runtime kept from LOCKED IN: API, bus, telemetry, voice input, and the robot and focus modules it still imports | LOCKED IN team; imported by Dhruv |
| `services/nemotron-investigator/` | Authenticated Nemotron API, incident-scoped read tools, evidence grounding, benchmark artifacts | Mustafa; reviewed revision by Sid |
| `frontend/src/airlock/` | The command center, consent dialogs, owner recovery, the security desk view | Dhruv, Sid |
| `frontend/src/airlock-bindings/` | Generated SpacetimeDB bindings (`make airlock` regenerates them) | Sid |
| `frontend/src/demo/`, `frontend/src/components/` | Face setup and labelled rehearsal (`#face-setup`, older alias `#demo`); the LOCKED IN focus console and voice panels (`?product=legacy`) | Dhruv |
| `spacetime/` | SpacetimeDB TypeScript module: enrollment, roles, sessions, correlation, approvals, lock requests, evidence grants, views | Sid |
| `edge/jetson/` | Areeb's `locki_edge` runtime, contract fixtures and tools; Sid's earlier camera adapter, retained separately | Areeb, Sid |
| `scripts/` | TypeScript bridges, publishers, lock adapter and Swift helper, Nemotron tool adapter, verify helpers, `airlock_dev.sh`; `edge_*.sh` Jetson deploy scripts; Mac setup | Sid, Areeb, Dhruv |
| `scripts/airlock_launcher/` | One-action launcher, M5 tunnel agent, readiness checks, [judge-day runbook](../../scripts/airlock_launcher/RUNBOOK.md), [new-Mac setup](../../scripts/airlock_launcher/NEW_MAC_SETUP.md) | Dhruv |
| `examples/` | Sandbox case for the protected-tool gate, Code Quest template | Dhruv |
| `docs/airlock/` | Contracts, runbooks and evidence; see the [index](README.md) | all |
| `docs/legacy/` | LOCKED IN robot-era docs that retained code still cites | — |

Integration choices are recorded in [INTEGRATION_DECISIONS.md](../INTEGRATION_DECISIONS.md).

## Machines, ports and connections

| Machine | Role | Listens on |
|---|---|---|
| Protected Mac: Dhruv's MacBook Air M4 (tested fallback) or Areeb's M5 Pro (final target, not yet verified) | Backend with the USB and app watchers and the local voice, the owner dashboard, an optional desk webcam, the speaker | Backend `127.0.0.1:8077`, dashboard `127.0.0.1:5174`, tunnel ends `127.0.0.1:18787` and `127.0.0.1:11435` |
| Mustafa's M5 Pro | Nemotron investigator API, and Ollama with `nemotron-3.5-bolt:e4b` | API `127.0.0.1:8787`, Ollama `127.0.0.1:11434`, both loopback only |
| NVIDIA Jetson `jetson-01` with a Logitech Brio | Areeb's `locki_edge` publisher, enrolled as role `jetson`: presence and camera health only | Health API `127.0.0.1:8765` |
| SpacetimeDB maincloud | Database `locki-w91bw` at `https://maincloud.spacetimedb.com`, the security record | HTTPS |

Speaker: the judge-day runbook expects a JBL Go 3 over Bluetooth, and the readiness
check treats only outputs whose name contains `jbl` as the external speaker. Earlier
audio tests used a JLab GO Party. Which one is current is unverified.

The protected Mac reaches the M5 over Tailscale through two loopback SSH forwards.
Keep the two connections separate:

| Connection | Protected-Mac endpoint | Used for |
|---|---|---|
| Investigator API | `127.0.0.1:18787` → M5 `127.0.0.1:8787` | Live incident questions. `LOCKI_INVESTIGATOR_URL` (default `http://127.0.0.1:18787`, loopback only) and `LOCKI_INVESTIGATOR_KEY_FILE` (default `~/.locki/investigator.key`) |
| Raw Ollama | `127.0.0.1:11435` → M5 `127.0.0.1:11434` | The retained local investigator for incidents that never reached the database (`LOCKEDIN_AIRLOCK_OLLAMA_URL`), and optionally conversation (`LOCKEDIN_LOCAL_LLM_BACKEND=ollama` with `LOCKEDIN_OLLAMA_URL`) |

The launcher's launchd agent `com.locki.airlock.m5-tunnel` keeps both forwards up in
one SSH process (batch mode, strict host-key checking), and the launcher's cold start
points both Ollama URLs at `http://127.0.0.1:11435`. By hand:

```sh
ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -L 18787:127.0.0.1:8787 -L 11435:127.0.0.1:11434 <m5-account>@<m5-tailscale-name>
```

Use names from `tailscale status`, never a fixed address in committed config. The
requester key in `~/.locki/investigator.key` is not the M5's evidence-only SpacetimeDB
credential (`NI_STDB_TOKEN_FILE`), and neither is an owner credential; the owner grants
each incident's evidence separately. Configuration alone does not prove a working
tunnel or grant. See the [investigator handoff](../../services/nemotron-investigator/INTEGRATION.md).

## Running the Mac by hand

The [launcher](../../scripts/airlock_launcher/RUNBOOK.md) is the normal way to start;
it reuses anything healthy and never starts a second copy. Without it, run one backend
at a time, and check that nothing else holds the camera or the USB monitor.

```sh
# backend on 127.0.0.1:8077: security on, offline, locks as dry runs
LOCKEDIN_SPACETIME_REMOTE_CAMERAS=jetson-01 make airlock-mac OLLAMA=http://127.0.0.1:11435
# dashboard on 127.0.0.1:5174, against the team database
VITE_SPACETIMEDB_URI=https://maincloud.spacetimedb.com VITE_SPACETIMEDB_DATABASE=locki-w91bw npm run dev --prefix frontend
```

- `make airlock-mac` reads the database and workstation from `data/security/spacetime.json`,
  which `scripts/airlock.py spacetime-setup --workstation <id>` writes; `SPACETIME_URL`
  and `SPACETIME_DB` override them. Without `LOCKEDIN_SPACETIME_REMOTE_CAMERAS`, the Mac
  ignores Jetson rows. Without the `VITE_` variables, the dashboard opens the local
  database (`http://127.0.0.1:3000`, `locki-airlock-sid`); `make airlock-cloud` sets them.
- Other options: `QUIET=1` keeps LOCKI silent (speech is recorded as text only),
  `LLM=bolt` routes conversation through Ollama (also export
  `LOCKEDIN_OLLAMA_URL=http://127.0.0.1:11435` to use the M5), and `LOCK=real` allows a
  real screen lock (read the [safety rules](#safety-rules) first).
- **Camera.** `VISION=1` (`LOCKEDIN_AIRLOCK_VISION=true`) turns the Mac's own desk
  camera on while armed or in a demo. It needs the person model:
  `cd backend && .venv/bin/python scripts/download_models.py --airlock-vision`
  (about 42 MB, sha256-pinned, and it also fetches legacy face weights). Draw the zones
  once with `cd backend && .venv/bin/python scripts/airlock_zones.py`: `2` marks the
  keyboard, `4` the USB ports, `s` saves. In the tested setup the person evidence came
  from the Jetson, not the Mac's webcam.
- **Voice.** Every security line uses one local Kokoro voice, `am_onyx` by default
  (`LOCKEDIN_SECURITY_VOICE`), offline. It needs the `local-ai` extra and the Kokoro
  files from `make setup` or `make models`. Speech plays on the current output; macOS
  switches to a Bluetooth speaker when it connects. To choose a device, use the voice
  settings (VoiceStudio) or set `LOCKEDIN_AUDIO_OUTPUT` before starting. Fish Audio is
  optional: it needs `FISH_AUDIO_API_KEY` in `.env` and is skipped while the security
  voice is on.
- **Dashboard routes.** `/` is the command center. `#face-setup` (older `#demo`)
  opens face setup and labelled rehearsal. `?product=legacy` opens the old focus console.
- **Voice ARM (advanced).** **Advanced controls → Authorize next voice ARM** issues a
  one-use grant of about 30 seconds for one enrolled Mac, with auto-lock off. The owner
  reviews the Mac, its fresh baseline and app, the approved apps and the duration, and
  keeps the console connected. The Mac consumes the grant when told "LOCKI, watch my
  setup" and confirms only after a fresh database readback. Disarming always needs the
  owner. See [M4_VOICE_ARM.md](M4_VOICE_ARM.md); for an owner authenticated only in the
  CLI, [OWNER_CONSENT.md](OWNER_CONSENT.md) keeps that credential on its Mac.
- **Active checks.** `make airlock-watch` prints real USB and app events
  (`ARGS="--launch-test-app"` opens and closes Calculator); it takes the USB-monitor lock,
  so stop the backend first. `make airlock-check` is an active integration check, not a
  preflight: it reads the USB registry, opens Calculator hidden and closes it, makes a
  real dry-run lock request, renders the warning in the local voice without playing it
  and runs the Relay and pytest gate, labelling each line REAL or NOT RUN. Coordinate it
  with whoever runs the backend. `make preflight` is the LOCKED IN offline check
  (`scripts/setup_mac.sh --check`) and fails after a software-only install; the AIRLOCK
  readiness check is `~/.locki/launcher/locki_launch.sh --check`.

**Security desk on a second Mac.** After cloning, run `npm ci --prefix frontend && make desk`
and open `http://127.0.0.1:5180/?client=desk&view=desk`. It is a static build that talks
only to SpacetimeDB, never to a Mac backend, and listens on 127.0.0.1 only; the build
refuses to serve a bundle containing a token-shaped string. The desk creates its own
identity, and the owner enrolls it with **Enroll identity → Grant human reviewer access**,
approval unchecked. It can view the workstation's incidents, evidence and investigation,
and acknowledge them. Never let the protected Mac's Vite server listen beyond
127.0.0.1: it proxies `/api` and `/ws` to the backend.

## Offline and degraded behaviour

| If this is missing | What happens |
|---|---|
| Internet | SpacetimeDB maincloud and the Tailscale link to Mustafa's M5 are gone. Only the Mac's own USB and app monitoring and LOCKI's local voice keep working. `LOCKEDIN_OFFLINE=true`, set by `make airlock-mac` and the launcher, keeps Fish Audio and model hubs from ever being contacted |
| SpacetimeDB | The Mac still warns from its own watchers (and its own camera, if on). Jetson presence reaches the Mac only through SpacetimeDB, so Jetson presence warnings, correlation and new incidents stop. The publisher retries with 0.5–15 s backoff on network errors and 5xx; a reducer refusal is final and recorded. At most 1000 events wait (the oldest are dropped), and the module refuses observations older than 120 s, so a long outage loses events |
| Mustafa's M5, the tunnel or the investigator key | Detection and warnings continue. For a database incident LOCKI says Nemotron couldn't review it and reads the local record, including the official severity; no local model substitutes. The retained local model, labelled "not Nemotron", answers only for incidents that never reached the database |
| The owner's evidence grant | The investigator's views return no rows, so there is no live investigation, and never fixture evidence instead. Local warnings are unaffected |
| The Jetson camera | Its health goes offline after 15 s without a heartbeat, and it reports `camera_offline` when it can. USB and app monitoring continue; a USB incident stays medium, because there is no person evidence to correlate. The launcher's READY needs a `jetson-01` heartbeat under 30 s old |

## Safety rules

- **Screen locks are dry runs.** A real lock needs `make airlock-mac LOCK=real`
  (`LOCKEDIN_SECURITY_LOCK_MODE=real`) and the owner's auto-lock consent in the armed
  session, agreed in advance with someone present to unlock. Start protection refuses
  while auto-lock is on, and the launcher's READY requires dry run. No real lock has
  run. Known gap: the integrated Python lock path never calls the module's `claim_lock`
  (only `scripts/airlock_lock_adapter.ts` does), so under `LOCK=real` the module would
  refuse its result reports. Close it before any real-lock trial; see
  [LOCK_ADAPTER.md](LOCK_ADAPTER.md).
- **Face identification.** Off in the backend default
  (`LOCKEDIN_AIRLOCK_FACE_IDENTIFICATION=false`) and in every make target, preserving the
  PRD's no-biometric scope. The launcher's cold start sets it to true unless
  `LOCKI_FACE_IDENTIFICATION=false`; it affects only the Mac's own optional camera,
  which the launcher leaves off. The running M4's value is unverified. The Jetson
  never publishes face data, camera events carry no images, identities or embeddings,
  and existing face templates stay unused by default.
- **Voice never authorizes.** Arming and disarming need the owner's authenticated
  action. A spoken "disarm" never disarms, uploaded recordings cannot ARM or DISARM,
  and voice ARM only consumes a one-use grant the owner issued.
- **Investigator means read-only.** It receives only the owner-approved incident,
  events and frozen baseline, under its own identity with no membership.
  `grant_reviewer(false)` still grants write rights (acknowledge, request information)
  and is not an investigator role.
- **Grants outlive a disarm.** Evidence grants have no expiry, and `disarm_session`
  does not revoke them. Stop protection revokes every open grant; otherwise the owner
  calls `revoke_incident_evidence`.
- **Fixtures stay labelled.** Simulated sessions and events are marked, correlation
  never mixes real and simulated, and the live paths refuse fixtures.
- **Private by construction.** USB serials leave the Mac only as salted hashes, and
  frames never leave the Jetson's detector process. The backend answers only its own
  Mac's dashboard (loopback Host, and a loopback Origin for state-changing requests
  and WebSockets).
- **One consumer per device.** Locks on the camera and the USB monitor stop a second
  watcher. Don't run Sid's standalone `app.security.voice_service` (a second voice path)
  or `mac_observer.py` (a second USB poller) beside `make airlock-mac`. Run one Jetson
  publisher per device: `locki_edge` or `edge/jetson/airlock_camera.py`, never both.
- **`make clean-data` deletes `data/`**, including this Mac's SpacetimeDB device token,
  so the Mac must be enrolled again afterwards.

## Software checks

Run from the repository root with Python 3.12, uv and Node 24 after the software-only
install in the [README](../../README.md#1-software-only). These are the test commands
the [CI workflow](../../.github/workflows/airlock.yml) defines; CI also runs `npm audit`.
They use fixtures and mocks, with no camera, microphone, model inference or OS action.
The vision extra supplies the locked OpenCV used to decode synthetic frames; tests that
need model weights skip when the files are absent.

```sh
npm run check --prefix spacetime && npm test --prefix spacetime        # module types, policy and transactional tests
npm test --prefix frontend && npm run build --prefix frontend          # dashboard; the build runs tsc
node frontend/node_modules/tsx/dist/cli.mjs --test scripts/*.test.ts scripts/tests/*.test.ts   # bridges and adapters
node --test scripts/airlock_owner_consent_http.test.mjs scripts/airlock_investigator_verify.test.mjs
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q backend/tests
PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s scripts -p 'airlock_nemotron_test.py'
PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s edge/jetson/tests   # mostly locki_edge; one module covers Sid's adapter
PYTHONPATH=services/nemotron-investigator/src backend/.venv/bin/python -m pytest -q -c services/nemotron-investigator/pyproject.toml services/nemotron-investigator/tests
```

Results on `a89cb7c` (a local guarded run with no real keys, not CI): backend 1929
passed, 3 skipped; investigator 428 passed, 3 live-model tests deselected; edge/jetson
241 passed, 2 skipped (run with pytest); node script tests 50/50; frontend tsc clean,
vitest 163/163, production build OK. The module tests were not part of that run.
GitHub Actions has not run them either: every recent run ended in `startup_failure`
before any job started. `make test` covers only backend pytest and the frontend build.
Physical camera, USB, audio, model, Relay and screen-lock checks need separate,
honest acceptance artifacts.
