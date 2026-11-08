# AIRLOCK volunteer accuracy trial protocol

Status: **protocol only; no volunteer accuracy results have been collected by this work.**
An accepted real camera event closes a transport check, not an accuracy check.
A ten-minute stream or high frame rate demonstrates neither low false alerts nor
reliable recognition of computer interaction. Keep deployment acceptance open.

## What the current implementation can establish

The checked-in `edge/jetson/airlock_camera.py` uses a full-body HOG person detector,
a bounding-box-center desk ROI and frame debounce. It emits `person_in_zone` and
`person_cleared`; continuous presence refreshes every five seconds. Its confidence
value is an uncalibrated detector margin. Motion-only mode cannot supply person
evidence. Neither mode classifies walking past, standing nearby, typing, touching
the computer, identity, permission or intent. This is the inspected Sid adapter;
Areeb's and Dhruv's local branches require a separate version/configuration record.

`spacetime/src/index.ts` treats person presence alone as a soft observation. An
unknown USB observation within the same workstation/session and the inclusive
10-second correlation window can become a high-severity incident. Temporal
co-occurrence does not establish that the detected person inserted the USB or
acted without permission. Measure that association error explicitly.

Do not rename `person_in_zone` to “computer interaction” to make a three-class
metric pass. Add no detector during this evaluation. First measure the existing
capabilities and failure modes; any later behavior classifier needs a new frozen
version and fresh held-out evaluation.

## Ground truth and expected behavior

Use consenting adult volunteers and a harmless demo account. A human observer
records the activity independently of the detector output. Labels describe visible
actions, not “attacker”, “suspicious person” or inferred malicious intent.

| Ground-truth activity | Standardized action | What counts as an unwanted escalation |
|---|---|---|
| `pass_by` | Walk past the desk, including a path briefly crossing the ROI; do not stop or touch equipment. | An interaction claim, security warning or incident attributed to computer interaction. Presence may be a correct observation if the person crossed the ROI. |
| `stand_nearby` | Stand near the desk for 15 seconds, hands away from the computer; include speaking to the seated owner. | An interaction claim, warning or incident based only on proximity. Presence is expected when inside the ROI. |
| `interact` | On cue, actually use the harmless keyboard/trackpad or touch the consenting owner's test computer for 10 seconds; include seated/partially occluded use. | Failure to recognize interaction is a capability gap. A presence-only result does not count as interaction recognition. Authorization must be recorded separately. |
| `empty` | No participant in the desk region. Include harmless lighting/background motion. | False person presence, interaction claim, warning or incident. |

Record `in_roi` independently: `yes`, `no`, `partial`, or `uncertain`. ROI presence
accuracy and three-way behavior discrimination are different questions. Mark
unclear actions `ambiguous` before looking at results; retain them with an exclusion
reason and report the exclusion count. Never silently remove failed captures or
relabel a scripted interaction because the detector missed it.

## Freeze the trial before capture

1. Record source commit, publisher version, camera device ID and authenticated
   public identity, workstation/session IDs, detector/model and version, resolution,
   capture/processing FPS, ROI, debounce values, camera position, lighting, clock
   offsets and server policy hash. Record actual values rather than assumed defaults.
2. Confirm exactly one publisher process owns each camera/state file. Preserve
   Areeb's `mac-dev-cam-01` enrollment while testing a different M4 camera; use each
   camera's own enrolled identity and evaluate their results separately.
3. Use a valid real session (`simulated=false`) with auto-lock disabled in both
   current and frozen policy. No lock executor, robot action, owner-token sharing,
   permission widening, detector downloads or baseline manipulation is part of this
   trial. Camera observation must be explicitly enabled by the local operator.
4. Record consent and the stop procedure locally. Use participant codes, not names,
   in trial data. No raw images, video, face crops or embeddings enter SpacetimeDB.
   Default to no stored recordings at all; live observer labels and sanitized event
   metadata suffice. Store only bounded protocol-approved metadata; trial labels and
   measurement fields belong in the local evaluation CSV, not extra reducer keys.
5. Write the sample counts, timing windows, randomization seed, exclusion rules and
   acceptance limits into a signed-off local run manifest before viewing held-out
   results. Freeze configuration after tuning. Stop on consent withdrawal, unsafe
   action, camera contention, unexpected locking or authentication mismatch.

## Practical pilot and held-out split

Proposed initial pilot: six volunteers, with two used only to set placement/ROI and
debounce, and four reserved for testing. For each held-out volunteer, randomize
three activities (`pass_by`, `stand_nearby`, `interact`) across two conditions
(ordinary unobstructed lighting; an explicitly recorded harder condition such as
backlighting or seated partial occlusion), twice per cell. This gives **48 held-out
episodes, 16 per activity**. Keep every repetition from a volunteer in the same
split. Use a distinct tuning set; do not tune against these 48 outcomes.

