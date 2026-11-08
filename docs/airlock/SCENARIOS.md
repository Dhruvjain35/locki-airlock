# Workspace scenarios

Status: planned. No case in this document has a recorded physical test result.

Each row defines the situation, required outcome and remaining work. Use the
[test plan](TEST_PLAN.md) for setup, result recording and the first acceptance runs.
P0 cases must be resolved or the affected capability excluded before autonomous
intervention. P1 cases must be resolved before an unattended workplace pilot.

## Ordinary shared-workspace activity

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| SOC-01 | P0 | A friend walks past the armed desk without touching anything. | No lock request, accusation, external escalation, or audible security warning based only on passage. A correct presence observation is allowed. | Remove presence-only security speech from the local path; check server, speaker and UI together. |
| SOC-02 | P0 | A coworker stands beside the desk for a conversation, including while the owner is absent. | Standing is not computer use. Quiet observation only; no identity or intent claim. | Keep proximity quiet regardless of familiarity; trial long conversations as well as pass-bys. |
| SOC-03 | P0 | A friend reaches across the keyboard to retrieve their own notebook. | Do not equate moving fingertips in a two-dimensional keyboard zone with actual typing. Preserve uncertainty. | Treat hand-zone overlap as uncertain activity; test reaching for objects beside the keyboard. |
| SOC-04 | P1 | A coworker borrows a shared charger beside the protected laptop. | Do not treat borrowing adjacent property as laptop tampering. Confirm which object or host changed. | Scope observation to the protected workstation; check shared charger behavior against actual USB events. |
| SOC-05 | P1 | A cleaner wipes the desk and moves a mouse. | Allow an authenticated, bounded maintenance arrangement if offered; otherwise use neutral access protection, not a hostile-person accusation. | Define cleaning/handover procedure and verify incidental input cannot bypass the native lock. |
| SOC-06 | P1 | Someone bumps the chair or moves the desk while walking past. | No theft or computer-use claim from a bump alone. Report any actual sensor displacement separately. | Check camera calibration and device reconnect noise after a bump; avoid inferring tampering. |
| SOC-07 | P0 | A colleague moves the laptop away from spilled water. | Keep session protection and human review; do not obstruct a person, threaten, shut down, or infer theft as fact. | Provide authenticated recovery after a legitimate emergency move; do not obstruct physical assistance. |
| SOC-08 | P0 | Owner and friend pair-program, and the owner steps aside briefly. | Owner defines whether collaboration continues. Mere presence of another person neither authorizes nor condemns their actions. | Document explicit handover and rearming; brief owner absence must not become a friend exception. |

## Permission and trust

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| AUTH-01 | P0 | Owner gives a teammate permission to perform one specific task. | Permission is authenticated, workstation-specific, time-limited and scoped; ordinary OS authentication remains required. | Use explicit owner handover and suitable OS access today; scoped task grants need separate design. |
| AUTH-02 | P0 | A permitted teammate does a different sensitive task. | The earlier grant does not cover the new task. Require its own authorization where enforcement exists. | Enforce task scope at the execution boundary if grants are built; do not treat friendship as authorization. |
| AUTH-03 | P0 | The teammate's permission expires while they remain seated. | Expiry does not silently renew because the same person remains. Define safe completion versus blocking a new action. | Deny new actions after grant expiry; define treatment of already-started work without arbitrary termination. |
| AUTH-04 | P0 | Owner revokes a grant while an action is pending. | If revocation commits before final execution authorization, deny the pending action. If execution already started irreversibly, report that boundary without promising rollback or killing unrelated jobs. | Exercise revocation before and after final authorization; include delayed voice results and pending callbacks. |
| AUTH-05 | P0 | Visitor says the owner gave them permission. | Speech alone cannot disarm, unlock, grant access, or approve a protected action. | Reject verbal permission claims; direct the visitor to the authenticated owner workflow. |
| AUTH-06 | P0 | Owner leaves an authenticated Airlock console accessible on an unlocked Mac; visitor clicks Disarm. | An unattended owner session must not let a nearby person remove protection without an appropriate fresh check or separate trusted control. | Test the saved owner session; lock before leaving and decide which controls require fresh authentication. |
| AUTH-07 | P0 | A security-desk reviewer tries to disarm, resolve, or expand their own privileges. | Only the exact assigned permissions apply. Acknowledgement does not imply ownership or disarm authority. | Test each reviewer action against its assigned role, including attempted self-enrollment and disarm. |
| AUTH-08 | P1 | Owner lends the laptop legitimately for an hour. | Use an explicit handover and suitable OS account; do not silently expose the owner's logged-in secrets or grant indefinite trust. | Document lending with a suitable OS account, handover duration and owner return procedure. |

