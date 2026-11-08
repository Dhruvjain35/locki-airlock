<!-- Integration (locki-airlock migration): this file was added on both branches. Both texts are kept whole. Sid's server answers come first
     because his module is the authoritative contract (spacetime/CONTRACT.md); the Mac side follows. Where they differ, the module wins and
     the Mac section is the question it answered. See docs/INTEGRATION_DECISIONS.md. -->

# Mac integration contract — Sid's server answers

These answers describe the current Sid module in `spacetime/src/index.ts`.
Dhruv reported local commit `4277d99` on `feature/airlock-dhruv`; that unpushed
branch and its original questions were not available in this checkout. This
document does not claim to review or merge that commit. The complete reducer
and row contract is [spacetime/CONTRACT.md](../../spacetime/CONTRACT.md).

## Enrolled Mac subscriptions

Use the same token whose identity the owner enrolled as role `mac`:

```sql
SELECT * FROM my_sessions;
SELECT * FROM my_incidents;
SELECT * FROM my_device_events;
SELECT * FROM my_lock_requests;
```

SDK accessors are `mySessions`, `myIncidents`, `myDeviceEvents`, and
`myLockRequests`. The two observation views now include enabled enrolled Macs,
scoped to their workstation memberships. They include the workstation's
correlated Jetson evidence, not only events published by the Mac itself.
Backing tables stay private. No event/incident row schema changed, so this fix
does not require regenerating client bindings.

Lock requests were already visible to the enrolled **target Mac identity**.
Check `targetDeviceId`: being another Mac enrolled at the same workstation
does not grant that request. A live session freezes the Mac identity/device
that supplied its baseline. No request means no authorized lock intent; lack
of a request must not be treated as a successful lock.

Revoking the identity or removing/reassigning its last enabled matching Mac
device removes its observation access. An unrelated workstation and Jetson
identities cannot use these incident/event views. This change grants no
acknowledgement, resolution, disarm, protected-action, approval, agent-trace or
audit access to a Mac. Do not promote the voice adapter to reviewer to work
around missing observations.

## Exact event metadata

`metadataJson` is a serialized JSON **object**, with only these optional keys:

| Key | Requirement |
|---|---|
| `deviceKey` | Required and nonempty for `usb_added` and `usb_removed`; use a redacted local device digest. |
| `appId` | Required and nonempty for `app_launched`; normally the bundle identifier. |
| `label` | Optional short redacted display label. |
| `detail` | Optional factual redacted observation detail. |
| `captureMode` | Optional description of the actual capture method; no enum. |
| `bootId` | Optional producer boot marker; no enum. |

Every present value must be a string of at most **256 JavaScript UTF-16 code
units**. Numbers, booleans, nulls, arrays and nested objects are rejected. Both
the submitted JSON and its canonical form must fit **2048 UTF-8 bytes**. No
additional keys are accepted; `vendorId`, `productId`, `serial`, `path`,
`signed`, `timestamp`, and `status` are not metadata keys. The adapter must
redact permitted strings too; a permitted key does not make its contents safe.
Do not send recordings, device files or raw serials.

The portable JSON Schema is slightly stricter: if `deviceKey` or `appId` is
present, it must be nonempty even for another event kind. Omit irrelevant keys
instead of sending empty values. Its Unicode character counting differs from
JavaScript's UTF-16 counting; obey the server limit above as well.

```json
{"deviceKey":"sha256:REDACTED_DIGEST","label":"USB peripheral","captureMode":"ioreg snapshot poll","bootId":"boot-1"}
```

Time, sequence, confidence and provenance belong in the event envelope:
`observedAtMs` (UTC epoch milliseconds), increasing per-device `seq`,
`confidence` in `[0,1]`, `simulated`, and `schemaVersion:1`. Generated SDK
reducers use these camelCase names; the portable publisher envelope uses
`observed_at`, `schema_version`, etc., as documented in TEAMMATE_INTERFACE.md.
The observation must fall inside the committed armed interval, be no more than
120 seconds old, and no more than five seconds in the server's future. Preserve
real capture times; do not relabel a pre-arm observation to make it admissible.
Check clock synchronization if genuine post-arm observations are rejected.