The numbers are a feasible pilot proposal, not a sample-size justification for
deployment. If only one or two volunteers are available, report a demonstration
case study rather than population accuracy. Preserve person/site/session grouping
when evaluating new settings; nearby frames and repeated trials are correlated.
Group-separated evaluation prevents the same subject's characteristics from
appearing in both tuning and evaluation. [scikit-learn grouped evaluation](https://scikit-learn.org/stable/modules/cross_validation.html)

For each episode, capture a five-second empty lead-in, the labeled activity and a
five-second tail. Maintain an observed empty interval of at least 15 seconds between
episodes, verify clearing, and record any persistent latch as a result. Do not
erase queued evidence or reset sequence counters between trials. The interval
reduces accidental overlap with the server's 10-second correlation window; it does
not replace checking actual event timestamps and incident links.

Add a separate 30-minute no-interaction background run and a preregistered USB
confound block. A suggested 12-episode confound pilot crosses all three activities
with approved/unapproved harmless USB conditions, twice each. In the pass-by and
stand-nearby cases, a **different consenting operator** performs the USB action.
Record who performed each action using participant codes. Include both temporal
orders and captures inside/outside the 10-second window. Do not alter the owner's
baseline just to provoke a result: use already approved test hardware or a harmless
unapproved peripheral under the owner's explicit trial authorization.

An unknown-USB incident is expected by the current policy even when the volunteer
is innocent. Count the policy firing separately from a false claim that proximity
proves interaction or wrongdoing. Report how often benign independent actions
produce the correlated high-severity rule and whether the actual warning wording
overstates the evidence. Record authorized interaction as its own stratum; touching
the computer is not evidence of unauthorized use.

## Evidence and scoring

Use the adjacent [CSV template](evidence/accuracy-trials-template.csv), one row per
episode. Keep original sanitized publisher payloads and reducer receipts locally,
linked by event ID. Record observation, publication, receipt and visible/audible
warning times separately; never overwrite capture time with publication time.
Owner-scoped `my_device_events` readback must match workstation, camera device,
session, event ID, original capture timestamp and `simulated=false`. Camera-role
self-readback is not currently supported; do not grant reviewer access to enable it.

An observer who has not viewed predictions labels actions first. On a random
preregistered subset, a second observer independently labels the same live episode;
record disagreements rather than resolving them using model output. A safe manual
cue can provide timing; document its precision and measured cross-device clock
offset. Use `not_measured` for missing acoustic timing.

Report at least:

- **Behavior confusion matrix:** ground truth `pass_by`, `stand_nearby`, `interact`
  versus emitted behavior `pass_by`, `stand_nearby`, `interact`, `unknown/unsupported`.
  The present presence-only adapter belongs in `unknown/unsupported`, not a fabricated
  behavior class. Its three-way interaction recall is not established.
- **ROI presence matrix:** independently labeled presence versus person evidence,
  including empty scenes, partial occlusion and unavailable capture.
- **Per-activity false-alert rate:** episodes with an unwanted interaction warning
  or escalation divided by all eligible pass-by or stand-nearby episodes. Also show
  raw presence-event rate; a correct presence observation is not itself a false alert.
- **Interaction miss rate:** missed interaction decisions divided by valid interaction
  episodes, only for a system that actually emits such a decision. Report unsupported
  capability explicitly instead of claiming success from a presence event.
- **Operational availability:** unavailable/failed capture and rejected/lost transport
  episodes divided by all scheduled episodes. Report end-to-end successful recognition
  over all scheduled positive episodes as well; excluding outages must not inflate it.
- **Alert burden:** distinct user-visible warning episodes and incidents per hour of
  no-interaction observation, with duplicate audio warnings counted separately. Group
  five-second presence refreshes within a trial into one presence episode; retain all
  raw event IDs. Frames and repeated refreshes are not independent trials.
- **Timing:** median, p95 and maximum capture-to-committed-event and
  capture-to-user-warning latency, with sample counts and clock uncertainty. A reducer
  acceptance time is not an acoustic onset measurement.
- **Confounds:** benign USB correlation rate, per-camera/per-condition/per-volunteer
  breakdowns, plus the exact incident summaries and warning wording that appeared.

Every rate needs a numerator, denominator and uncertainty. Report 95% binomial
intervals for independent episode estimates (Wilson or exact, identified in the
report). For repeated-person/session data, also show grouped results; ordinary
binomial intervals do not account for clustering. With only four held-out people,
do not present a narrow frame-level interval or a stable population generalization
claim. A larger study should resample or model participant/session clusters and
justify its number of independent groups. [NIST proportion intervals](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm)

For intuition, under independent equal-probability trials, zero errors among 16
trials still permits a one-sided 95% error upper bound of
`1 - 0.05^(1/16)`, about **17.1%**. If a product owner selected a maximum 1% error
rate, at least 299 independent zero-error trials would be needed merely for that
one-sided bound; clustering, multiple conditions and missed interactions require
additional study. These are statistical illustrations, not observed results or
LOCKI-certified thresholds. [NIST exact binomial limits](https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbici.htm)

## Acceptance and next decisions

The original PRD §14 already sets prototype measurement goals: false physical
alerts at most **1 per 20 benign trials**, at least **90% accurate known test
cases**, hardware-event-to-local-warning at most **1.5 seconds**, USB-to-dashboard
at most **2 seconds on LAN**, and owner alert at most **3 seconds on LAN**. Retain
these targets in the run report beside actual results and sample counts. Define
which benign activities and what constitutes an “alert” before the run. Report
the known-case denominator and case mix; passing easier transport/security cases
cannot be described as 90% accuracy on the three requested human activities.
These PRD point targets and a 20–40-case test set are demo goals, not a statistical
guarantee of a production error rate.

Proposed **demo** gate, to approve before the run: all planned cells accounted for,
all failures retained, correct identities/provenance/readback, zero automatic lock
actions, and zero interaction/security warnings from proximity-only trials without
digital evidence. Record the exact counts even when a gate fails. This gate supports
a bounded supervised demo of the measured capability only. It cannot make the
current presence detector a three-class interaction recognizer.

Before any deployment claim, the team must agree operational limits for benign
false-warning rate, interaction miss rate, availability and latency; justify sample
size using those limits; evaluate an unchanged version on sufficient held-out
volunteers, sites and conditions; and show confidence bounds meeting the limits.
Measure authorized-use nuisance alerts and human review outcomes too. If the product
still cannot distinguish the three activities, state that limitation prominently
and keep the requirement open. More detectors or a passed transport test do not
substitute for this evidence.

No result, enrollment, armed session, camera capture or deployment was performed
by creating this protocol.