## Engineering work continuity

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| WORK-01 | P0 | Engineer arms the desk while a long compile, training job, or test suite is running. | Protect interactive access without intentionally terminating the job; measure output integrity, completion time and resource impact. | Run a representative job through ARM, native lock and unlock; compare output, duration and event capture. |
| WORK-02 | P0 | A GUI automation, simulator, screen-dependent test, or remote desktop session is running. | Explicitly establish whether locking breaks the workload. An exception must have a stated exposure and expiry. | Publish a workload compatibility matrix; explicitly bound any workflow that must stay unlocked. |
| WORK-03 | P0 | Firmware flashing or a hardware experiment is in progress when a warning occurs. | No arbitrary process kill, power-off, USB reset or destructive interruption. Test lock compatibility on a safe fixture. | Test on a safe hardware fixture; response must not reset USB, cut power or terminate the flashing process. |
| WORK-04 | P1 | Engineer has an authorized SSH or remote-maintenance session while away. | Distinguish authorization from physical presence; do not call all remote activity an intrusion. | Exercise approved remote access while the desk is empty; separate app policy from physical presence. |
| WORK-05 | P0 | An updater, scheduled task, test runner, or developer tool launches an app after arming. | A launch is a fact, not evidence that a nearby person caused it. Avoid high-priority blame and unnecessary disruption. | Run expected updaters and launchers against the frozen app policy; use factual review language. |
| WORK-06 | P0 | A docking station reconnects after sleep, power change, or a loose cable. | Group the hardware episode, apply the actual device policy, and avoid an alarm storm or blaming a passerby. | Record one physical dock reconnect and all emitted events; group repeats without hiding new devices. |
| WORK-07 | P1 | An approved development board changes its USB identity during a normal reset. | Recognize only explicitly authorized device transitions if supported; otherwise request neutral review without calling it malware. | Capture actual development-board identities across reset/bootloader modes before defining approval behavior. |
| WORK-08 | P0 | Owner repeatedly steps away and returns during debugging. | Recovery remains fast, authenticated and free of repeated locking. Measure whether the owner disables the product to work. | Repeat arm/return/disarm during debugging; measure effort, stale events and accidental responses. |

## Unauthorized use and sabotage

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| TAMP-01 | P0 | Visitor edits source files in an editor that was already open; no new app or USB appears. | Prevent access through an already-locked session or accurately declare the detection gap; do not claim a new-app rule covers edits. | Use native screen lock as the access boundary; demonstrate the existing-editor observation gap with disposable files. |
| TAMP-02 | P0 | Visitor uses an already-open terminal to perform a harmless simulated destructive action. | Protect the session before interaction. A camera may warn but must not be advertised as guaranteed command prevention. | Keep the terminal behind the OS lock; use an inert fixture to document the lack of command-level coverage. |
| TAMP-03 | P0 | Visitor uses an already-authenticated browser to copy dummy project data or act in a test cloud account. | Recognize this as a coverage gap unless a specific preventive control is integrated; preserve no-compromise uncertainty. | Protect the browser session with OS access controls; test only dummy data and disposable accounts. |
| TAMP-04 | P0 | Visitor copies a disposable file using a USB device already in the approved baseline. | Approved identifier does not prove benign use or permission to copy data. Never claim all exfiltration is prevented. | Define native removable-media policy; do not claim a baseline device approval prevents file copying. |
| TAMP-05 | P0 | An unapproved harmless USB device is inserted while armed. | Observe the real event, apply owner-consented policy, label it unapproved rather than malicious, and verify any lock outcome. | Trial a harmless unapproved accessory; measure OS authorization, event receipt, response and confirmed lock separately. |
| TAMP-06 | P0 | A test fixture represents a peripheral advertising a keyboard interface or a familiar label. | Device labels and identifiers do not become strong identity or a malware verdict. Do not run real keystroke-injection hardware. | Use inert interface fixtures; document that device labels and hashes are not firmware attestation. |
| TAMP-07 | P0 | A familiar signed app is used for an unauthorized operation. | Do not infer harmlessness from app allowlist membership or signing. State the limit of application-level observations. | Declare the app observer's limits; an approved executable cannot authorize every operation it performs. |
| TAMP-08 | P0 | An untrusted README asks an integrated agent to read a dummy secret or deploy a dummy artifact. | Unauthorized callback never starts; exact approved benign action can execute. The model cannot authorize itself. | Use a malicious-instruction fixture with dummy data; assert denied managed callbacks never start. |

