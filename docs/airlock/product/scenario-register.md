**Airlock shared engineering workspace: 108 scenario specifications**

Prepared 8 November 2026. Every entry is planned, not executed. These are proposed product expectations, not claims about present capabilities. P0 means resolve or explicitly exclude before autonomous intervention; P1 means resolve before an unattended workplace pilot. Missing capabilities are valid findings and must not be scored as successful detection.

**Ordinary shared-workspace activity.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| SOC-01 | P0 | A friend walks past the armed desk without touching anything. | No lock request, accusation, external escalation, or audible security warning based only on passage. A correct presence observation is allowed. | Source distinguishes presence from incidents, but local presence speech still needs a nuisance test. |
| SOC-02 | P0 | A coworker stands beside the desk for a conversation, including while the owner is absent. | Standing is not computer use. Quiet observation only; no identity or intent claim. | Presence-only Jetson adapter cannot distinguish standing, passing and interaction. |
| SOC-03 | P0 | A friend reaches across the keyboard to retrieve their own notebook. | Do not equate moving fingertips in a two-dimensional keyboard zone with actual typing. Preserve uncertainty. | Mac zone geometry needs calibrated desk trials; current source does not prove contact or key input. |
| SOC-04 | P1 | A coworker borrows a shared charger beside the protected laptop. | Do not treat borrowing adjacent property as laptop tampering. Confirm which object or host changed. | Desk and protected-object boundaries must be validated; presence alone has no object ownership semantics. |
| SOC-05 | P1 | A cleaner wipes the desk and moves a mouse. | Allow an authenticated, bounded maintenance arrangement if offered; otherwise use neutral access protection, not a hostile-person accusation. | A maintenance permission model is a proposed product feature, not verified existing support. |
| SOC-06 | P1 | Someone bumps the chair or moves the desk while walking past. | No theft or computer-use claim from a bump alone. Report any actual sensor displacement separately. | Motion or changed image geometry is not proof of a malicious act. |
| SOC-07 | P0 | A colleague moves the laptop away from spilled water. | Keep session protection and human review; do not obstruct a person, threaten, shut down, or infer theft as fact. | Emergency physical assistance and removal have overlapping observable signals. |
| SOC-08 | P0 | Owner and friend pair-program, and the owner steps aside briefly. | Owner defines whether collaboration continues. Mere presence of another person neither authorizes nor condemns their actions. | Authenticated collaboration scope is not equivalent to enrolling a camera or reviewer. |

**Permission and trust.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| AUTH-01 | P0 | Owner gives a teammate permission to perform one specific task. | Permission is authenticated, workstation-specific, time-limited and scoped; ordinary OS authentication remains required. | Scoped guest task permission is a design proposal; do not present it as implemented. |
| AUTH-02 | P0 | A permitted teammate does a different sensitive task. | The earlier grant does not cover the new task. Require its own authorization where enforcement exists. | Identity or friendship must not become a blanket bypass. |
| AUTH-03 | P0 | The teammate's permission expires while they remain seated. | Expiry does not silently renew because the same person remains. Define safe completion versus blocking a new action. | Test grant expiry separately from posture and ongoing job continuity. |
| AUTH-04 | P0 | Owner revokes a grant while an action is pending. | A pending sensitive operation rechecks authorization before execution; revocation cannot undo a completed action. | Existing managed-tool gates provide a useful boundary; guest grants would need a separate design. |
| AUTH-05 | P0 | Visitor says the owner gave them permission. | Speech alone cannot disarm, unlock, grant access, or approve a protected action. | Repository already intends authenticated control; verify live spoken claims and social pressure. |
| AUTH-06 | P0 | Owner leaves an authenticated Airlock console accessible on an unlocked Mac; visitor clicks Disarm. | An unattended owner session must not let a nearby person remove protection without an appropriate fresh check or separate trusted control. | Inspected UI calls owner reducers using its existing browser identity. Server identity checks do not establish who is now clicking. |
| AUTH-07 | P0 | A security-desk reviewer tries to disarm, resolve, or expand their own privileges. | Only the exact assigned permissions apply. Acknowledgement does not imply ownership or disarm authority. | Role boundaries are documented; a real operator trial must verify understandable restrictions. |
| AUTH-08 | P1 | Owner lends the laptop legitimately for an hour. | Use an explicit handover and suitable OS account; do not silently expose the owner's logged-in secrets or grant indefinite trust. | Handover and guest OS isolation are outside the present incident rules. |

