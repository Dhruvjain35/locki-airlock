# Release acceptance reconciliation

Pre-`f73c40a` merged application checkpoint: **1,877 backend tests passed, 20 skipped
in 185.75s** and **135 frontend tests passed across eight files**. Final frontend
typecheck and production build passed. The application source at `fa196ad`
combines pinned upstream `afd008e`, owner-access commit `330530f` and the reviewed
client/runtime safeguards. A separate merge-focused run passed 176 tests in 4.71s.
The full backend suite does not include the separate edge or investigator-service
suites; their receipts retain their own source scope.
The earlier reviewed M4 client checkpoint passed 1,852 backend tests/20 skipped
and 79 focused tests before the edge/recorded-investigation merge.
An earlier full attempt had one unchanged simulated-motion timing failure; all
ten legacy mock tests passed on a targeted rerun and the subsequent full run passed.
No actual motion, model, microphone, camera or lock was activated by these checks.

Reviewed 2026-11-08 against the original supplied `LOCKI_AIRLOCK_PRD.md` §§0–16 and
the `Dhruvjain35/locki-airlock` audited snapshot `2212251`, with the current source
handoffs now including pinned upstream `afd008e`, edge snapshot `e1c1a7b` and owner-access
commit `330530f`. This is the current
integration checklist; [PRD_STATUS.md](PRD_STATUS.md) retains historical lane evidence.
**Full acceptance is not complete.** Verification activated no hardware, microphone,
model or screen lock; cloud authorization tests used isolated fixture identities.

The prior full backend checkpoint passed **1,772 tests, 20 skipped in 182.14s**,
before the new M4 service-client merge and its independent review.
The final investigator-service unit suite passed **425 tests, one skipped and
three live-model tests deselected in 4.19s**, including 38 new legacy-cache
authorization cases. Its earlier 387-test baseline retains its earlier source scope.
The older 54-test frontend checkpoint and
recorded-input build remain historical, superseded for the merged UI by the 135-test run.
Earlier full backend checkpoints were 1,699/20 skipped before dispatch hardening
and 1,729/20 skipped afterward; their source scopes and logs remain distinct.
Node adapters passed **151** and the combined investigator/owner HTTP boundaries
passed **50**, including the **35-test** investigator verifier. Earlier retained
component checks passed 149 transactional +28 policy, 28 owner-consent policy,
22 camera-free edge and 10 model-transport tests. These runs have distinct source
scopes, overlap, and are not an acceptance score or a final-commit certification.