## Removal, loss and physical interference

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| THEFT-01 | P0 | Someone lifts the laptop while it remains on battery and connected to Wi-Fi. | If removal detection is claimed, independently prove the object left its assigned location; otherwise explicitly mark that capability unsupported. | Keep removal detection unclaimed until an object-movement detector is validated independently of presence. |
| THEFT-02 | P0 | Someone closes the lid before taking the laptop. | Protection and alert delivery cannot assume the built-in camera or awake Mac remains available. | Close the lid under controlled conditions; record which independent observations and network paths survive. |
| THEFT-03 | P0 | Someone disconnects power and networking immediately before removing the laptop. | Do not depend on a later cloud command for prevention. Preserve already-locked state and report independently observed loss of coverage. | Trace shared power/network dependencies and test controlled loss; report the last genuinely available observation. |
| THEFT-04 | P1 | Someone removes a monitor, keyboard, or development board but leaves the Mac. | Only claim protection of objects actually monitored. USB removal may be observed, but theft and actor identity are not established. | Inventory which assets are actually observable; peripheral disappearance is not proof of theft. |
| THEFT-05 | P0 | Owner moves the laptop to a meeting room. | An authenticated move or disarm flow prevents needless external escalation while maintaining normal OS protection. | Provide an owner relocation procedure that ends the old session and recalibrates the new desk. |
| THEFT-06 | P0 | Teammate collects the laptop with the owner's permission. | Require a bounded authenticated handover if offered; avoid both automatic trust and a criminal accusation. | Use explicit handover; do not equate a familiar person with permission to remove equipment. |
| THEFT-07 | P0 | Laptop is moved while the relevant area is outside the camera view. | Report unobserved/unavailable evidence. Do not fill in an imagined theft sequence or claim the desk is safe. | Mark blind regions and disclose them; outside-view removal cannot be counted as detected. |
| THEFT-08 | P0 | Both laptop and external sensor lose power or are removed. | State the last verified observation and current unknown condition; never promise continued live protection. | Test loss of both host and observer; retain last-known status without claiming live protection or recovery. |

## Perception and attribution

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| VISION-01 | P0 | People approach in backlight, darkness, glare, or rapidly changing illumination. | Report uncertain or unavailable observations rather than confident computer-use claims; measure misses by condition. | Evaluate actual lighting conditions on held-out episodes; degraded images must not become an all-clear. |
| VISION-02 | P0 | Hands are hidden behind the body, a monitor, or the laptop lid. | No false claim that no interaction occurred; retain the sensor blind spot. | Test occlusion from each claimed work position; presence does not establish typing or touching. |
| VISION-03 | P1 | Different seated heights, wheelchair positions, clothing, and working postures are used. | Comparable access and recovery; evaluate coverage for the actual user population without inferring character from appearance. | Recruit representative consenting participants and placements; measure coverage rather than infer intent from appearance. |
| VISION-04 | P0 | Two people overlap or exchange seats while a device event occurs. | Do not attribute an event to a particular person merely from temporal coincidence or a tracker ID. | Stage overlapping people with unrelated events; preserve uncertainty about who performed the action. |
| VISION-05 | P1 | A reflection, person on a monitor, poster, or background motion enters the image. | Measure false presence and interaction; an image detection cannot authorize any sensitive action. | Use controlled reflections, posters and empty-desk scenes to measure false observations. |
| VISION-06 | P0 | A neighbor works in the adjacent desk zone. | Their movement must not trigger this workstation's intervention; validate each ROI and sensor-to-desk mapping. | Validate desk-to-sensor mapping with a busy neighboring desk before shared-space use. |
| VISION-07 | P0 | Camera is nudged after zone calibration. | Detect or expose invalid geometry before treating old zones as current; do not silently overclaim coverage. | Define how a moved camera invalidates calibration; show missing coverage until geometry is checked again. |
| VISION-08 | P0 | Video is frozen or replayed while the camera process still sends heartbeats. | A fresh heartbeat must not imply fresh usable frames; degrade coverage and retain timestamp distinctions. | Inject a frozen test stream while heartbeats continue; measure frame freshness independently. |