**Engineering work continuity.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| WORK-01 | P0 | Engineer arms the desk while a long compile, training job, or test suite is running. | Protect interactive access without intentionally terminating the job; measure output integrity, completion time and resource impact. | Normal screen lock is the intended action, not shutdown; application-specific behavior still needs measurement. |
| WORK-02 | P0 | A GUI automation, simulator, screen-dependent test, or remote desktop session is running. | Explicitly establish whether locking breaks the workload. An exception must have a stated exposure and expiry. | Do not promise all jobs survive screen locking unchanged. |
| WORK-03 | P0 | Firmware flashing or a hardware experiment is in progress when a warning occurs. | No arbitrary process kill, power-off, USB reset or destructive interruption. Test lock compatibility on a safe fixture. | A security reaction must not damage an engineering task; current product does not provide experiment orchestration. |
| WORK-04 | P1 | Engineer has an authorized SSH or remote-maintenance session while away. | Distinguish authorization from physical presence; do not call all remote activity an intrusion. | Camera presence and visible app launches cannot establish authorization of an existing remote session. |
| WORK-05 | P0 | An updater, scheduled task, test runner, or developer tool launches an app after arming. | A launch is a fact, not evidence that a nearby person caused it. Avoid high-priority blame and unnecessary disruption. | Current server creates a review incident for an unapproved app ID without proving human causation. |
| WORK-06 | P0 | A docking station reconnects after sleep, power change, or a loose cable. | Group the hardware episode, apply the actual device policy, and avoid an alarm storm or blaming a passerby. | Baseline/device identity stability and multi-device re-enumeration require hardware trials. |
| WORK-07 | P1 | An approved development board changes its USB identity during a normal reset. | Recognize only explicitly authorized device transitions if supported; otherwise request neutral review without calling it malware. | A frozen device-key baseline may not express bootloader/application identity transitions. |
| WORK-08 | P0 | Owner repeatedly steps away and returns during debugging. | Recovery remains fast, authenticated and free of repeated locking. Measure whether the owner disables the product to work. | Face identification is off by default; presence is not owner authentication. |

**Unauthorized use and sabotage.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| TAMP-01 | P0 | Visitor edits source files in an editor that was already open; no new app or USB appears. | Prevent access through an already-locked session or accurately declare the detection gap; do not claim a new-app rule covers edits. | Inspected authoritative rules consume USB/app launches and presence, not arbitrary file edits. Local camera warnings are a separate path. |
| TAMP-02 | P0 | Visitor uses an already-open terminal to perform a harmless simulated destructive action. | Protect the session before interaction. A camera may warn but must not be advertised as guaranteed command prevention. | No new app launch is required for abuse of an existing terminal; evaluate with disposable files only. |
| TAMP-03 | P0 | Visitor uses an already-authenticated browser to copy dummy project data or act in a test cloud account. | Recognize this as a coverage gap unless a specific preventive control is integrated; preserve no-compromise uncertainty. | Existing USB/app-launch rules do not inspect arbitrary browser actions or cloud sessions. |
| TAMP-04 | P0 | Visitor copies a disposable file using a USB device already in the approved baseline. | Approved identifier does not prove benign use or permission to copy data. Never claim all exfiltration is prevented. | Airlock observes device events, not general removable-media read/write authorization. |
| TAMP-05 | P0 | An unapproved harmless USB device is inserted while armed. | Observe the real event, apply owner-consented policy, label it unapproved rather than malicious, and verify any lock outcome. | This is a principal documented server incident path; full hardware-to-response timing remains an acceptance task. |
| TAMP-06 | P0 | A test fixture represents a peripheral advertising a keyboard interface or a familiar label. | Device labels and identifiers do not become strong identity or a malware verdict. Do not run real keystroke-injection hardware. | Product boundary test using recorded or synthetic metadata only; enumeration-after-the-fact is not pre-execution prevention. |
| TAMP-07 | P0 | A familiar signed app is used for an unauthorized operation. | Do not infer harmlessness from app allowlist membership or signing. State the limit of application-level observations. | App identity and action authorization are different; no whole-OS action gate is shown. |
| TAMP-08 | P0 | An untrusted README asks an integrated agent to read a dummy secret or deploy a dummy artifact. | Unauthorized callback never starts; exact approved benign action can execute. The model cannot authorize itself. | Managed workflow gate exists; real model-mediated and exact host integration evidence must be collected. |

