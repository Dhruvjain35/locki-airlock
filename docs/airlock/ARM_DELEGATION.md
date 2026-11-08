# Owner-authorized one-use ARM contract

Status: deployed to maincloud `locki-w91bw` on 2026-11-08 and verified with
independent identities on an isolated cloud test workstation. The existing M4 voice/CV adapter is now integrated and software-verified in the
new repository; **the physical spoken delegation demonstration remains unverified**. No existing
owner-only reducer is relaxed. This is a demo integration contract, not a production
readiness or speaker-authentication claim.

## Authority and owner session

The owner reviews an enrolled Mac's fresh baseline and authorizes exactly one future
session. `authorize_arm` binds the actual authenticated `ctx.sender` and
`ctx.connectionId`, not a caller-supplied login/session string. Keep that owner SDK
WebSocket connected. The grant expires within 60 seconds and is revoked when the
server observes that issuing connection disconnect. A different connection with the
same owner identity disconnecting does not revoke it. Silent network loss is bounded
by expiry, not claimed to be detected instantly. Authentication proves possession of
the existing owner credential; it does not prove a new password/MFA prompt or that
the person speaking is the owner.

The local host test confirmed an HTTP-issued grant was immediately revoked when its
transient connection ended. Therefore use the persistent authenticated owner SDK
connection, **not `spacetime call` or a one-shot HTTP request**, to issue this grant.
The ordinary camera enrollment command can continue using the owner's CLI.

## Additive schema and exact reducers

New private table `arm_authorizations` and caller-scoped `my_arm_authorizations`
view. SQL field names use snake_case; generated TypeScript uses camelCase. All
millisecond values below are `u64` / SDK `bigint`; other arguments are strings.

```ts
authorize_arm({
  workstationId, authorizationId, deviceId, sessionId,
  baselineJson, activeAppId, approvedAppsJson, expectedOwnerPolicyHash,
  durationMs, ttlMs,
})
arm_session_delegated({ workstationId, authorizationId })
revoke_arm_authorization({ workstationId, authorizationId })
```

Generated SDK names: `authorizeArm`, `armSessionDelegated`,
`revokeArmAuthorization`. Existing `arm_session` and `disarm_session` signatures and
owner checks stay unchanged. The lifecycle export `arm_owner_disconnected` is a
server disconnect hook, not a client command.

Grant rows contain `id`, `workstationId`, `owner:Identity`, `ownerConnectionId`,
`deviceId`, `delegate:Identity`, `sessionId`, canonical `baselineJson`, `activeAppId`,
canonical `approvedAppsJson`, `durationMs`, `ownerPolicyHash`, `issuedAtMs`,
`expiresAtMs`, `status`, and `consumedAtMs`. Status is `authorized`, `consumed`,
`revoked`, or `expired`. Maintenance records expiry; callers must also compare
`expiresAtMs` because status propagation can lag. Consumption always checks server
time. Authorization IDs cannot be reused; retained rows are tombstones as well as
audit evidence. **Grant tombstones currently have no retention bound**; production
retention and issuance limits need a separate design before sustained deployment.

Only the workstation owner can issue or explicitly revoke. Issuance requires:

- Target device and membership enabled, role exactly `mac`; delegate identity comes
  from its enrollment, never a voice-supplied identity.
- Workstation disarmed, requested session ID unused, and current auto-lock disabled.
- `ttlMs` 1,000–60,000 inclusive; exact armed duration 60,000–3,600,000 inclusive.
- Explicit reviewed baseline inventory, active app and current owner policy hash
  matching server state. Approved apps are supplied only by the owner.
- Fresh target-Mac capture and online USB health under the existing arm rules.

The Mac can consume only its own exact workstation/device-identity authorization.
It supplies **no session, duration, policy, baseline, app or simulation override**.
The server rechecks enrollment, membership, authorizing owner, expiry, current policy,
fresh target capture and unchanged reviewed inventory/app. The session is always
`simulated=false`, auto-lock disabled, and uses the granted duration/apps/session ID.
`armedBy` remains the actual Mac caller; the authorization and audit retain owner
provenance. Freeze the target Mac's capture even if another Mac has a newer capture.
Success atomically consumes the grant and arms; errors roll back both. A retry after
success is denied; query the bound session to distinguish a lost receipt from failure.

Only one pending intent per workstation is allowed: new issuance revokes the old
intent. Owner direct ARM, owner DISARM (including while already disarmed), policy
change, membership revocation, existing-device re-enrollment, explicit withdrawal,
or issuing owner disconnect invalidate pending intents. Broad workstation invalidation
on a membership/enrollment change is intentionally conservative. New camera enrollment
does not replace any other device. No owner/reviewer/service privilege is granted.

