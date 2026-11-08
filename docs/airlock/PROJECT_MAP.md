# LOCKI AIRLOCK project map

This document maps the original requirements to the current implementation. It
organizes the project without moving the working voice, CV or runtime code.
Read the [original PRD](PRD.md) for scope, [contracts](CONTRACTS.md) for actual wire
names, and [release acceptance](RELEASE_ACCEPTANCE.md) for proof and open gates.

The PRD is a historical engineering plan, not a description of completed features.
Its older repository references and proposed reducer/state names do not override
the current `Dhruvjain35/locki-airlock` source and deployed contract.

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
| WATCH: edge | `edge/jetson/` | Areeb; current fallback adapter by Sid | Separate enrolled publisher, original capture time, sequence and sanitized metadata. Current checked-in adapter establishes presence, not identity or intent. Areeb's newer runtime needs its own integration record. |
| SPEAK / runtime | `backend/app/runtime.py`, `backend/app/voice/` | Dhruv | Existing ASR, deterministic intents, conversation and priority speech. One backend, one voice consumer and one monitor per device. |
| Existing M4 investigator | `backend/app/intelligence/airlock_agent.py`, `backend/app/security/tools.py` | Dhruv | Current in-process investigation path invoked by the M4 service, with gated tools and Ollama/fallback model selection. |
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
6. **Investigate.** Tools expose only the permitted evidence. Server severity is
   authoritative; model risk is a separately labelled assessment. A missing source
   is uncertainty, not permission to invent data or silently use fixtures.
7. **Recover.** An authorized human acknowledges/reviews; the owner controls disarm.
   A model, camera, recording or unauthenticated phrase cannot grant those rights.

## Active paths versus retained alternatives

| Concern | Current source path | Retained alternative / integration gap |
|---|---|---|
| Browser voice grant | `AirlockApp` → `VoiceArmAuthorization` → `VoiceArm.tsx`; explicitly `ttlMs: 30000n` | `armConsent.ts` also retains an older 60-second builder, but the current rendered grant UI does not call it. Server ceiling is 60 seconds. |
| Voice consume | Existing runtime → deterministic full-utterance intent → `SecurityService.request_arm_by_voice` → exact delegated reducer → fresh readback | No second voice service. Stopped/replaced/held/stale capture and uploaded recordings cannot consume a grant. |
| Owner with CLI authentication only | Optional local consent window maintains the M4's persistent authenticated issuer connection | A short-lived CLI reducer connection cannot stand in for the persistent voice-grant issuer. No owner token enters voice. |
| M4 investigation today | `SecurityService.agent()` builds `AirlockAgent(AirlockTools(...))`; `_agent_model()` selects raw Ollama/fallback | Mustafa API source exists, but no M4 backend client for `18787` is present. Its actual activation and one-consumer handoff remain open. |
| M5 service | M5 loopback `8787`, M4 SSH forward `18787`, separate API bearer key | Raw Ollama `11434` / M4 forward `11435` is a different connection and permission surface. |
| Camera reads | Enrolled `jetson` publisher sends observations | Empty `my_device_events` for the camera is expected under its publish-only role; use accepted receipts and authorized owner/Mac readback. |

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

## Work order and completion rules

1. Preserve the existing runtime and source ownership; agree contracts before shared
   edits. New repository only, no force pushes or database resets.
2. Close the owner-local incident grant and M5 own-identity read/denial proof.
3. Coordinate activation of the reviewed M5 service and its client handoff in the
   existing M4 runtime. Run one investigation consumer, not competing backends.
4. Execute real owner-consent → microphone → delegated ARM → server state → audible
   confirmation on the actual M4, including expiry/replay/revocation negatives.
5. Run real camera/USB/two-client recovery and native-model/Relay acceptance trials.
6. Measure pass-by, standing and interaction false alerts with held-out consenting
   volunteers before tuning or adding detectors. Follow [ACCURACY_TRIALS.md](ACCURACY_TRIALS.md).
7. Publish only measured claims. Complete the [acceptance matrix](RELEASE_ACCEPTANCE.md),
   review any public source export, and keep optional features behind stable P0 work.

Passing software tests, deployment of a reducer, delivery to `main`, host restart,
an accepted device event and complete physical acceptance are different milestones.