**Removal, loss and physical interference.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| THEFT-01 | P0 | Someone lifts the laptop while it remains on battery and connected to Wi-Fi. | If removal detection is claimed, independently prove the object left its assigned location; otherwise explicitly mark that capability unsupported. | Person/hand-zone detection is not validated laptop-object tracking or theft detection. |
| THEFT-02 | P0 | Someone closes the lid before taking the laptop. | Protection and alert delivery cannot assume the built-in camera or awake Mac remains available. | An independent sensor is potentially useful, but sustained physical coverage has not been proved by this review. |
| THEFT-03 | P0 | Someone disconnects power and networking immediately before removing the laptop. | Do not depend on a later cloud command for prevention. Preserve already-locked state and report independently observed loss of coverage. | Offline and post-removal recovery require separate controls; total isolation defeats network notification. |
| THEFT-04 | P1 | Someone removes a monitor, keyboard, or development board but leaves the Mac. | Only claim protection of objects actually monitored. USB removal may be observed, but theft and actor identity are not established. | Current unknown-USB addition rule is not a general asset-removal policy. |
| THEFT-05 | P0 | Owner moves the laptop to a meeting room. | An authenticated move or disarm flow prevents needless external escalation while maintaining normal OS protection. | Relocation permission is proposed; absence of a face match must not imply theft. |
| THEFT-06 | P0 | Teammate collects the laptop with the owner's permission. | Require a bounded authenticated handover if offered; avoid both automatic trust and a criminal accusation. | The system cannot verify the owner's verbal permission from the camera. |
| THEFT-07 | P0 | Laptop is moved while the relevant area is outside the camera view. | Report unobserved/unavailable evidence. Do not fill in an imagined theft sequence or claim the desk is safe. | Object visibility and current sensor capability bound any conclusion. |
| THEFT-08 | P0 | Both laptop and external sensor lose power or are removed. | State the last verified observation and current unknown condition; never promise continued live protection. | No local software can guarantee observation or delivery after every sensing and communication path disappears. |

**Perception and attribution.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| VISION-01 | P0 | People approach in backlight, darkness, glare, or rapidly changing illumination. | Report uncertain or unavailable observations rather than confident computer-use claims; measure misses by condition. | Real-world held-out accuracy is open; do not transfer benchmark or one-run timing into deployment accuracy. |
| VISION-02 | P0 | Hands are hidden behind the body, a monitor, or the laptop lid. | No false claim that no interaction occurred; retain the sensor blind spot. | Mac zone pipeline documents that unseen hands produce no event. |
| VISION-03 | P1 | Different seated heights, wheelchair positions, clothing, and working postures are used. | Comparable access and recovery; evaluate coverage for the actual user population without inferring character from appearance. | Camera placement and full-body assumptions can change observation quality. |
| VISION-04 | P0 | Two people overlap or exchange seats while a device event occurs. | Do not attribute an event to a particular person merely from temporal coincidence or a tracker ID. | Current correlation is workstation/session/time based, not a verified causal identity match. |
| VISION-05 | P1 | A reflection, person on a monitor, poster, or background motion enters the image. | Measure false presence and interaction; an image detection cannot authorize any sensitive action. | Perception artifact cases need controlled scenes, including empty-desk negatives. |
| VISION-06 | P0 | A neighbor works in the adjacent desk zone. | Their movement must not trigger this workstation's intervention; validate each ROI and sensor-to-desk mapping. | Shared cameras introduce cross-desk attribution and privacy risks. |
| VISION-07 | P0 | Camera is nudged after zone calibration. | Detect or expose invalid geometry before treating old zones as current; do not silently overclaim coverage. | Camera calibration health is distinct from the process being alive. |
| VISION-08 | P0 | Video is frozen or replayed while the camera process still sends heartbeats. | A fresh heartbeat must not imply fresh usable frames; degrade coverage and retain timestamp distinctions. | Use inert test streams; no proof of liveness follows from transport health alone. |