## Device and application semantics

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| DEVICE-01 | P0 | macOS blocks an accessory connection before data access is allowed. | Describe what the OS actually allowed and what Airlock observed; do not claim successful data access or malware execution. | Test accessory authorization on the actual Mac configuration; distinguish connection from allowed data access. |
| DEVICE-02 | P1 | A friend connects a phone or a charge-only cable. | Distinguish power-only behavior and actual data interfaces where observable. Never infer malicious intent from novelty. | Trial charge-only and data-capable connections separately; record what the observer can actually distinguish. |
| DEVICE-03 | P0 | A previously approved peripheral is replaced by one presenting similar metadata. | Do not treat a baseline hash as hardware attestation or proof of safe firmware. | Use inert duplicate-metadata fixtures; do not upgrade a device key into an assurance claim. |
| DEVICE-04 | P0 | An unwanted device is attached before the owner arms the desk. | Arming must not silently turn an unreviewed inventory into a safety certification. Show what was captured and what was approved. | Show the captured inventory to the owner; separate capture freshness from explicit approval. |
| DEVICE-05 | P1 | The same peripheral moves to another port or hub. | Measure identifier stability and explain any review requirement without repeated criminal-sounding warnings. | Move the same peripheral across available ports/hubs and check identifier stability and review burden. |
| DEVICE-06 | P0 | A legitimate app omitted from the frozen allowlist launches during an armed session. | Neutral review, with scheduled or authorized context when available; no automatic claim that a human intruded. | Run a legitimate omitted app; review the policy mismatch without attributing it to an intruder. |
| DEVICE-07 | P0 | A background agent or shell child process starts without a visible GUI application launch. | Declare whether the configured observer sees it. Never describe visible-app notifications as complete process monitoring. | Document visible-app notification exclusions using background and existing-shell fixtures. |
| DEVICE-08 | P1 | A hub enumerates many interfaces and repeatedly flaps. | One understandable episode with bounded alerts; preserve distinct material changes without unbounded duplicates. | Exercise a harmless hub event storm and identify duplicate runtime observers; bound notifications per episode. |

## Warnings, conversation and notification

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| HUMAN-01 | P0 | A benign visitor triggers an audible message in a busy shared office. | No threats, insults, person labels, or public disclosure of the owner's work. Prefer quiet presence observation. | Review every spoken line and status label; test the no-template presence path and duplicate warning consumers. |
| HUMAN-02 | P1 | Audio is routed to headphones or the wrong Bluetooth speaker. | Never report a local warning was heard merely because playback was queued. Preserve an accessible visual/remote path. | Test actual output routing and acoustic onset; preserve a readable alternative when audio goes elsewhere. |
| HUMAN-03 | P0 | Speaker disconnects during an urgent event. | Core policy and evidence continue; warning failure is explicit and alternative configured channels are attempted. | Disconnect the speaker during a trial; report output failure while other valid monitoring continues. |
| HUMAN-04 | P0 | Anyone says Stop talking. | Silence speech according to policy without disarming, changing approval rights, or deleting the incident. | Check that stop-talking only mutes speech, including while a real incident remains open. |
| HUMAN-05 | P0 | Friend says Disarm, I'm the owner or It's only me. | No disarm or access grant from the utterance alone; provide the authenticated route. | Try unapproved disarm phrases; verify no reducer permission or sensitive action follows. |
| HUMAN-06 | P0 | TV playback, quoted commands, negation, accents, or multiple languages resemble an arm command. | No sensitive state change without valid exact authorization; measure accidental invocation and accessible alternatives. | Test quoted, negated and delayed voice commands; end permission before ASR returns in one variant. |
| HUMAN-07 | P0 | User asks the model who hacked the Mac or whether files were stolen. | Answer only supported observations; explicitly say when actor, permission, file access or intent is unknown. | Ask leading questions about a harmless incident; require evidence-based answers with actor/intent unknown. |
| HUMAN-08 | P0 | Phone provider accepts an alert but the phone is offline, in Do Not Disturb, or left on the desk. | Show accepted versus acknowledged accurately; do not promise the owner saw it. Follow only predefined escalation policy. | Test offline and Do Not Disturb delivery; keep provider acceptance separate from owner acknowledgement. |