`seq` is a positive safe-integer **counter**. Start at **1** after a new or
owner-reset enrollment: the server initializes its last accepted sequence to
zero, so a first event numbered zero is rejected. Persist the counter across
sessions and reconnects. Every new event must increase it, with a forward delta
of at most **1,000,000** from the server's last accepted value. That delta is
stateful; a persisted counter may grow above 1,000,000. Do not use an epoch
timestamp as the sequence. Exact retries keep the original event ID and counter.

The portable publisher rejects unknown top-level envelope keys, matching the
JSON Schema's `additionalProperties:false`. For example, `workstationId` must
not be added alongside `workstation_id`; use the documented portable names.
The reducer argument order and permitted metadata keys are unchanged.

## Full health status and component lists

`report_health` accepts exactly **`online`, `degraded`, `offline`,
`unavailable`**. Values such as `ok`, `healthy`, `error`, `starting`, and
`disconnected` are rejected; describe the condition using `detail` instead.

| Enrolled role | Allowed `component` values |
|---|---|
| `mac` | `watcher`, `usb`, `apps`, `voice` |
| `jetson` | `camera`, `jetson` |
| `service` | `model`, `gateway` |

`detail` is a string limited to 512 JavaScript UTF-16 code units before trimming.
The `capacity` component on device `database` is server-owned and cannot be
reported by an adapter. Device health is not a session or lock status enum.

```ts
await connection.reducers.reportHealth({
  workstationId, deviceId,
  component: "voice", status: "online",
  detail: "Incident subscription and local warning output available",
});
```

Report a component online only when it is actually available. A generic Mac
heartbeat refreshes `watcher`; it does not establish microphone/audio health.
USB/app observations update `usb`/`apps` respectively using their original
capture time, capped at server receipt time. Delayed evidence older than 15
seconds cannot revive online health, and an older capture does not replace a
newer component health report. Explicit `report_health` is a current liveness
assertion, so a publisher must not use it to declare stale replay healthy.
Refresh active component health every 3–5 seconds: online/degraded records
become offline after more than 15 seconds without an update, processed by the
five-second server maintenance timer. Gateway liveness never makes model
health online. Revoked/disabled publisher health is excluded from caller views;
server-owned database capacity warnings remain visible.

## End-to-end verification boundary

The regression verifier `scripts/airlock_mac_views_verify.ts` checks separate
authenticated owner/Mac/Jetson identities, same incident/event IDs, targeted
lock visibility, workstation isolation and revocation. Its observations are
explicit fixtures; it opens no camera, microphone, speaker or OS lock.

Dhruv's reported **262 ms local warning** remains his measurement, separate
from server-committed incident subscription-to-voice latency. After deployment,
reconnect using the same Mac token and test a fresh incident: old incidents
may be intentionally suppressed by the voice bridge's age/dedup rules.

---

# The Mac side of the contracts (Dhruv)

These are the messages between Dhruv's Mac agent, Areeb's Jetson publisher and Sid's SpacetimeDB module. Each has a JSON Schema and
sample messages in [`contracts/`](contracts/), so you don't need Python to use them. Tell Dhruv if a field doesn't fit your SDK.

| Contract | From | To | Schema | Sample |
|---|---|---|---|---|
| `SecurityEventV1` | Mac, Jetson | SpacetimeDB `ingest_device_event` | [schema](contracts/SecurityEventV1.schema.json) | [USB](contracts/fixtures/security_event_usb_unknown.json), [app](contracts/fixtures/security_event_app_watchlist.json), [person](contracts/fixtures/security_event_person_in_zone.json), [camera lost](contracts/fixtures/security_event_camera_unavailable.json) |
| `IncidentUpdateV1` | SpacetimeDB | Mac voice and agent, consoles | [schema](contracts/IncidentUpdateV1.schema.json) | [sample](contracts/fixtures/incident_update.json) |
| `LockRequestV1` | SpacetimeDB | Mac lock adapter | [schema](contracts/LockRequestV1.schema.json) | [sample](contracts/fixtures/lock_request.json) |
| `AgentStepV1` | Mac agent | SpacetimeDB, dashboard | [schema](contracts/AgentStepV1.schema.json) | [sample](contracts/fixtures/agent_step.json) |

## Rules for every message

- **Times** are integer milliseconds since the Unix epoch.
- **`seq`** only ever rises for a given `device_id`, including across restarts.
- **`is_fixture: true`** means sample data. Never show it as a real observation.
- **Unknown fields are refused.** Each message rejects fields the schema doesn't define.

