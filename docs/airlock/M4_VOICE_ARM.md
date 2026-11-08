# Existing M4 voice ARM integration

The existing M4 backend on **port 8077** now consumes an owner-authorized ARM
grant through its existing `SecurityService` and `SpacetimeTransport`. Keep its
current ASR, Claude conversation, TTS, camera and USB watcher. Do not launch Sid's
standalone voice service, another backend, or another USB watcher. The M4 Claude
operator owns the controlled restart; no remote restart or physical demo is
implied by the verification below.

The live contract is maincloud `https://maincloud.spacetimedb.com`, database
`locki-w91bw`. See [ARM_DELEGATION.md](ARM_DELEGATION.md) for server authority and
revocation rules. Existing backend configuration uses `LOCKEDIN_SPACETIME_URL`,
`LOCKEDIN_SPACETIME_DATABASE`, `LOCKEDIN_SPACETIME_WORKSTATION`, or its existing
`data/security/spacetime.json`. Its configured workstation and locally enrolled
Mac device must agree with the owner's selected target. Keep the Mac's own
credential in its existing private file; never give the voice process the owner's
credential or create a replacement identity to make a failed request work.

If the owner is authenticated only in the M4 CLI, use the optional
[existing-owner consent window](OWNER_CONSENT.md). It reads that existing private
credential **in place**, verifies the exact owner on its persistent SDK connection,
and exposes only reviewed one-use consent to the local page. Neither the browser
nor the voice backend receives the general owner credential. M4 Claude must verify
the actual local CLI configuration path, token key and authenticated SDK owner;
do not assume another Mac's paths or CLI layout. This optional window is a consent
process, not a replacement backend, voice service or watcher, and does not change
the existing port-8077 restart ownership.

## Owner review, then speech

1. Connect the dashboard as the actual workstation owner. A reviewer or a new
   anonymous browser identity cannot authorize this action.
   For a CLI-only owner, the optional consent window supplies the same explicit
   review and 30-second one-use grant workflow through its owner SDK connection.
2. When the owner intends to start a new session, use **Authorize next voice ARM**.
   The workstation must be disarmed with automatic screen locking off. Select the
   enrolled Mac and review its fresh USB inventory, active app, approved apps,
   policy and session duration. Check the explicit consent box.
3. **Authorize once · 30 seconds** creates a single-use grant; it does not arm.
   Keep this owner tab and its SDK connection open. The UI uses a **30-second**
   grant lifetime; the server permits at most **60 seconds**. The reviewed armed
   duration is separate: the UI offers 1 minute, 15 minutes or 1 hour, within the
   delegated server limit of 1 minute–1 hour.
4. Say **“LOCKI, watch my setup.”** The configured Mac selects exactly one matching
   fresh grant and calls only
   `arm_session_delegated(workstationId, authorizationId)`.
5. Treat an armed response as confirmed only after the matching committed session
   is visible in the owner dashboard. A missing, expired, revoked or already-used
   grant needs a new explicit owner review; voice never authorizes itself.

Do not disarm an existing owner-selected session just to exercise this flow. A
currently armed session, including an owner-direct session longer than one hour,
is not a pending voice grant. Verification uses a separate synthetic workstation;
the owner controls any transition of the actual workstation.

## Deterministic intent and confirmation

The existing runtime classifies the **complete original utterance**, before its
300-character display limit or echo removal. Examples include “watch my setup,”
“please monitor my desk,” “start monitoring my computer,” “enable setup
monitoring,” and “arm my workstation,” with supported LOCKI/politeness prefixes.
Negation, quotations, conditional instructions, discussion and extra action
arguments do not become ARM commands. The runtime's disabled-security exclusion
still applies, and commands captured during speech playback cannot arm. Claude
does not classify or authorize this security action.

“Disarm,” “stop watching my setup,” and “stand down” request authenticated owner
action only. They do not invoke a disarm reducer. A spoken phrase is not proof of
the speaker's identity; the owner's short-lived grant supplies the authority.

Before consuming a grant, the production Python transport reads five fixed,
caller-scoped views directly from the server: memberships, devices, grants,
sessions, then workstations. It verifies the enabled Mac principal, exact device,
owner, grant, policy and timing. After the confirmed reducer call, it reads them
again and verifies the consumed grant, exact real active session, caller,
baseline, apps, policy, duration, timestamps and auto-lock-off state.