## Failures and resource conditions

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| FAIL-01 | P0 | Camera is disconnected or permission is revoked. | Display lost visual coverage; keep genuinely available host monitoring. Do not call the scene empty. | Physically disconnect the camera and revoke its permission; verify degraded state and fresh recovery. |
| FAIL-02 | P0 | Mac watcher stops while the independent camera remains online. | Do not infer host protection from camera liveness. Mark USB/app coverage missing. | Stop only the host watcher; camera health must not mask missing USB/app observations. |
| FAIL-03 | P0 | SpacetimeDB or the network becomes unreachable during an incident. | Preserve defined local behavior and OS protection; protected actions without verifiable authorization do not execute. | Disconnect the database path; test available local behavior and deny actions lacking verifiable authorization. |
| FAIL-04 | P0 | Nemotron is unavailable or takes too long. | Immediate deterministic protection continues; investigation is pending or explicitly unavailable, never invented. | Stop or delay the actual configured model service; immediate policy must not wait for an invented result. |
| FAIL-05 | P0 | Battery runs low, Mac sleeps, or power is removed. | Accurately report coverage loss and last verified state; do not equate sleeping display, screen lock and disk protection. | Test sleep and controlled power loss; trace whether Jetson power/network actually survive the Mac. |
| FAIL-06 | P1 | Compiler, VM, GPU job or model inference saturates the machine. | Measure detection/response tails and workload slowdown; a healthy-looking dashboard must not hide event starvation. | Run a representative compile/VM/inference load and measure latency tails, dropped events and slowdown. |
| FAIL-07 | P0 | Services restart with queued or duplicate events. | Recover correct session and evidence once; no duplicate lock, alert storm, or replay of old intervention. | Restart the integrated services with duplicates queued; inspect all consumers for repeated actions. |
| FAIL-08 | P0 | Event queue, storage, or session capacity is exhausted. | Expose loss of coverage, bound resource use and preserve meaningful evidence; do not become green on heartbeat alone. | Use bounded-capacity fixtures to verify visible loss, backpressure and operator recovery. |

## Session transitions and authority

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| STATE-01 | P0 | Owner presses Arm when the baseline is stale or capture failed. | Remain clearly unarmed or degraded as defined; never announce successful protection before committed confirmation. | Attempt ARM with failed/stale baseline capture; inspect committed state and spoken/UI confirmation. |
| STATE-02 | P0 | Session is armed but screen lock is disabled, a dry run, pending or failed. | Keep these states visibly distinct. Armed must not mean the interactive session is already inaccessible. | Check dry-run, pending, failed and confirmed states independently on the deployed build, including after native lock. |
| STATE-03 | P0 | Owner disarms or revokes consent while a lock request is pending. | Recheck consent at the final execution authorization boundary. A revocation committed before that boundary prevents execution; an already-started lock must be reported and recoverable. | Race consent revocation with final lock authorization; include a delayed voice result after permission ends. |
| STATE-04 | P0 | Owner unlocks after an alert while stale events are still arriving. | No lock loop. Authenticated recovery cancels stale actions; return does not authorize unrelated new sensitive actions. | Unlock while stale events arrive; verify normal recovery and no old-request lock loop. |
| STATE-05 | P1 | Owner changes the policy and then rearms. | Show the effective frozen policy and explain when a change takes effect. No mixed old/new permissions. | Change policy while armed and rearm; display the policy actually frozen for each session. |
| STATE-06 | P0 | Events are delayed, out of order, or cross the correlation time boundary. | Keep observed and received time separate; no fresh intervention from stale evidence and no invented ordering. | Exercise delayed/out-of-order events around correlation and freshness boundaries with controlled clocks. |
| STATE-07 | P0 | Forged, replayed, or cross-workstation events and approvals arrive. | Reject wrong authority/scope/replay; do not expose another workstation's case or execute its action. | Use isolated identities to test replay, wrong workstation/session, scope and revoked rights. |
| STATE-08 | P0 | Armed session expires while the owner remains away and a job continues. | Make the lapse clear and retain ordinary OS protection. Never silently extend monitoring or imply continuing coverage. | Let the session expire during a continuing job; show expiry while preserving ordinary OS protection. |