## `SecurityEventV1` kinds

| `source` | `kind` | Sent by |
|---|---|---|
| `mac_usb` | `usb_device_added`, `usb_device_removed` | Mac |
| `mac_app` | `app_launched` | Mac |
| `mac_agent` | `monitor_health`, `lock_status` | Mac |
| `mac_gate` | `protected_tool_decision` | Mac |
| `mac_vision`, `jetson_vision` | presence: `person_in_view`, `person_approaching`, `person_in_zone`, `person_left_zone`, `zone_clear`. Faces: `owner_face_match`, `unrecognized_face`, `face_uncertain`, `no_face_visible`. Zones: `hand_near_keyboard`, `keyboard_interaction`, `laptop_interaction`, `hand_near_usb_zone`, `usb_zone_interaction`, `protected_device_interaction`. Camera: `camera_unavailable`, `camera_offline`, `camera_obstructed`, `camera_restored` | The AIRLOCK camera pipeline (`backend/app/vision/airlock`): `mac_vision` on the Mac's webcam today, `jetson_vision` on the Jetson later |
| `jetson_health` | `jetson_health`, `camera_unavailable`, `camera_offline`, `camera_obstructed`, `camera_restored` | Jetson |

Camera observations come from their own device id (`cam-...`), not the Mac's, and carry typed `VisionMetadata`: `camera` (a label such as `mac_webcam`, never a serial), `inference`, `detector`, `zone`, `started_at`, `dwell_ms`, `persons_in_view`, `capture_to_event_ms`, `frame_rate`, `detail`, `armed`, `session_id`. Nothing else is accepted, so an event cannot carry a picture, a face template, a name or a guess at intent. Face templates stay on the camera's machine.

**The camera and Sid's module.** The desk camera enrolls as its own device, with role `jetson`. Its identity is `data/airlock_vision/`, and `scripts/airlock.py camera-setup` prints the owner's `enroll_device` command. These rules come from `spacetime/src/index.ts` on `feature/airlock-sid`:

| Camera kind | Module kind |
|---|---|
| `person_in_zone` | `person_in_zone` |
| `person_left_zone`, `zone_clear` | `person_cleared` |
| `camera_offline`, `camera_unavailable`, `camera_obstructed` | `camera_offline` |
| `camera_restored` | `camera_online` |

- **Metadata.** Only `label` (the zone), `captureMode` (the inference profile) and `detail`, as strings.
- **Kept on the Mac.** Face states and zone interactions have no module kind yet, so they stay local. They still drive LOCKI's own security mode.
- **Correlation.** The module correlates an unknown USB device with a `person_in_zone` within 10 s.
- **Turning it on.** Publishing starts when `data/airlock_vision/spacetime.json` has `"enabled": true`, after the owner enrolls the camera. `camera_kinds` there overrides the map above.

## Security mode and visual state (for the voice, the dashboard and the investigator)

`backend/app/security/modes.py` decides LOCKI's mode from recorded events with deterministic rules, and no model:

| Mode | When |
|---|---|
| `NORMAL` | disarmed, or armed with nobody near the desk |
| `WATCHFUL` | armed, and someone near the desk is not matched to the owner: unrecognized, uncertain, or no face yet |
| `DEFENSIVE` | armed, and someone not matched to the owner uses the keyboard, the laptop or a protected device |
| `CRITICAL` | armed, and the Mac's watcher reports a real unknown USB device, or the module reports a correlated incident |
| `OWNER_RETURNED` | the enrolled owner's face matched and settled. The tone is normal again, but the workstation stays armed until an authenticated disarm |

Each change is published on the backend bus as `security.mode`, with the full visual state. It is also at `GET /api/security` under `visual_state` and `mode_transitions`. The visual state fields are:

- `armed_status`, `session_id`
- `person_present`, `at_desk`, `person_track_id`
- `face_identity_state` (`owner`, `unrecognized` or `uncertain`, or null when nobody is there), `identity_confidence`, `identity_detail`
- `keyboard_interaction`, `laptop_interaction`, `protected_device_interaction`, `usb_zone_interaction`
- `last_seen_timestamp`, `camera_health`
- `current_security_mode`, `previous_mode`, `mode_since`, `mode_reason`, `evidence_ids`, `updated_at`

`identity_confidence` is the share of recent frames that agreed, not a probability of identity. A face match never disarms anything and never removes a USB warning. Nothing in these modes calls anyone a hacker or an intruder.