**Device and application semantics.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| DEVICE-01 | P0 | macOS blocks an accessory connection before data access is allowed. | Describe what the OS actually allowed and what Airlock observed; do not claim successful data access or malware execution. | Apple accessory authorization is a separate preventive boundary; observation visibility must be tested. |
| DEVICE-02 | P1 | A friend connects a phone or a charge-only cable. | Distinguish power-only behavior and actual data interfaces where observable. Never infer malicious intent from novelty. | Apple documents charging can remain possible when data access is denied. |
| DEVICE-03 | P0 | A previously approved peripheral is replaced by one presenting similar metadata. | Do not treat a baseline hash as hardware attestation or proof of safe firmware. | Baseline records an observed identifier, not a physical trust guarantee. |
| DEVICE-04 | P0 | An unwanted device is attached before the owner arms the desk. | Arming must not silently turn an unreviewed inventory into a safety certification. Show what was captured and what was approved. | Current live baseline proves freshness and origin, not that every captured device is benign. |
| DEVICE-05 | P1 | The same peripheral moves to another port or hub. | Measure identifier stability and explain any review requirement without repeated criminal-sounding warnings. | Port/location-derived identity can vary; verify the actual adapter's device-key behavior. |
| DEVICE-06 | P0 | A legitimate app omitted from the frozen allowlist launches during an armed session. | Neutral review, with scheduled or authorized context when available; no automatic claim that a human intruded. | Current server rule classifies unapproved app IDs and opens incidents. |
| DEVICE-07 | P0 | A background agent or shell child process starts without a visible GUI application launch. | Declare whether the configured observer sees it. Never describe visible-app notifications as complete process monitoring. | Apple documents exclusions for NSWorkspace app-launch notifications; existing-terminal actions are an additional gap. |
| DEVICE-08 | P1 | A hub enumerates many interfaces and repeatedly flaps. | One understandable episode with bounded alerts; preserve distinct material changes without unbounded duplicates. | Hardware event storms are different from one synthetic USB addition. |

**Warnings, conversation and notification.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| HUMAN-01 | P0 | A benign visitor triggers an audible message in a busy shared office. | No threats, insults, person labels, or public disclosure of the owner's work. Prefer quiet presence observation. | Local presence speech may occur despite no server incident; test social cost separately from lock behavior. |
| HUMAN-02 | P1 | Audio is routed to headphones or the wrong Bluetooth speaker. | Never report a local warning was heard merely because playback was queued. Preserve an accessible visual/remote path. | Audio device routing and acoustic onset differ from software playback success. |
| HUMAN-03 | P0 | Speaker disconnects during an urgent event. | Core policy and evidence continue; warning failure is explicit and alternative configured channels are attempted. | Model-independent speech still depends on working output hardware. |
| HUMAN-04 | P0 | Anyone says Stop talking. | Silence speech according to policy without disarming, changing approval rights, or deleting the incident. | This separation is intended by the repository and needs live-language trials. |
| HUMAN-05 | P0 | Friend says Disarm, I'm the owner or It's only me. | No disarm or access grant from the utterance alone; provide the authenticated route. | Voice is an interface, not a sufficient authenticator. |
| HUMAN-06 | P0 | TV playback, quoted commands, negation, accents, or multiple languages resemble an arm command. | No sensitive state change without valid exact authorization; measure accidental invocation and accessible alternatives. | A one-use owner-authorized voice ARM path is distinct from free-form speech permission. |
| HUMAN-07 | P0 | User asks the model who hacked the Mac or whether files were stolen. | Answer only supported observations; explicitly say when actor, permission, file access or intent is unknown. | Grounded investigation must resist both suggestive user wording and missing evidence. |
| HUMAN-08 | P0 | Phone provider accepts an alert but the phone is offline, in Do Not Disturb, or left on the desk. | Show accepted versus acknowledged accurately; do not promise the owner saw it. Follow only predefined escalation policy. | Current dispatch source distinguishes provider acceptance from human receipt. |