## Privacy, workplace governance and accessibility

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| PRIV-01 | P0 | Default configuration starts on a shared desk with existing legacy face files. | No default enrollment, face identification, identity naming or use of old templates. | Check the actual running revision/configuration with legacy templates present; no default identity enrollment or matching. |
| PRIV-02 | P0 | Camera field includes a neighboring engineer's screen or confidential whiteboard. | Minimize observation to the intended desk; no capture or narration of unrelated secrets. | Inspect the real field of view, including neighboring screens; reposition or mask unnecessary coverage. |
| PRIV-03 | P1 | An incident remains unresolved past ordinary retention or the device is decommissioned. | Explain actual retention and remove data through the agreed process, including applicable external copies. | Exercise retention and decommissioning, including unresolved incidents and external copies; document exceptions. |
| PRIV-04 | P0 | Coworker or visitor objects to being monitored. | Provide transparent notice and a workable camera-free or placement alternative; do not covertly enroll them. | Establish visible notice and a workable placement/camera-free alternative through workplace feedback. |
| PRIV-05 | P0 | One engineer opens another engineer's incident URL or dashboard. | Deny access without appropriate membership; notification content also avoids unnecessary exposure. | Attempt cross-workstation reads from a separate unprivileged account, including notification links. |
| PRIV-06 | P0 | Teammate leaves the team or their reviewer device is lost. | Revoke current and pending rights; avoid persisting broad access through saved sessions or outstanding grants. | Revoke a reviewer and test existing sessions, cached views and outstanding grants against the defined boundary. |
| PRIV-07 | P0 | Device/app labels or investigation output contain dummy secrets or hostile instructions. | Redact sensitive content and treat names and tool results as untrusted data; no privileged instructions or secret-bearing notifications. | Use dummy secrets and hostile metadata to check redaction, display escaping and model/tool authority. |
| PRIV-08 | P1 | Owner cannot use speech, hear warnings, easily reach the camera, or distinguish status colors. | Equivalent accessible controls, textual status and authenticated recovery remain available. | Test text and non-color status, keyboard controls and authenticated recovery without speech or hearing. |

## Recovery, proof and product value

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| REC-01 | P0 | Lock helper returns success but actual OS lock state is unverified. | Report request/execution separately from independently confirmed lock; never claim success from TTS or subprocess completion alone. | Compare helper completion with independently observed OS lock; do not substitute speech for confirmation. |
| REC-02 | P0 | Owner marks an incident as a friend's permitted activity. | Record a bounded correction; do not automatically grant permanent trust to the person, app, device or all future events. | Resolve a benign incident and check that it does not silently change future device, app or person permissions. |
| REC-03 | P0 | A permitted task finishes but OS access remains available. | Terminate or bound the delegated access as designed; do not imply Airlock grants provide OS account isolation. | End a handover and test remaining OS access; product grants alone do not isolate the owner's account. |
| REC-04 | P0 | Disposable source files were changed before detection. | Explain what was observed and what cannot be undone. Test independent backup/version recovery rather than promise rollback. | Change disposable files before an alert; verify separate backup/version recovery and disclose irreversible copying. |
| REC-05 | P1 | Owner's phone is unavailable and the desk reviewer does not respond. | Use an explicit timeout/escalation rule and show no acknowledgement. Do not automatically broadcast to all coworkers. | Exercise an unreachable owner and unresponsive reviewer; show timeout and apply only configured escalation. |
| REC-06 | P0 | Owner loses Airlock credentials or the server is offline during return. | Normal OS credentials still permit access; document safe product recovery without disabling unrelated protections. | Test owner return without server access; preserve native unlock and document product credential recovery. |
| REC-07 | P0 | Compare the same scenarios with native lock/accessory controls alone and with Airlock added. | Measure incremental protection, review speed, setup burden and job disruption; retain only value demonstrated over the baseline. | Compare identical tasks under native protections alone and with Airlock; measure added benefit and overhead. |
| REC-08 | P1 | Run a full realistic workday with many harmless interactions. | Count nuisance sounds, notifications, accidental interventions and disabling behavior per protected hour and per episode. | Observe a realistic workday; count nuisance interventions, recovery effort and whether people disable the system. |

