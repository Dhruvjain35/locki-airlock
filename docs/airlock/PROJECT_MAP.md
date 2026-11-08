# LOCKI AIRLOCK project map

This document maps the original requirements to the current implementation. It
organizes the project without moving the working voice, CV or runtime code.
Read the [original PRD](PRD.md) for scope, [contracts](CONTRACTS.md) for actual wire
names, and [release acceptance](RELEASE_ACCEPTANCE.md) for proof and open gates.

The PRD is a historical engineering plan, not a description of completed features.
Its older repository references and proposed reducer/state names do not override
the current `Dhruvjain35/locki-airlock` source and deployed contract.

Source update: the pinned upstream `afd008e` handoff includes the M4 API client,
the recorded-turn investigation display, and Areeb's `e1c1a7b` edge snapshot with
follow-ups. The incoming client/runtime changes are being reconciled with the
reviewed `29d0dde` safeguards. The 1,852-pass backend checkpoint predates this merge;
source wiring does not verify activation on M4/M5/Jetson, an actual evidence grant,
a model response, physical accuracy or a spoken result.

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
| M4 → M5 API client | `backend/app/intelligence/nemotron_service.py`, `backend/app/runtime.py`, `backend/app/api/routes.py` | Dhruv | Question-driven route for authoritative live incident IDs when the M4 API requester key is configured; read-only record display for the console. Incoming changes must retain reviewed origin, authorization, report-binding and stale-result guards; actual-host proof remains open. |
| Restricted M5 investigator | `services/nemotron-investigator/` | Mustafa | Separate authenticated API with evidence-only read tools, grounding and advisory risk. No membership, approvals, arm/disarm, acknowledgement, resolution or lock authority. |
| Owner/reviewer interface | `frontend/src/airlock/`, `frontend/src/airlock-bindings/` | Sid | Reads actual server state, obtains owner consent, presents policy/evidence and submits permitted human actions. Human reviewers and the investigator have different permissions. |
| Managed tool boundary | `backend/app/missions/`, `backend/app/security/tool_gate.py` | Dhruv | Limits the owned Code Quest workflow; native Relay evidence and actual allowed/denied callbacks require separate proof. |

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
| Authoritative live incident question | Existing runtime → `NemotronService` → M4 loopback `18787` → M5 API, with `evidence_mode: "live"`, when the requester key is configured | Source client exists; independent credential/origin/report/cancellation review and actual M4/M5 adoption remain separate gates. |
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
Actual owner grant receipt, existing-identity negative tests and one genuine M5
model investigation remain required; source code and mocked tests do not close them.
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