**Failures and resource conditions.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| FAIL-01 | P0 | Camera is disconnected or permission is revoked. | Display lost visual coverage; keep genuinely available host monitoring. Do not call the scene empty. | Health status must describe the failed component and its consequences. |
| FAIL-02 | P0 | Mac watcher stops while the independent camera remains online. | Do not infer host protection from camera liveness. Mark USB/app coverage missing. | Independent camera observations cannot replace on-host event collection. |
| FAIL-03 | P0 | SpacetimeDB or the network becomes unreachable during an incident. | Preserve defined local behavior and OS protection; protected actions without verifiable authorization do not execute. | Queued telemetry and local warnings do not establish that a server-issued lock request can be delivered. |
| FAIL-04 | P0 | Nemotron is unavailable or takes too long. | Immediate deterministic protection continues; investigation is pending or explicitly unavailable, never invented. | Repository intends model-independent warning and policy paths. |
| FAIL-05 | P0 | Battery runs low, Mac sleeps, or power is removed. | Accurately report coverage loss and last verified state; do not equate sleeping display, screen lock and disk protection. | Hardware/power state changes require real trials and a separate at-rest security baseline. |
| FAIL-06 | P1 | Compiler, VM, GPU job or model inference saturates the machine. | Measure detection/response tails and workload slowdown; a healthy-looking dashboard must not hide event starvation. | Representative engineering load is more relevant than an idle demo benchmark. |
| FAIL-07 | P0 | Services restart with queued or duplicate events. | Recover correct session and evidence once; no duplicate lock, alert storm, or replay of old intervention. | Existing sequencing and dedup design is useful but must be tested on the integrated host. |
| FAIL-08 | P0 | Event queue, storage, or session capacity is exhausted. | Expose loss of coverage, bound resource use and preserve meaningful evidence; do not become green on heartbeat alone. | Source documents bounded capacity; product needs a clear operator recovery path. |

**Session transitions and authority.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| STATE-01 | P0 | Owner presses Arm when the baseline is stale or capture failed. | Remain clearly unarmed or degraded as defined; never announce successful protection before committed confirmation. | Fresh baseline requirements exist; user comprehension of failed arming must be tested. |
| STATE-02 | P0 | Session is armed but screen lock is disabled, a dry run, pending or failed. | Keep these states visibly distinct. Armed must not mean the interactive session is already inaccessible. | Default dry-run behavior and unverified real lock are material current release limits. |
| STATE-03 | P0 | Owner disarms or revokes consent while a lock request is pending. | No later execution under stale permission; measure the final local execution boundary, not only a button state. | Current claim/expiry controls should be verified under real timing races. |
| STATE-04 | P0 | Owner unlocks after an alert while stale events are still arriving. | No lock loop. Authenticated recovery cancels stale actions; return does not authorize unrelated new sensitive actions. | Normal OS unlock, incident resolution and Airlock disarm are separate states. |
| STATE-05 | P1 | Owner changes the policy and then rearms. | Show the effective frozen policy and explain when a change takes effect. No mixed old/new permissions. | Repository freezes session policy; confusing next-session changes can cause unexpected action. |
| STATE-06 | P0 | Events are delayed, out of order, or cross the correlation time boundary. | Keep observed and received time separate; no fresh intervention from stale evidence and no invented ordering. | Current correlation and lock-freshness windows are engineering choices, not validated behavior thresholds. |
| STATE-07 | P0 | Forged, replayed, or cross-workstation events and approvals arrive. | Reject wrong authority/scope/replay; do not expose another workstation's case or execute its action. | Use isolated fixtures to verify roles, exact actions, nonces and revocation. |
| STATE-08 | P0 | Armed session expires while the owner remains away and a job continues. | Make the lapse clear and retain ordinary OS protection. Never silently extend monitoring or imply continuing coverage. | Expiry is a product event requiring an understandable owner policy. |