## Compound and adversarial sequence cases

| Case | Priority | Situation | Required behavior | Work to do |
|---|---|---|---|---|
| COMB-01 | P0 | Friend passes the desk while an unrelated dock reconnects within ten seconds. | Keep each fact and uncertainty; no claim the friend connected the device. Measure unnecessary high-severity escalation. | Pair a passerby with a benign dock reconnect; inspect both temporal escalation and any nuisance lock. |
| COMB-02 | P0 | Authorized teammate types, then introduces a device outside their task grant. | Permit only the granted task; device approval remains separate and authenticated. | Separate permitted teammate activity from unapproved peripheral policy; no blanket friend exception. |
| COMB-03 | P0 | Camera becomes unavailable just before an unknown USB arrives. | Host-only policy still applies if its prerequisites hold; no invented physical evidence and no waiting for the model. | Disconnect camera before a harmless unknown USB event; use only genuine host evidence and available policy. |
| COMB-04 | P0 | Owner returns and unlocks just as a delayed incident and lock request arrive. | Revalidate current session/consent and expiry; no stale lock loop or claim the returning owner is an attacker. | Return/unlock during delayed lock delivery; verify current authority at the final execution boundary. |
| COMB-05 | P0 | Authorized remote maintenance launches an app while a cleaner stands nearby. | No attribution to the cleaner; review launch permission independently from physical coincidence. | Launch an app through authorized remote maintenance while a cleaner is nearby; prohibit causal attribution. |
| COMB-06 | P0 | An authorized board flash re-enumerates USB while Nemotron times out. | Keep safe job behavior, independent policy and clear investigation status; no shutdown or misleading malware finding. | Use a safe board-reset/flash fixture while the model times out; preserve job integrity and truthful policy state. |
| COMB-07 | P0 | Visitor uses an already-open browser while their hands are occluded. | Already-locked session is the main boundary; do not claim detection merely because presence was seen. | Use an existing browser with hands occluded; demonstrate the native lock boundary and observation gap. |
| COMB-08 | P0 | Laptop is picked up after Wi-Fi is disabled and its camera is covered. | Report only independently available evidence; disclose loss of reachability and absence of guaranteed recovery. | Stage controlled offline removal with dummy assets; report independent observations without promising recovery. |
| COMB-09 | P0 | Camera stream freezes and Mac watcher crashes while heartbeats or a dashboard connection remain. | Coverage must become component-specific degraded/unknown, never a healthy all-clear. | Freeze camera frames and stop the Mac watcher while connections remain; require component-specific loss indicators. |
| COMB-10 | P0 | Two adjacent desks produce person and USB events at nearly the same time. | Enforce workstation/session isolation and calibrated mapping; no wrong-person or wrong-desk incident. | Generate concurrent events at two desks; verify sensor mapping as well as workstation/session authorization. |
| COMB-11 | P0 | Speech is muted, phone is in Do Not Disturb, and a second material event occurs. | Keep monitoring, preserve bounded distinct alerts and accurately show missing acknowledgement. | Mute speech and withhold phone acknowledgement, then add a distinct event; preserve it without an alert storm. |
| COMB-12 | P0 | Owner authorizes a move, sensors go offline, and old queued events return at the new desk. | Respect the new session/location and current policy; retain old evidence as history without fresh punishment. | Move/rearm at a new desk and replay old queued observations; retain history without a fresh old-session response. |
