# Read-only Nemotron investigator integration

## Current integration target (2026-11-08)

The team integration repository is now `Dhruvjain35/locki-airlock`; the inspected
source includes the reviewed M4 client checkpoint `29d0dde` and the incoming
pinned upstream `afd008e` client/recorded-turn handoff. The incoming changes are
being reconciled with the reviewed credential, provenance and cancellation safeguards.
Coordination and the exact missing artifacts are tracked in
[issue #2](https://github.com/Dhruvjain35/locki-airlock/issues/2). Do not post new
integration work to the retired `Dhruvjain35/locked-in` PR.

The snapshot's `backend/app/intelligence/airlock_agent.py` is Dhruv's Python
investigator attached to `SecurityService` and `AirlockTools`. It reads local
session/evidence state, can record local review/lock proposals and run the managed
sandbox, and records steps through the service callback. It is **not** Mustafa's
separately authenticated, evidence-only SpacetimeDB client. Its report uses an
`assessment` enum rather than a risk-severity field. Do not silently convert that
enum to server severity or grant this service's other tools to the read-only
principal.

The M4 now also has `backend/app/intelligence/nemotron_service.py`. For a
“what happened?” request about an authoritative live `incident:<32 lowercase hex>`,
the existing runtime selects this HTTP client when its requester key is configured.
It submits `evidence_mode: "live"` to the M4's loopback API tunnel on `18787` and
polls the service's voice view; local cases retain the in-process investigator.
The route is present in source, not proof that either running host has adopted it.
The incoming source also supplies `GET /api/security/investigation/{id}` on the M4
and a recorded-turn console view of the headline, grounded facts/evidence IDs,
tools and latency. Its merge must retain fresh live authorization, exact run/incident
binding and per-turn isolation; an old answered run cannot describe a new failed turn.

`LOCKI_INVESTIGATOR_KEY_FILE` holds the M4 **API requester key**; it is not
`NI_STDB_TOKEN_FILE`, the M5's evidence-only database credential, and neither is
the owner credential used to grant evidence. `LOCKI_INVESTIGATOR_URL` selects the
API tunnel. The incoming client/record changes are being reconciled with the reviewed
credential/origin validation, result-to-incident binding and stale or cancelled
result handling. A failed or unavailable
API review must not be credited as Nemotron output or silently replaced by fixtures.

`backend/scripts/airlock_investigate.py` starts a local runtime, arms a local test
session, opens Calculator and may run a real model. It is **not** the read-only
verifier and was not run for this reconciliation. No second investigator consumer
was started. The snapshot's `docs/airlock/INTEGRATION.md` §5 describes Dhruv's camera
measurements. Mustafa's actual host contract is now included at
[`services/nemotron-investigator/INTEGRATION.md`](../../services/nemotron-investigator/INTEGRATION.md), v0.3.

Fresh software verification: the read-only verifier passed 35/35,
the report reconciler 24/24, and investigator transactional authorization 38/38.
Fixture transports verify wrong
identity, scope, provenance, unsafe timestamps, broader/private access and
mid-read revocation failures; report tests preserve server high versus advisory
medium (and the inverse), and reject invented/cross-session citations. These tests
do not establish that Mustafa has been granted access or that his model ran.

The safe host activation remains explicit: use the three views below through his
existing credential, expose only returned approved evidence as read tools, retain
model/Relay logs locally, refresh authorization before rendering, and reconcile
the final advisory result with server severity. Keep action/sandbox/review tools
out of this principal. The actual host source is now present; no second investigator
consumer, model process or service was started during integration.

The reviewed M4 client checkpoint passed 1,852 backend tests/20 skipped and 79
focused client/runtime tests. The earlier 1,772-pass backend run and 387-pass
service run (one skipped/three live-model tests deselected) retain their original
source scope. None certify the incoming `afd008e` merge or current M4/M5 operation.
Fresh merge checks and manifest binding remain separate, while actual
token/grant/model/spoken acceptance stays pending.

The investigator extension is deployed to maincloud `locki-w91bw`. Source is on
`integration/sid-acceptance` in the new repository. It was first deployed separately, then preserved in the
combined ARM/investigator release. Current module hash:
`0x169d2dbe86daff56008eb83c638ef8c32c8c346296d8a24a157acc7aab62aa29`.
No database reset, existing identity replacement, camera enrollment change or
private-table publication was performed.

Target investigator: `c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883`.
Assigned workstation: `dhruv-m4-air`. Its owner remains
`c200bf80129f645f49c762279a175c3c3e84cf56af437c06191a237ac3eac422`.
**The target identity has not yet been granted access or tested with its credential.**
The owner credential remains on Dhruv's M4; the investigator credential stays with Mustafa.

Mustafa's v0.3 §5 and scenario-B fixture are now available in the imported source.
The approved event names and string-array baseline projection match the contract
below. The integrated host additionally validates its server-authenticated principal,
rejects every enabled membership, filters tools to this incident/session's approved
evidence, refreshes server severity before final reporting, and withholds stale or
revoked cached reports. These are software changes; they do not prove that the running
M5 has adopted them. His stored real-model benchmark artifacts were not rerun here.

## Minimum authorization

Do not enroll this identity as `service`, `reviewer`, `mac` or owner.
`grant_reviewer(..., false)` still permits incident acknowledgement, information
requests and workstation refresh writes; it does not satisfy read-only access.
Existing service
permissions include action proposals and agent-step writes. Instead, a new private
`incident_evidence_grants` table provides explicit, revocable read scope:

```ts
grant_incident_evidence({
  workstationId: string, identityHex: string, incidentId: string,
  eventIdsJson: string, includeBaseline: boolean
})
revoke_incident_evidence({
  workstationId: string, identityHex: string, incidentId: string
})
```

Both are owner-only. Generated SDK names are `grantIncidentEvidence` and
`revokeIncidentEvidence`. The exact event-ID list must be a subset of the incident's
linked events in the same workstation/session/provenance. Regrant replaces scope;
it does not union old and new permissions. `includeBaseline` opts into only the
immutable `session:<sessionId>` baseline, never `latest:<deviceId>`.

No membership/device row is created. An active investigator grant cannot coexist
with an enabled membership anywhere or a grant for another workstation. While grants
are enabled, role enrollment, reviewer assignment and self-registration as an owner
are denied for that identity. Revoke grants before intentionally changing its role.
The investigator cannot arm/disarm, change policy, publish observations/health, create
approvals, request/claim/report locks, propose/consume actions, or record agent steps.
Keep investigator/Relay run logs local; this identity is read-only in SpacetimeDB.

There is **no TTL promise** for this explicit/revocable grant. The owner revokes it
after the investigation. Revocation withdraws future reads, not evidence already
retrieved. Owner/session/incident generation anchors and approved event/baseline
fingerprints prevent old grants from exposing replacement content after ID reuse or
retention cleanup. Newly correlated events require a new owner approval. Grant records
are retained; a bounded retention policy remains future production work.

## Caller-scoped read schema

Subscribe or SQL-query only these views using the investigator's existing token:

```sql
SELECT * FROM my_investigator_incidents;
SELECT * FROM my_investigator_events;
SELECT * FROM my_investigator_baselines;
```

Generated SDK names: `myInvestigatorIncidents`, `myInvestigatorEvents`,
`myInvestigatorBaselines`. SQL fields are snake_case; SDK fields below are camelCase.
`u64` values are SDK `bigint`; preserve decimal strings at JSON boundaries and never
rewrite observation time as publication/receipt time.

| View | Fields |
|---|---|
| Incidents | `id`, `workstationId`, `sessionId`, `severity`, `state`, `createdAtMs:u64`, `updatedAtMs:u64`, `simulated:bool`, `approvedEventIdsJson:string`, `evidenceApprovedAtMs:u64`, `evidenceComplete:bool` |
| Events | `id`, `workstationId`, `sessionId`, `deviceId`, `source`, `kind`, `observedAtMs:u64`, `receivedAtMs:u64`, `seq:u64`, `confidence:f64`, `metadataJson:string`, `simulated:bool`, `schemaVersion:u32`, `classification` |
| Baselines | `id`, `workstationId`, `deviceId`, `sessionId`, `observedAtMs:u64`, `receivedAtMs:u64`, `baselineJson:string`, `activeAppId:string` |

All unannotated fields are strings. `baselineJson` is a canonical JSON array of
redacted approved USB device keys, not an array of device objects. The investigator
baseline projection intentionally omits publisher identity, cameraStatus and healthJson.
Incident projection omits owner/reviewer notes, actor identities and mutable summary
text. `evidenceComplete=false` means current incident evidence is not fully available
under the approved scope; missing evidence must not be invented or fetched elsewhere.

The exact event kinds are `usb_added`, `usb_removed`, `app_launched`, `person_in_zone`,
`person_cleared`, `camera_offline`, `camera_online`, `heartbeat`. Sources are `mac`,
`jetson`, `service`, or explicitly simulated `fixture`. There is no live
`usb_inserted`, `person_detected`, or device-object-baseline contract. Metadata keys
remain `deviceKey`, `appId`, `label`, `detail`, `captureMode`, `bootId`: bounded strings,
not instructions, raw files, face images or embeddings. See [CONTRACTS.md](CONTRACTS.md).

The original broad `my_*` views remain empty for this evidence-only principal,
including approvals, lock requests, sessions, audit, devices and memberships. Backing
tables remain private. Cloud collaborator/admin privileges are a separate layer that
can bypass private-table access controls: the actual-token verifier must pass, not
just a module-role test. Do not give the investigator administrative cloud access.

## Deterministic severity versus model assessment

SpacetimeDB's `severity` is authoritative (`medium|high`). Nemotron's advisory result
uses separate `model_assessment.severity` (`low|medium|high`) and a rationale/citations.
The report's top-level `severity` and `incident_severity` always copy the server.
`authoritative_incident_updated_at` preserves the server timestamp. A model `medium`
assessment never changes a server `high` incident, session, lock policy or database row.

`scripts/airlock_investigator_report.mjs` exports:

```js
reconcileInvestigatorReport({
  incidentId, incidents, events,
  modelAssessment: { severity, rationale, evidenceIds }
})
```

Supply a fresh coherent authorized incident snapshot and only that incident's returned
approved events. The helper rejects invented/unreturned IDs, wrong workstation/session,
provenance mismatches and model fields attempting to override canonical data. It is
an independent report helper. Mustafa's integrated host separately refreshes authorized
evidence before final reporting and display, binds stored live reports to the exact
approved snapshot, and withholds output after revocation or scope/severity changes.
A pure helper cannot detect stale cached authorization.

The correlation test reproduces unknown USB plus same-session physical evidence within
10 seconds: server `high` with model `medium` remains `high`. This establishes the
integration rule, not physical accuracy of the available scenario-B fixture or a
current live severity readback.

## Owner-local grant and investigator verification

The exact private incident selection and owner-local command are in issue #2.
The template below requires the owner to select a genuine incident and its linked
event subset. Replace both approved-ID placeholders before running it through the
M4's existing authenticated CLI. Sid and Mustafa do not handle the owner credential:

```bash
spacetime call --no-config \
  --server https://maincloud.spacetimedb.com \
  locki-w91bw grant_incident_evidence \
  '"dhruv-m4-air"' \
  '"c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883"' \
  '"OWNER_APPROVED_INCIDENT_ID"' \
  '"[\"OWNER_APPROVED_EVENT_ID\"]"' \
  true
```

This command is prepared, **not executed by Sid**. No owner token transfer is needed.
Mustafa then runs the read-only verifier on his own host:

```bash
node scripts/airlock_investigator_verify.mjs \
  --server https://maincloud.spacetimedb.com --database locki-w91bw \
  --workstation dhruv-m4-air \
  --outside-workstation KNOWN_OTHER_WORKSTATION_ID \
  --expected-identity c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883 \
  --token-file /path/to/existing-private-investigator-token.json \
  --incident OWNER_APPROVED_INCIDENT_ID \
  --output investigator-live-verification.json
```

The existing token JSON must be an operator-owned, mode-0600 regular file containing
`token`. The verifier never creates credentials, calls reducers or prints the token.
Use an independently verified existing workstation for the negative check. The
three investigator views must successfully return zero rows for that workstation;
network/HTTP errors fail verification. The report records whether this check ran
and does not claim that a restricted identity established the other station's existence.
It also checks the server-authenticated identity, exact scoped evidence and timestamps,
empty privileged views, nine private-table SQL denials, and unchanged read scope at
the end. Share its non-secret report, not credentials. Revoke after the run:

```bash
spacetime call --no-config --server https://maincloud.spacetimedb.com \
  locki-w91bw revoke_incident_evidence \
  '"dhruv-m4-air"' \
  '"c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883"' \
  '"OWNER_APPROVED_INCIDENT_ID"'
```

## Verification boundaries

38 new authorization tests and 24 severity/report tests pass; the read-only verifier
has 35 tests. Actual local and cloud tests used separate test identities/stations and
verified permitted reads, private SQL denial, cross-workstation denial, no mutation
authority, severity separation, scope replacement and revocation. Test stations were
disarmed. Fixture input in these contract tests is not actual sensor/model proof.
The combined development branch also passes its existing 111 datastore tests.

Earlier authorized readback checked a genuine stored USB case's original capture
and receipt timestamps, non-simulated classification and matching frozen baseline.
The exact private scope is recorded in issue #2 and local ignored acceptance artifacts.
This is stored-value verification, not proof that Mustafa has read it or independent
proof of physical capture. No owner/deployment credential was used for the current
investigator handoff or anonymous deployed-schema comparison.
See [verification manifest](evidence/investigator-verification.json).

Still required: Dhruv's actual scope grant, Mustafa's actual-token verifier report,
activation of the reviewed host changes, and a real-model run over approved live
evidence. No reset or replacement credential is necessary.