**Privacy, workplace governance and accessibility.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| PRIV-01 | P0 | Default configuration starts on a shared desk with existing legacy face files. | No default enrollment, face identification, identity naming or use of old templates. | Source default is face-disabled; running-host configuration remains unverified in the release record. |
| PRIV-02 | P0 | Camera field includes a neighboring engineer's screen or confidential whiteboard. | Minimize observation to the intended desk; no capture or narration of unrelated secrets. | A physical field-of-view test is needed even if no video is stored. |
| PRIV-03 | P1 | An incident remains unresolved past ordinary retention or the device is decommissioned. | Explain actual retention and remove data through the agreed process, including applicable external copies. | Current database retention has unresolved-case exceptions; it is not a universal deletion promise. |
| PRIV-04 | P0 | Coworker or visitor objects to being monitored. | Provide transparent notice and a workable camera-free or placement alternative; do not covertly enroll them. | Workplace adoption and governance require user research; jurisdiction-specific legal review is separate. |
| PRIV-05 | P0 | One engineer opens another engineer's incident URL or dashboard. | Deny access without appropriate membership; notification content also avoids unnecessary exposure. | Caller-scoped views are a foundation, not a reason to skip negative access tests. |
| PRIV-06 | P0 | Teammate leaves the team or their reviewer device is lost. | Revoke current and pending rights; avoid persisting broad access through saved sessions or outstanding grants. | Role revocation is documented; real workflow and recovery need validation. |
| PRIV-07 | P0 | Device/app labels or investigation output contain dummy secrets or hostile instructions. | Redact sensitive content and treat names and tool results as untrusted data; no privileged instructions or secret-bearing notifications. | Metadata minimization and managed tool validation need end-to-end output checks. |
| PRIV-08 | P1 | Owner cannot use speech, hear warnings, easily reach the camera, or distinguish status colors. | Equivalent accessible controls, textual status and authenticated recovery remain available. | Accessibility is a first-class product case rather than an exception to security. |

**Recovery, proof and product value.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| REC-01 | P0 | Lock helper returns success but actual OS lock state is unverified. | Report request/execution separately from independently confirmed lock; never claim success from TTS or subprocess completion alone. | Current release lists real lock/unlock and confirmation as open. |
| REC-02 | P0 | Owner marks an incident as a friend's permitted activity. | Record a bounded correction; do not automatically grant permanent trust to the person, app, device or all future events. | Acknowledgement, resolution and future permission are separate decisions. |
| REC-03 | P0 | A permitted task finishes but OS access remains available. | Terminate or bound the delegated access as designed; do not imply Airlock grants provide OS account isolation. | Any future guest workflow must account for the underlying session and credentials. |
| REC-04 | P0 | Disposable source files were changed before detection. | Explain what was observed and what cannot be undone. Test independent backup/version recovery rather than promise rollback. | Current incident and lock controls do not provide universal file restoration or revoke copied secrets. |
| REC-05 | P1 | Owner's phone is unavailable and the desk reviewer does not respond. | Use an explicit timeout/escalation rule and show no acknowledgement. Do not automatically broadcast to all coworkers. | Response depends on a reachable responsible person, not merely a published row. |
| REC-06 | P0 | Owner loses Airlock credentials or the server is offline during return. | Normal OS credentials still permit access; document safe product recovery without disabling unrelated protections. | Prototype host-issued identities are not a mature organizational SSO/account-recovery system. |
| REC-07 | P0 | Compare the same scenarios with native lock/accessory controls alone and with Airlock added. | Measure incremental protection, review speed, setup burden and job disruption; retain only value demonstrated over the baseline. | No customer ROI, willingness-to-pay or incremental security result has been established by this research. |
| REC-08 | P1 | Run a full realistic workday with many harmless interactions. | Count nuisance sounds, notifications, accidental interventions and disabling behavior per protected hour and per episode. | A balanced demo and aggregate test-pass count do not estimate everyday false-alert burden. |