The user-authorized first application batch reached main at
[`2044019`](https://github.com/Dhruvjain35/locki-airlock/commit/204401941a0af5c58b7e7726555812ec5a0469d8).
The exact updated workflow was separately published at
[`7328c4e`](https://github.com/Dhruvjain35/locki-airlock/commit/7328c4e8788cd9985816e77a2f506a8b88f1fac4).
The earlier OAuth workflow-scope rejection is a historical delivery attempt,
not a current credential blocker; no additional user credentials are required.
Separately, [Actions run 42464179187](https://github.com/Dhruvjain35/locki-airlock/actions/runs/42464179187)
last reported `startup_failure` with zero jobs. Its cause is not established by
that status, and local test results are not a claim of passing GitHub CI. The
earlier investigator hardening/docs are bound by their checkpoint hashes below.
The owner-access batch is confirmed published at `330530f`. The broader `fa196ad`
merged delivery remains pending at this cutoff; no later main commit is invented.
The M4 client/runtime/record changes have been reconciled with the reviewed
`29d0dde` safeguards. The manifest binds the merged source and separately scoped
test logs; successful Git delivery does not establish host activation. Later upstream
`f73c40a` is outside this checkpoint and requires its own merge/check receipt.
Git delivery does not restart M4/M5.

The production M4 service method was earlier exercised against cloud using isolated fixture identities,
the real owner-consent HTTP/SDK path and a private fixture CLI credential: first
ARM succeeded only after session readback, replay failed, final fixture was disarmed
and there were zero lock requests. No actual M4 credential, microphone, camera,
model or audio was used by that test.

Earlier authorized readback confirmed the team's actual M4 session armed with
automatic locking off. Two stored Jetson `person_in_zone` rows were
verified in that session with distinct capture/receipt timestamps, `simulated=false`
and `soft_observation` classification. This is **stored-value verification through
an authorized admin read**, not independent inspection of frames, publisher receipts
or the actual camera's scoped view. The publish-only camera role normally has no
event-read view; an empty camera view is expected. Session/event identifiers and raw
incident evidence remain in ignored local artifacts under CONTRIBUTING.md.
The latest separate authorized status readback found the workstation **disarmed**,
the Jetson enrollment enabled and its stored sequence at 13. These are readback
facts at their observation time, not a new arm/disarm or physical capture test.

An independent authorized admin read also confirmed exactly **one enabled
incident-evidence grant**, granted by the exact existing owner, with one selected
event and the frozen baseline included, and **zero investigator membership rows**.
This confirms stored grant presence and scope counts. It does not reproduce an
investigator-token negative test or prove that the reviewed M4/M5 revisions are running.
A subsequent incident/event read confirmed the selected record's workstation/session
match, official medium severity and one non-simulated Mac `usb_added` event, with
capture stored one millisecond before commit. This is stored-value verification,
not independent physical-frame or publisher-receipt proof.

Evidence levels used here:

- **Checked software:** inspected implementation and/or a freshly executed bounded
  test. This proves only that test's boundary.
- **Recorded artifact:** a tracked report from an earlier run, inspected here but
  not independently rerun. Its source, principal and fixture classification matter.
- **Team report:** a README/runbook statement without the underlying raw run artifact
  in this snapshot. Treat it as a useful lead, not independently verified acceptance.
- **Open:** required artifact, integration or physical trial is unavailable.

## All ten P0 requirements

| P0 | Current integration evidence | Remaining release gate |
|---|---|---|
| 1 Authenticated arm/disarm and frozen baseline/app/camera/policy | Deployed owner-only reducers and narrow one-use ARM delegation; fixture cloud proof has 12 checks and production-service ARM/replay proof. Owner recovery and scoped investigator consent UI are included in the 135-test frontend suite; no live recovery was performed by these tests. Earlier actual owner ARM was read back; the latest status readback is disarmed. | Actual existing-owner browser recovery and running M4 owner-consent→voice→session-readback trial remain open; fixture credentials and direct owner ARM do not prove them. |
| 2 Independent physical evidence and 20–40 consented trials | Areeb's `e1c1a7b` edge snapshot and follow-ups are now present under `edge/jetson/locki_edge/`, with Sid's earlier adapter and Dhruv's distinct Mac CV retained. Two actual-session Jetson rows marked non-simulated were previously verified by authorized stored-data readback; this does not establish which new source revision produced them. | Actual device inventory, merged-source capture/unplug recovery, publisher receipt/frame provenance and held-out volunteer activity/false-alert trials. Source face identification defaults off; actual M4 privacy configuration remains unverified. |
| 3 Real USB and visible app observations | M4 IOKit/NSWorkspace paths are present. Historical Sid app-launch proof is documented; README reports physical USB receipt/audio on M4. | Obtain original event/receipt/timing artifacts from the actual merged host, approved peripheral baseline and removal/insertion trial. App launch is not compromise. |
| 4 Deterministic correlation | Server correlation, unknown USB and soft person observation are implemented; transactional tests cover scope and timing. | Real independent camera plus USB episode and benign concurrent-USB confound trial; compare server incident to local warning without promoting local CV mode to server severity. |
| 5 Immediate warning and consented screen lock | Cached priority warning and dry-run lock paths exist; M4 README reports audio timings. | Full hardware-to-acoustic timing and actual expressly consented lock/unlock with observed confirmation. Dry run/executed is not confirmed locked. |
| 6 Authoritative SpacetimeDB | Server owns enrollment/session/incident/review/approval; private scoped views and deployment manifests are present. | Validate the merged host and two physical clients against the same deployed module; preserve database authority in all local fallback UI. |
| 7 Operational local Nemotron E4B | Merged client/runtime/record guards passed the 1,877-test backend suite and 176 focused checks. An admin read confirms one enabled owner-matched evidence grant and no investigator memberships. Mustafa reports successful own-token verification and two LIVE E4B API runs, one through the M4 tunnel, with seven selected read tools; see the attributed report below. | Confirm the current incident's grant freshness, inspected verification/model artifacts and adoption of the reviewed host revisions; run the actual M4 runtime WAV path and preserve native model/Relay traces. The teammate's API report does not independently establish those runtime/native gates. |
| 8 Native Relay and protected Code Quest tools | Historical native Relay/managed boundary evidence and local gate source exist. | Actual merged-model LLM/tool spans, correlation IDs and pre-execution allowed/denied Code Quest callback proof; application logs do not substitute. |
| 9 Existing live voice and safety priority | M4 speech stack and new turn/quiet/barge behavior are preserved. Fake tests cover microphone close/reopen failure, held/late ASR cancellation and immutable capture-epoch checks; no second voice service is needed. | Coordinated M4 restart, real mic→ASR→model/fallback→speaker, warning interruption and stop-talking-without-disarm on the integrated host; no duplicate runtime. |
| 10 Recovery and two-client synchronization | Historical independent SDK/browser/reconnect evidence exists. The merged owner-recovery UI verifies the existing owner against the selected destination/workstation before persisting a recovered profile; fake tests cover its boundaries. | Actual existing-owner recovery, same real incident acknowledged/resolved on two physical clients, owner disarm, reconnect/network recovery and normal unlock. |

## Every named acceptance criterion (§13)

| Criterion | Evidence level / disposition | Exact closure artifact |
|---|---|---|
| HW-01 | Open; enrolled label says Orin Nano Super/Brio 101, not on-device proof | On-device model, power connector, JetPack, RAM and webcam identification. Do not assume the PRD's Nano designation. |
| HW-02 | Open; Areeb Mac soak is a team report and a different device | Ten-minute actual Jetson camera stream with timestamps, resource/FPS metrics and unplug recovery. |
| PHY-01 | Two actual-session Jetson soft observations stored as non-simulated verified; team Mac presence measurements | Volunteer/frame provenance, original publisher receipts and held-out pass-by/standing/interaction labels, false alerts and trial counts. |
| MAC-01 | Checked server baseline rules; earlier empty physical baseline | Genuine allowed peripheral in frozen baseline, no high incident from that peripheral. |
| MAC-02 | Team M4 USB incident report | Exact real USB event, armed session, accepted reducer receipt and incident; no reading/execution of device files. |
| MAC-03 | Historical native Calculator proof; new source uses NSWorkspace | Merged-host event/receipt and bundle/signature metadata where available; explicit visible-app limits. |
| MAC-04 | Dry-run implementation; README says no real lock run | Prior explicit consent, real own-Mac request/action/observed-lock evidence and unlock/disarm. |
| VOX-01 | Preserved software and team audio reports | Live microphone/ASR/model/speaker sample with P0 interruption and acoustic timing method. |
| NV-01 | v0.3 source present; Mustafa reports real `nemotron-3.5-bolt:e4b` LIVE runs on M5 and through the M4 tunnel | Inspect original model/runtime/response receipts and confirm adoption of the reviewed revision; no cloud-model substitution. |
| NV-02 | Checked local/scoped investigator software; Mustafa reports seven selected read tools, grounded facts and distinct official/advisory severity | Inspect those private run/verification artifacts and prove the current M4 runtime path on its freshly granted incident; preserve failure/uncertainty and authoritative server severity. |
| NV-03 | Historical native tool/guardrail proof | Actual Nemotron run's native Relay start/end LLM/tool spans with shared correlation ID. |
| NV-04 | Historical managed-boundary proof | Unauthorized callback never starts, harmless authorized callback succeeds in current host; fixture approval labeled. |
| ST-01 | Recorded local/cloud reducer evidence | Recheck merged runtime's real session/incident IDs are reducer-created, not merely local mirrors. |
| ST-02 | Recorded independent clients; physical-client trial open | Same real incident on two separately authenticated physical clients, rendered timestamps. |
| ST-03 | Recorded authorization tests | Current exact module/runtime test denies unauthorized acknowledgement and approval. |
| ST-04 | Recorded transactional denial tests | Repeated, expired, modified and cross-scope approval consumption denied; no callback on denial. |
| ST-05 | Recorded SDK reconnect/server restart | Current merged clients recover latest committed state after actual network disconnect/reconnect. |
| SEC-01 | Historical managed tool denial; real host/model open | Harmless malicious README fixture proposes forbidden tool; native gate denies before execution with no real secrets touched. |
| SEC-02 | Software stale/offline handling | Physically disconnect camera; UI degrades and no stale visual evidence is presented as current. |
| SEC-03 | Model-independent paths and earlier cached warning proof | Actual hardware warning while model unavailable; truthful policy/action state. |
| SEC-04 | Server bounded metadata and retention policy; 35/35 fixture read-verifier tests | Inspect real wire payloads: no frames/embeddings, consented retention, documented unresolved-case exceptions/backups. |
| SEC-05 | Owner-only disarm and deterministic voice restrictions | Live unapproved/quoted/negated voice trial causes zero disarm and no delegated permission issuance. |

The user's additional **EDGE-06** has partial stored-data evidence: two Jetson rows
preserve capture/receipt time and `simulated=false` in the correct active workstation
session. Exact ACCEPTED publisher receipts, original capture provenance and a
workstation-authorized device-scoped readback remain required. Publish-only camera
roles are intentionally unable to read incident/event views; do not loosen this
authorization to manufacture a camera self-read. This Jetson evidence does not close
the separate Areeb Mac development-camera or M4-camera acceptance. Preserve all
separate enrollments and verify each requested camera's own receipt.

## Accuracy, exclusions and release claims

Use [ACCURACY_TRIALS.md](ACCURACY_TRIALS.md): tune on separate people/scenarios,
freeze configuration, then report held-out episodes. Distinguish walking past,
standing nearby and interacting; do not infer malicious intent or count every frame
as an independent success. Include benign USB correlation confounds, missing-data
rates, confusion matrices and uncertainty. No numerical accuracy result has been
created by this reconciliation.

Keep original targets: ≤1 false physical alert per 20 benign trials; ≥90% known-case
accuracy; hardware-event→local-warning ≤1.5s; USB→dashboard ≤2s LAN; owner alert ≤3s
LAN; zero unauthorized protected callbacks. Record n, p50/p95/max, exact timing
boundaries, clock uncertainty and failures. Commit-to-audio timing excludes capture
and physical/audio latency unless separately measured. One fast run is not a guarantee.

The original snapshot's enabled face enrollment/matching conflicted with the PRD's
explicit no-face-identification / no-biometric-recognition scope. **The integrated
source now disables face identification by default**, including demo enrollment and
identity display. Focused privacy/vision tests passed 97 with one skip, followed by
the broader backend regressions. Retained optional biometric code is not P0 work
and cannot authorize arm/disarm. The currently running M4 build and privacy setting
have not been verified: do not describe it as privacy-compliant solely because the
new source default is safe. Person/zone activity measurement remains the target.

P1 stays optional: second Zeus camera, electrically reviewed Grove button/LCD,
Jetson local playback, opted-in external notifications and signed attestations.
Motors, firmware flashing, pursuit, malware USB/injection, network attacks, covert
recording, global quarantine, remote shell kill switches, emergency calls and Apple
Lockdown Mode toggles remain excluded. No new model downloads or hardware actions
are necessary for the software checks here.

The new integration repository is private. Historical public source/CI from Sid's
older snapshot does not prove this merged release is publicly submitted or that its
CI passed. Publication/submission must use a reviewed sanitized snapshot and preserve
attribution/licenses; no raw runtime data, credentials or model weights. No blanket
sponsor eligibility or deployability claim is supported yet.

## Investigator handoff and fresh checks

[Issue #2](https://github.com/Dhruvjain35/locki-airlock/issues/2) requests Mustafa's
owner-approved incident/event selection and actual-host verification. The imported
[v0.3 contract](../../services/nemotron-investigator/INTEGRATION.md) is available.
Stored evidence-grant presence and exact owner match are independently confirmed;
actual-token/model results below are attributed to Mustafa. Only the real owner may grant the
minimum read scope. Never substitute deployment/admin credentials or give the
investigator mutation roles. [INVESTIGATOR_INTEGRATION.md](INVESTIGATOR_INTEGRATION.md)
contains exact views/reducers and prepared owner commands.

Main `1bbbfbf` additionally supplies the M4 HTTP client and “what happened?” route
for configured authoritative live incident IDs. This replaces the earlier missing-client
gap. The merged `afd008e` source preserves strict endpoint/key-file checks,
bounded responses, exact incident/run/live/severity binding and per-turn isolation.
Read-only records are fetched afresh; running or failed jobs expose status only.
The 1,877-test backend and 135-test frontend results cover this merged application,
while actual reviewed-host adoption and the M4 runtime/model/spoken proof remain separate.

In [Mustafa's issue #2 report](https://github.com/Dhruvjain35/locki-airlock/issues/2#issuecomment-6719727896),
he states that the investigator's own-token verifier exited zero, private tables
were denied, no reducers were called, and one incident was visible with no memberships.
He reports a non-simulated LIVE E4B API run through the M4 tunnel in 11.9s and an
M5 operator run in 12.1s, with seven read-only evidence tools followed by finish.
The M4-tunnel run completed with warnings: grounding flagged intent wording.
Official medium severity remained distinct from the model's medium risk/low
confidence. These are **Mustafa's reported results**, not a reproduction by this
review or independent inspection of the original model/native artifacts.
He explicitly notes that the tunnel API run did not exercise the M4
`NemotronService` runtime WAV path. Fresh incident grants, that runtime trial,
and native Relay/LLM proof remain required. Publishing API tool traces through
the database `record_agent_step` path is also not established; the read-only
record panel alone does not populate that shared investigation timeline.

The service uses an evidence-only investigator identity, with no workstation
membership/device role. Its API key does not create a database grant. Service
token files may be plain or JSON; the separate Node verifier requires private
JSON containing `token`. Fresh server-authenticated principal and incident scope
checks precede model use, finalization and serving saved live reports. These
software guards and anonymous schema comparison do not verify the actual M5 token
or absence of separate cloud administrative access; the own-token verifier and
its sanitized receipt remain required.

Separate source-review receipts cover **142 mocked-HTTP adapter tests**, **121
scoped tools/agent/assessment tests**, and **77 voice-input hold tests with one
skip**. These overlap broader suites and use fake capture/scripted model inputs;
they establish neither an actual M5 service nor a microphone or model run.

The locally checked read-only verifier passed **35/35** with fixture responses, including
wrong-principal, cross-workstation, token-revocation and failed-transport cases.
That fixture run proves no actual grant, M5 verification or model trace; the
independent admin readback and attributed live report have separate scopes. Earlier
fixture checks passed 24/24 for authoritative severity/report reconciliation and
38/38 for transactional investigator authorization. Their commands and file hashes,
plus the inspected integration test totals and
sanitized cloud-proof summaries, are recorded in
[release-acceptance.json](evidence/release-acceptance.json). Module/script type checks,
backend changed-Python lint and dependency audits passed (zero reported npm vulnerabilities).
Investigator critical lint (`E9,F63,F7,F82`) passed; pre-existing full-style findings
remain and are not claimed clean. The final merged build retains a 1,002.04kB JavaScript chunk warning; backend reports one dependency
deprecation warning. The 20 skipped backend tests are not passing acceptance proof.
No aggregate software count closes the physical/model requirements above.

## Edge source handoff

Pinned upstream `afd008e` includes Areeb's `e1c1a7b` snapshot of
`feature/airlock-edge` (`4d673db`) and follow-ups. The new runtime has configurable
MobileNet-SSD/OpenCV DNN and detectNet person providers, a motion-only alternative,
capture supervision, a durable metadata outbox, server `last_seq` synchronization
and presence reassertion (default five seconds; zero disables, otherwise one to
less than ten seconds). It includes clearing/camera event mappings compatible
with the module source and sends optional presence pushes only for locally noted
accepted presence events. Reassertions are ongoing presence, not independent
visits or new incidents; neither provider code nor replay tests establish intent
or physical detection accuracy.

The [new edge runbook](edge/README.md) and [deployment guide](edge/DEPLOY.md) are
source handoffs. Their device/deployment statements remain team reports until
the original host receipts are collected. Sid's earlier adapter remains at
`edge/jetson/airlock_camera.py`; it has separate sequence state and must not run
alongside the new runtime for the same camera/device. The earlier 22-test adapter
receipt does not certify the newly imported suite. No new camera, model download,
edge process, notification or physical acceptance trial was started for this
documentation reconciliation.