The Mac's kinds have typed metadata that the schema checks in `contracts.py`. The Jetson's own health publisher (`jetson_health`) is free-form for now, within these limits:

- snake_case keys and plain JSON values, 4 KB at most.
- No key may carry a secret or a camera frame. Words such as `serial`, `token`, `image`, `frame`, `video` and `face` are refused. `frame_rate`, `frame_count` and `frames_dropped` are allowed because they only describe frames.

Areeb, send your metadata fields and I'll add a typed model.

## What the events mean

- An event says what was observed, never what it means. An unfamiliar USB device has `allowlist_status: "unknown"`, not "malicious".
- USB serial numbers never leave the Mac. `fingerprint` is a salted hash.
- A disarmed Mac publishes only its monitor health (`monitor_health`). USB and app observations are counted locally and not sent.

## The Mac and Sid's module (locki-w91bw on maincloud)

The Mac speaks the documented HTTP and WebSocket APIs with its own identity, because SpacetimeDB has no official Python SDK. Before it sends anything, it checks the live module's reducer signatures against its own copy (`EXPECTED_REDUCERS` in `backend/app/security/spacetime_adapter.py`). If they differ, it stops sending and reports why, while local warnings carry on.

| Mac message | Reducer it calls |
|---|---|
| USB arrival and removal, app launch | `ingest_device_event`, with kind `usb_added` or `usb_removed` and metadata `{"deviceKey": fingerprint}`, or kind `app_launched` and metadata `{"appId": bundle id}`. These are the only keys the module accepts, with string values only. Every other detail stays on the Mac |
| Gate decisions, local lock events | Kept local, because the module has no kind for them yet |
| Monitor health | `report_health`, with component `usb` or `apps` and status `online`, `offline` or `degraded`. Others stay local |
| What is connected, so the owner can arm with it | `record_baseline`, where `baseline_json` is a list of up to 256 salted fingerprints, one per device |
| Investigator steps | `record_agent_step` |
| The result of a lock request from the database | `report_lock_result` |

**It follows three views:** `my_sessions` to know whether it is armed, `my_incidents`, and `my_lock_requests`. Sid, please check these choices:

- **OS facts:** `confidence` is sent as `1.0`, and `metadata.detection` says how the event was observed.
- **Device-scoped views (the request):** please let an enrolled `mac` device read its own workstation's rows in `my_incidents`, `my_lock_requests` and `my_sessions`, or give it new views, for example `device_incidents` and `device_lock_requests`. Membership should match on `memberships.identity` with the device's role. If you name new views, add `"incidents_view"` and `"lock_view"` to `data/security/spacetime.json` on the Mac. No code change is needed.
- **Incidents for the Mac:** as the enrolled `mac` device, the Mac sees its session in `my_sessions` but nothing in `my_incidents` or `my_device_events`, while your owner view shows the incident. Can enrolled devices read their own workstation's incidents and lock requests? Without that, the Mac warns from its own detection, which takes about 260 ms, but can't follow your incident state or a camera correlation.
- **Event metadata:** your module took only `deviceKey` for USB and `appId` for apps. It refused vendor and product IDs, names and interface classes. Do you want any of those for the dashboard? Which keys are allowed?
- **Health statuses:** your module accepted `online` and refused `started`, `ok` and `healthy`. Is the full list `online`, `offline` and `degraded`? `offline` and `degraded` haven't been tried, so a wrong status isn't recorded as true.
- **Device role:** the Mac assumes `mac` in `enroll_device`. Tell me the real one.
- **Lock requests:** the Mac acts on a request unless its status is finished (`executed`, `confirmed`, `failed`, `refused`, `expired`, `cancelled`, `done`, `completed` or `dry_run`). It still checks the session, the owner's policy hash and the expiry itself.
- **Policy hash:** a lock request with an empty `owner_policy_hash` is never acted on.

**Arming:** while connected, only the owner arms, through the dashboard or `scripts/airlock.py owner-commands`. A spoken "arm" sends a fresh baseline and asks the owner to arm. A local disarm code doesn't apply to a session that SpacetimeDB armed.

## Sid: what the Mac expects from SpacetimeDB

The Mac uses the documented HTTP and WebSocket APIs, because SpacetimeDB has no official Python SDK. Reducer and table names are settings (`LOCKEDIN_SPACETIME_*`), so they can follow your module.