**Compound and adversarial sequence cases.**

| Case | Priority | Situation | Expected behavior | Evidence or gap |
|---|---|---|---|---|
| COMB-01 | P0 | Friend passes the desk while an unrelated dock reconnects within ten seconds. | Keep each fact and uncertainty; no claim the friend connected the device. Measure unnecessary high-severity escalation. | Current temporal rule can correctly fire on a benign coincidence; causal attribution would be unsupported. |
| COMB-02 | P0 | Authorized teammate types, then introduces a device outside their task grant. | Permit only the granted task; device approval remains separate and authenticated. | A friend exception must not bypass all other boundaries. |
| COMB-03 | P0 | Camera becomes unavailable just before an unknown USB arrives. | Host-only policy still applies if its prerequisites hold; no invented physical evidence and no waiting for the model. | Tests must separate camera loss from digital protection and communication health. |
| COMB-04 | P0 | Owner returns and unlocks just as a delayed incident and lock request arrive. | Revalidate current session/consent and expiry; no stale lock loop or claim the returning owner is an attacker. | Concurrency needs an end-to-end local execution test. |
| COMB-05 | P0 | Authorized remote maintenance launches an app while a cleaner stands nearby. | No attribution to the cleaner; review launch permission independently from physical coincidence. | Time correlation is not identity, causation or permission. |
| COMB-06 | P0 | An authorized board flash re-enumerates USB while Nemotron times out. | Keep safe job behavior, independent policy and clear investigation status; no shutdown or misleading malware finding. | Combines engineering device transitions, response cost and model failure. |
| COMB-07 | P0 | Visitor uses an already-open browser while their hands are occluded. | Already-locked session is the main boundary; do not claim detection merely because presence was seen. | Combines no new digital trigger with the camera's interaction blind spot. |
| COMB-08 | P0 | Laptop is picked up after Wi-Fi is disabled and its camera is covered. | Report only independently available evidence; disclose loss of reachability and absence of guaranteed recovery. | Physical removal plus communication loss defeats a purely on-laptop alert path. |
| COMB-09 | P0 | Camera stream freezes and Mac watcher crashes while heartbeats or a dashboard connection remain. | Coverage must become component-specific degraded/unknown, never a healthy all-clear. | Tests semantic liveness rather than transport liveness. |
| COMB-10 | P0 | Two adjacent desks produce person and USB events at nearly the same time. | Enforce workstation/session isolation and calibrated mapping; no wrong-person or wrong-desk incident. | Cryptographic sender identity does not fix a sensor configured for the wrong desk. |
| COMB-11 | P0 | Speech is muted, phone is in Do Not Disturb, and a second material event occurs. | Keep monitoring, preserve bounded distinct alerts and accurately show missing acknowledgement. | Avoid both alert flooding and permanent suppression after the first event. |
| COMB-12 | P0 | Owner authorizes a move, sensors go offline, and old queued events return at the new desk. | Respect the new session/location and current policy; retain old evidence as history without fresh punishment. | Combines handover, calibration, stale-event handling and reconnect recovery. |