These are **five separate fresh SQL requests**, not an atomic multi-view snapshot:
the HTTP API accepts one SQL statement per request. The reducer performs atomic
authorization; final readback validates the cross-view facts and active-session
pointer. No cached grant, cached local session or reducer success alone produces
an armed confirmation. Explicit reducer denial stays failure. A lost transport
receipt may be resolved by exact committed readback within that same attempt.

The service permits one pending attempt. Repeated cancellation retains ownership
until an already-running HTTP worker finishes, preventing overlapping requests or
late success speech. Subscription updates for the delegated session cannot speak
armed ahead of verification. Stop/interrupt changes to the existing speaker epoch
suppress the request's late confirmation. These checks do not cancel a transaction
that has already committed; current state remains the owner's dashboard decision.

## Controlled restart checks — M4 Claude operator

1. Record the existing process, launcher, checkout and configuration. Confirm its
   public enrolled device identity and current server session without printing
   tokens. Preserve the owner's current session and all existing watcher/CV
   configuration.
2. Integrate this patch into that backend. Stop and restart **that same service**
   using its existing launcher, during the agreed controlled restart. Do not
   start a second process or use a standalone replacement.
3. Confirm exactly one listener on 8077 and one existing USB watcher. Inspect
   `/api/security` for the expected workstation/device, subscription connection,
   transport/schema errors and `voice_arm` outcome. Treat cached display fields as
   diagnostic information, not fresh authorization proof.
4. Check that the existing ASR → runtime → `request_arm_by_voice` callback is used,
   and the existing TTS renders its selected response. Do not route Claude text
   into a security callback. A subsequent physical trial needs an owner-reviewed
   grant for the configured Mac; do not consume a live grant as a background test.
5. For that owner-controlled trial, record the actual recognized phrase,
   confidence, grant/session IDs, fresh database result and playback outcome.
   Selecting a speech category or queueing text is not proof of audible speech.
   Do not claim camera/USB/Jetson or screen-lock acceptance from this ARM test.

## Verification and limits

The focused regression run passed **145 tests** across:

```sh
PYTHONPATH=backend OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
backend/.venv/bin/python -m pytest -q \
  backend/tests/test_security_arm_delegation.py \
  backend/tests/test_security_arm_readback.py \
  backend/tests/test_security_spacetime.py \
  backend/tests/test_security_service.py \
  backend/tests/test_companion_security_guard.py
```

The raw local test log is `data/acceptance/m4-voice-arm-focused.log`.
Coverage includes exact scope/identity, ambiguous and stale grants, policy/session
readback mismatches, replay, lost receipts, explicit refusal, full-utterance and
echo exclusions, repeated cancellation, and state-agnostic owner-only DISARM
responses with both empty and populated session caches. Touched Python files passed Ruff;
the patch passed `git diff --check`.

The [cloud acceptance harness](../../scripts/tests/airlock_m4_arm_acceptance.ts)
passed at **2026-11-08 07:21:29 UTC**. Its local receipt is
`data/airlock-m4-arm-acceptance/report.json`. It used a unique synthetic workstation
and baseline with actual owner/Mac credentials created for that fixture. The
production `request_arm_by_voice` method and HTTP transport consumed a real cloud
grant and verified its committed session; a repeated request did not select
another armed line. The session was real (`simulated=false`) with auto-lock off,
zero lock requests, and the fixture was verified disarmed afterward. The actual
Dhruv workstation was not changed by this test.

The harness stubs runtime session application and speech rendering: it proves
the selected `security_armed` category, **not microphone capture, complete runtime
startup, CV behavior or audible playback**. Separate read-only production checks
verified all five SQL responses and rejection of a non-Mac principal. Actual M4
restart, physical speech and hardware acceptance must be reported separately.

The latest combined fixture uses the owner consent HTTP window and private fixture CLI loading before the production M4 service call; see [m4-integrated-arm-verification.json](evidence/m4-integrated-arm-verification.json). It does not use the real M4 owner credential or prove audible playback.
