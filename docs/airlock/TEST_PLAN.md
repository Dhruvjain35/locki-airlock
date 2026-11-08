# Workspace acceptance tests

These tests cover behavior around an unattended engineering workstation. Software
tests are listed in [SETUP.md](SETUP.md#software-checks); perception sampling and scoring are
defined in [ACCURACY_TRIALS.md](ACCURACY_TRIALS.md). The full scenario backlog is in
[SCENARIOS.md](SCENARIOS.md). The runs below are pending.

## Test setup

Use a test account, disposable files, harmless peripherals and consenting participants.
Record the running Mac and Jetson revisions, configuration, peripheral baseline,
app policy, camera position and workload. Check which watcher and warning services
are active, including the optional edge alarm; duplicate consumers can make a single
event look like several incidents. Check observer capability and accepted publication
separately from HTTP health success.

Run each relevant case with normal OS lock enabled. An unlocked demonstration does
not establish what the product sees or prevents on a locked workstation. Keep real
lock trials separate from dry runs and follow [LOCK_ADAPTER.md](LOCK_ADAPTER.md).
Leave a person present who can unlock the test Mac.

## First acceptance runs

| Test | Procedure | Pass condition | Cases |
|---|---|---|---|
| Friend near the desk | Arm, lock normally, then have a friend walk past, stop to talk and reach for an adjacent notebook. Inspect local speech as well as server/UI state. | Presence alone causes no lock, accusation, security warning or external escalation. | SOC-01–03, HUMAN-01 |
| Unattended owner console | Leave a test owner console authenticated. Compare access before and after the native lock. Inspect every available disarm route. | The documented unattended setup prevents a visitor from using owner controls. Any unlocked exception remains an explicit gap. | AUTH-06 |
| Existing editor and browser | Attempt harmless edits and dummy-data access in apps that were already open, with the screen locked. Repeat unlocked only to establish observation limits. | Native lock prevents interactive access. The unlocked case is reported as an Airlock coverage gap, not successful detection. | TAMP-01–03, COMB-07 |
| Device policy | Connect an approved peripheral, a harmless unapproved peripheral and a charge-only cable. Include OS accessory denial. | Reports match actual observations and permission. No claim of data access or malware merely from connection. Any consented lock has independently observed confirmation. | TAMP-05, DEVICE-01/02/04 |
| Benign coincidence | Have a friend pass while a dock reconnects. Repeat with no passerby and compare both responses. | No claim that the friend caused the connection. Record any unnecessary escalation or lock as a product failure, even if the temporal rule operated correctly. | WORK-06, COMB-01 |
| Work continues | Run a compile, a GUI-dependent job and a safe board-flashing fixture through arm, lock and recovery. | No shutdown, process kill, power cut or USB reset. Compare job output and duration; publish unsupported workloads instead of claiming universal compatibility. | WORK-01–03/07, COMB-06 |
| Missing coverage | Disconnect camera, stop host watcher and interrupt database connectivity separately. Also freeze frames while the camera heartbeat continues. | UI names the unavailable capability within its declared freshness budget. Remaining monitoring is accurate; reconnect requires fresh state. | FAIL-01–03, VISION-08, COMB-09 |
| Sensor independence | Trace actual Jetson power/network dependencies; safely disconnect the Mac or dock path. | Independent observation survives only where the claimed dependencies permit it. Loss is shown as loss, not an empty desk. | THEFT-03/08, FAIL-05 |
| Return and revocation | Deliver delayed events while the owner unlocks/disarms. Revoke a pending action before, then after its final execution authorization. | No stale lock loop. Revocation before the boundary prevents execution; an already-started action is reported honestly and recoverable. | AUTH-04, STATE-03/04/06, COMB-04 |
| Voice permission | Try unapproved disarm, quoted ARM, “stop talking,” and a delayed ASR result after listening permission ends. | No unauthorized state change or restored listening. Muting speech leaves monitoring and incident state intact. | HUMAN-04–06, STATE-03 |
| Investigator and tools | Ask leading questions about a benign incident; supply hostile metadata with dummy secrets; make the model unavailable. | Answers stay within evidence. Denied managed callbacks never start, no secrets leak, and immediate policy does not wait for the model. | TAMP-08, HUMAN-07, PRIV-07, FAIL-04 |
| Review and recovery | Use two separately authenticated clients, including an unprivileged one. Acknowledge, resolve, disarm, reconnect and unlock. | Both authorized clients recover the same committed state; wrong-scope actions fail. Resolution does not silently grant future trust. | AUTH-07, STATE-07, PRIV-05/06, REC-01/02/06 |

Before each run, set the expected policy, health-expiry budget and timing boundary.
Do not choose them after seeing the result. Where workload tolerances differ, record
the owner's accepted tolerance before the trial.

## Results

Use the [episode template](evidence/accuracy-trials-template.csv), with a separate
sanitized result summary for non-vision cases. Record:

- Case ID, deployed revision/configuration, exact procedure and expected outcome.
- Actual observations, selected response and impact on work as separate fields.
- Event capture, server receipt, response and confirmed outcome times, with clock
  uncertainty. A lock helper returning success is not confirmation of OS lock.
- Passed, failed or blocked, with missing data and failed attempts retained.
- Evidence reference, recovery outcome and any remaining limitation.

Provider acceptance does not establish that the owner read an alert. A successful
audio call does not establish that someone heard it. Collect those outcomes at the
boundary the claim names.

Keep raw recordings, credentials and incident payloads out of git. Store only
sanitized summaries under `docs/airlock/evidence/`, following
[CONTRIBUTING.md](../../CONTRIBUTING.md). Update [release acceptance](RELEASE_ACCEPTANCE.md)
when a required claim has supporting results; do not turn planned scenarios into
passing results by changing a status label.

## Workplace pilot

After the first runs, compare ordinary native protection with the same setup plus
Airlock. Use separate tuning and held-out people/sessions. Measure useful incident
review, nuisance sounds/notifications/locks per protected hour, missed events,
coverage loss, job disruption and owner recovery effort. Include adjacent desks,
different postures, accessibility needs and ordinary lighting changes.

Report counts and denominators alongside latency distributions. Keep the existing
prototype targets in the accuracy protocol; a passing demonstration is not a
workplace false-alarm estimate. If added context does not improve response enough
to justify setup and interruptions, narrow the product before adding more hardware.