**It calls these reducers,** each with one argument, the message as a JSON string. Validate it on your side.

| Reducer | Argument |
|---|---|
| `ingest_device_event` | a `SecurityEventV1` |
| `record_agent_step` | an `AgentStepV1` |

If a reducer throws, the Mac treats that as final and records the reason. It retries only when the server is unreachable or returns another 5xx.

**It subscribes to these tables.** Column names must match the contract fields, and extra columns are ignored. A row that doesn't fit the contract is counted and dropped.

| Table | Contract |
|---|---|
| `incident_update` | `IncidentUpdateV1` |
| `lock_request` | `LockRequestV1` |

**Decisions needed from you:**

1. **Arming.** How does the Mac learn a session is armed, and how should it send its USB baseline? Today the owner arms locally and everything says `source: local`. When your `arm_session` exists, I'll follow your session table instead.
2. **Reading events.** The investigator should read events through your authorized view, so it also sees Jetson events. Which table or view should it use?
3. **Identities.** The Mac creates its own identity with `POST /v1/identity`. Tell me how to register it as this workstation's device.

**Check your module against the Mac** with this. It's a dry run, so it never locks the screen:

```
cd backend && .venv/bin/python scripts/airlock_spacetime_check.py --url <server> --database <name>
```

It currently calls `request_workstation_lock(session_id, action_id, expiry, reason, owner_policy_hash)` and reads a `device_event` table, as in [the test module](../../backend/tests/fixtures/spacetime_test_module/index.ts). That module is only for checking the adapter. It isn't a suggested design.

**Your screenshot shows `maincloud`.** If events go there, they leave the local network, including app names, device names and paths with the home folder shortened. USB serials never do. The PRD says "local", so please decide this with Dhruv.

## Voice-initiated arming: proposal for Sid

**Goal.** The owner says "LOCKI, secure my workstation" and the workstation arms without a click. The Mac never gets owner powers, and a voice alone never counts as authorization.

**Today.** `arm_session` is owner-only, so in the database the Mac can only answer "confirm on the dashboard". Until Sid's module has the reducers below, voice arming is **incomplete**, and the demo says so.

**Proposed module additions.** All of these are Sid's code.

1. **`grant_voice_arm({workstationId, deviceId, validForMs, sessionDurationMs, approvedAppsJson})`**
   - Owner only, called from the owner's authenticated dashboard or phone.
   - It creates one single-use grant for the enrolled Mac `deviceId`.
   - `validForMs` is at most 10 minutes; `sessionDurationMs` is 1 minute to 2 hours.
   - A new grant replaces any unused one.
   - Audit: `voice_arm_granted`.
2. **`revoke_voice_arm({workstationId})`.** Owner only.
3. **A `my_arm_grants` view.** Only the target Mac identity sees its own grant: id, expiry and the session length. It gives no other access.
4. **`arm_from_grant({workstationId, grantId, sessionId})`**
   - The sender must be the enrolled, enabled target Mac identity named in the grant.
   - The grant must be unused, unexpired and for this workstation.
   - The workstation must not already be armed.
   - The same live checks as `arm_session` apply: the freshest Mac capture is at most 30 s old, and USB health is online at most 15 s ago.
   - It arms with `simulated = false`. The baseline is the fresh capture, the policy and auto-lock consent come frozen from the workstation, and the apps come from the grant.
   - It marks the grant used. Audit: `session_armed_by_voice_grant`, naming the granting owner.
5. **Disarm stays owner-only (`disarm_session`).** A spoken "disarm" is refused on the Mac and points the owner to the dashboard.

**Why this is safe enough for a demo and for daily use:**
- The authorization is the owner's authenticated grant. The voice only picks the moment.
- A stolen Mac token can at most *arm*, which adds protection, and only once, within the grant's minutes. It can never disarm, approve or read cases.
- A used grant cannot be replayed, and everything is audited.

**What the Mac does (ours, once the reducers exist):**
- It follows `my_arm_grants`.
- On the arm intent with a valid grant, it calls `arm_from_grant`. It says "Armed" only when the session row arrives from the database.
- With no grant, it says the owner must allow voice arming first, and nothing is armed.

**Demo use.** The owner presses "Allow voice arming for 10 minutes" once on their phone before the judge arrives. "LOCKI, secure my workstation" then arms with no click.
