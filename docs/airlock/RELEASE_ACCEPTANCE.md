# Release acceptance reconciliation

Reviewed 2026-11-08 against the original supplied `LOCKI_AIRLOCK_PRD.md` §§0–16 and
the `Dhruvjain35/locki-airlock` audited snapshot `2212251`. This is the current
integration checklist; [PRD_STATUS.md](PRD_STATUS.md) retains historical lane evidence.
**Full acceptance is not complete.** Verification activated no hardware, microphone,
model or screen lock; cloud authorization tests used isolated fixture identities.

Source update: `services/nemotron-investigator/` is present in main snapshot
`1bbbfbf`. Earlier handoff notes describing it as absent are historical. Source
import does not close the live model, deployment or tracing gates below.

Final integration update: the full backend suite passed **1,562 tests, 20 skipped**;
frontend **46 tests and production build** passed; module **149 transactional +28
policy** tests passed; Node adapters **151**, read/HTTP boundaries **31**, owner
consent policy **28**, camera-free edge **22**, and model transport **10** passed.
These suites overlap and are not an acceptance score. The actual production M4
service method was also exercised against cloud using isolated fixture identities,
the real owner-consent HTTP/SDK path and a private fixture CLI credential: first
ARM succeeded only after session readback, replay failed, final fixture was disarmed
and there were zero lock requests. No actual M4 credential, microphone, camera,
model or audio was used by that test.

Authorized readback at 07:06 UTC confirmed the team's actual M4 session armed with
automatic locking off. At 07:17 UTC, two stored Jetson `person_in_zone` rows were
verified in that session with distinct capture/receipt timestamps, `simulated=false`
and `soft_observation` classification. This is **stored-value verification through
an authorized admin read**, not independent inspection of frames, publisher receipts
or the actual camera's scoped view. The publish-only camera role normally has no
event-read view; an empty camera view is expected. Session/event identifiers and raw
incident evidence remain in ignored local artifacts under CONTRIBUTING.md.

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
| 1 Authenticated arm/disarm and frozen baseline/app/camera/policy | Deployed owner-only reducers and narrow one-use ARM delegation; fresh cloud proof has 12 checks. Full owner-consent HTTP/private fixture CLI→production M4 service ARM/replay proof passed with zero locks and final fixture disarmed. Actual M4 owner ARM stored state was verified separately. | Actual running M4 owner-consent→voice→session-readback trial remains open; fixture credentials and direct owner ARM do not prove it. |
| 2 Independent physical evidence and 20–40 consented trials | Sid's Jetson adapter plus Dhruv's distinct Mac CV code are present; two actual-session Jetson rows marked non-simulated were verified by authorized stored-data readback. Areeb's newer edge/Mission Control source remains absent. | Actual device inventory, capture/unplug recovery, publisher receipt/frame provenance and held-out volunteer activity/false-alert trials. Source face identification now defaults off; actual M4 privacy configuration remains unverified. |
| 3 Real USB and visible app observations | M4 IOKit/NSWorkspace paths are present. Historical Sid app-launch proof is documented; README reports physical USB receipt/audio on M4. | Obtain original event/receipt/timing artifacts from the actual merged host, approved peripheral baseline and removal/insertion trial. App launch is not compromise. |
| 4 Deterministic correlation | Server correlation, unknown USB and soft person observation are implemented; transactional tests cover scope and timing. | Real independent camera plus USB episode and benign concurrent-USB confound trial; compare server incident to local warning without promoting local CV mode to server severity. |
| 5 Immediate warning and consented screen lock | Cached priority warning and dry-run lock paths exist; M4 README reports audio timings. | Full hardware-to-acoustic timing and actual expressly consented lock/unlock with observed confirmation. Dry run/executed is not confirmed locked. |
| 6 Authoritative SpacetimeDB | Server owns enrollment/session/incident/review/approval; private scoped views and deployment manifests are present. | Validate the merged host and two physical clients against the same deployed module; preserve database authority in all local fallback UI. |
| 7 Operational local Nemotron E4B | Python investigator, transport and imported service source exist. Historical team report: run-ba05ba8e294e, four calls/4.4s through M5. | Actual host branch, exact tag/runtime/model response and native evidence for 3+ model-selected approved tools on the correlated incident. Team timing is not inspected run proof. |
| 8 Native Relay and protected Code Quest tools | Historical native Relay/managed boundary evidence and local gate source exist. | Actual merged-model LLM/tool spans, correlation IDs and pre-execution allowed/denied Code Quest callback proof; application logs do not substitute. |
| 9 Existing live voice and safety priority | M4 speech stack is preserved; no second voice service is needed. Team reports local cached warnings/Bluetooth measurements. | Real mic→ASR→model/fallback→speaker, warning interruption and stop-talking-without-disarm on the integrated host; no duplicate runtime. |
| 10 Recovery and two-client synchronization | Historical independent SDK/browser/reconnect evidence exists. | Same real incident acknowledged/resolved on two physical clients, owner disarm, reconnect/network recovery and normal unlock. |

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
| NV-01 | Team real E4B run report; service source imported, running host unverified | Exact local M5 model tag, runtime/version and actual response artifact; no cloud-model substitution. |
| NV-02 | Checked local investigator; scoped evidence helper 24/24 | Real model selects ≥3 approved tools on real correlated evidence, records failures/uncertainty and preserves authoritative server severity. |
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
| SEC-04 | Server bounded metadata and retention policy; 16/16 read-verifier tests | Inspect real wire payloads: no frames/embeddings, consented retention, documented unresolved-case exceptions/backups. |
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
the full 1,562-pass backend suite. Retained optional biometric code is not P0 work
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
actual branch/contract and owner-approved incident/event IDs. His identity has no
verified actual-token grant/report in this work. Only the real owner may grant the
minimum read scope. Never substitute deployment/admin credentials or give the
investigator mutation roles. [INVESTIGATOR_INTEGRATION.md](INVESTIGATOR_INTEGRATION.md)
contains exact views/reducers and prepared owner commands.

Fresh on this snapshot, without network or hardware: read-only verifier **16/16**;
authoritative severity/report reconciler **24/24**; investigator transactional
authorization suite **38/38**. These suites use explicit fixtures.
Their commands and file hashes, plus the inspected final integration test totals and
sanitized cloud-proof summaries, are recorded in
[release-acceptance.json](evidence/release-acceptance.json). Module/script type checks,
changed-Python lint and dependency audits passed (zero reported npm vulnerabilities).
Build retains a 965KB JavaScript chunk warning; backend reports one dependency
deprecation warning. The 20 skipped backend tests are not passing acceptance proof.
No aggregate software count closes the physical/model requirements above.