The view exposes grants only to the owning identity or the enrolled target Mac's
identity. Camera, service, reviewer, unrelated Mac and outsider identities do not
receive them. Authentication binds identities: two processes deliberately sharing a
Mac token are the same principal and cannot be distinguished as separate devices.

## Voice integration handoff

1. On the owner's persistent dashboard connection, review the current target Mac
   capture, approved app set, duration and auto-lock-off policy. Issue one grant with
   fresh unique authorization/session IDs. Do not issue a blanket or recurring grant.
2. The enrolled Mac subscribes to `my_arm_authorizations`. A voice ARM intent may
   trigger only `armSessionDelegated({workstationId, authorizationId})` for a current
   matching grant. Transcript/model output must never choose a reducer or its scope.
3. Announce armed only after a successful reducer result and committed session
   readback. For expired/revoked/missing intent, request new owner authorization.
4. Voice DISARM stays denied. It may offer navigation to an authenticated owner
   control; it must not hold, forward or impersonate owner credentials.

The deterministic voice adapter and owner consent controls are being integrated in
this checkout. Dhruv's unpublished implementation must integrate the dedicated
callback after conflict review. No claim of a completed microphone-to-ARM demo is
made by the server tests. Browser identity is distinct from the M4 CLI identity;
a browser profile name does not authenticate the owner.

## Acceptance evidence and rollout

37 new datastore cases cover success, wrong roles/identities/workstations, null owner
connection, TTL/duration limits, reviewed-state races, stale/refreshed capture, target
capture selection, expiry boundary, replay/ID reuse, connection-specific disconnect,
all revocation paths, owner-only original reducers, scoped reads, and zero lock requests.
The existing 74 datastore regressions also pass.

An actual isolated local SpacetimeDB run (`locki-arm-delegation-test`) verified six
groups: target-scoped read and caller denial; atomic fixed ARM with no locks; replay
and Mac DISARM denial; issuing WebSocket disconnect revocation; unrelated owner-tab
disconnect isolation; transient HTTP issuance revocation. Its baseline is explicitly
fixture data in a test-only database, not a real sensor observation or EDGE-06 proof.
Test sessions were disarmed. Local evidence: `data/airlock-arm-integration/report.json`.

Maincloud deployment completed with `--delete-data=never`, preserving existing
Mac, Areeb camera and `jetson-01` enrollments and memberships. All three ARM reducers
and the caller-scoped grant view are live. Current combined ARM/investigator module:
`0x169d2dbe86daff56008eb83c638ef8c32c8c346296d8a24a157acc7aab62aa29`.

Eleven cloud runtime checks passed using independent fixture identities and a unique
`arm-proof-*` workstation. They cover exact session parameters, wrong callers, replay,
owner-only disarm, actual expiry, policy/app/enrollment changes, owner connection
lifecycle, and zero lock requests. Test station was disarmed afterward. See
[evidence/voice-arm-verification.json](evidence/voice-arm-verification.json).
The deployment itself did not arm `dhruv-m4-air`. A later direct owner ARM was
independently read back while this integration was reviewed, with auto-lock off.
This is distinct from the single-use delegated voice flow.

Current integration adds a 30-second reviewed owner control, exact target/socket
consent, fresh authenticated Mac selection, one fixed delegated reducer, and five
separate fresh scoped SQL reads before spoken confirmation. The reads are not
claimed to be an atomic snapshot; authorization remains atomic inside the reducer.
See [M4_VOICE_ARM.md](M4_VOICE_ARM.md) and [OWNER_CONSENT.md](OWNER_CONSENT.md).

A current-cloud fixture exercised private fixture CLI loading, local owner consent
HTTP/SDK, the production M4 service method/transport, exact committed readback and
replay denial. Its unique test workstation was disarmed with zero lock requests.
[evidence/m4-integrated-arm-verification.json](evidence/m4-integrated-arm-verification.json)
contains the sanitized fixture result. It used no microphone, camera, model or
audio playback and did not use the actual M4 owner credential.

Remaining demo gates: M4 Claude reviews/pulls the integration, verifies the existing
M4 owner credential configuration locally, coordinates its controlled restart on
port 8077, and runs a real spoken request with audible committed-state confirmation.
Do not start a second voice service or USB watcher. No owner token is transferred
to the voice process or another machine.

Runtime behavior references: [SpacetimeDB lifecycle reducers](https://spacetimedb.com/docs/functions/reducers/lifecycle/)
and [TypeScript client connections](https://spacetimedb.com/docs/clients/typescript/).
