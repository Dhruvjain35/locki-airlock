---
title: "LOCKI AIRLOCK | Product Requirements & 24-Hour Engineering Plan"
subtitle: "HackUTD 2026 - NVIDIA Nemotron + SpacetimeDB"
author: "Team: Dhruv (software/integration), Areeb (Jetson/hardware), Sid (SpacetimeDB/frontend)"
date: "November 7, 2026"
geometry: margin=0.78in
fontsize: 10pt
colorlinks: true
linkcolor: blue
---

# LOCKI AIRLOCK

**Product requirements document - v1.0 | 7 Nov 2026 | Engineering build plan, not a claim of completed functionality**

**Pitch:** A desktop cyber-physical security companion that connects off-computer vision, real Mac security events, NVIDIA-powered investigation, and a trusted real-time security control plane. When a protected, armed workstation sees an unexpected physical interaction **and** a suspicious digital event, LOCKI responds immediately: it warns aloud, records evidence, can request a normal macOS screen lock with the owner's consent, and restricts actions inside its **own protected agent/workflow**. It escalates to an authorized human contact; it does not pretend to be police, enterprise antivirus, or a malware-proof lock.

**The demo-defining moment:** A consenting demo participant walks up to the *armed* station and plugs in an unknown USB device. The **Jetson's independent Logitech camera** reports a desk-zone event, the **Mac's USB monitor** reports a new device, **SpacetimeDB** correlates and authoritatively changes the session to `SUSPECTED_INTRUSION`, **LOCKI speaks immediately**, and the authorized Mac monitor initiates **normal screen lock** in a separately tested and opt-in mode. **Nemotron** uses actual diagnostic tools to investigate the event, explain exactly what is known and unknown, and prepare a reviewable response. The same state updates on two connected devices. In a second scenario, a malicious instruction attempts an unauthorized tool call in our owned Code Quest workflow and **NeMo Relay's pre-execution gate denies it**. No malware is executed in the demo.

## 0. Competition alignment and binding constraints

Read directly from the organizers' 2026 slides photographed at HackUTD:

- **NVIDIA:** "Use Nemotron Beyond a Chatbot" plus (1) beyond simple prompt-response, (2) multi-step workflows, (3) tools and external services, and (4) real-world application. Parakeet or Jetson alone **does not** satisfy the Nemotron requirement.
- **SpacetimeDB main track:** SpacetimeDB must power the **primary application functionality**, **not merely chat or movement**. A public source-code repository is required for prizes. (The SpacetimeDB slides also describe 2026 main-track first prize $2,048.)
- **Project theme:** cybersecurity. We are **not** entering the T-Mobile track.
- **Architecture reality:** Both Macs are Apple Silicon (Dhruv's M4 Air/16GB; teammate's M5 Pro/24GB). The NVIDIA Jetson is an independent embedded computer; its exact model/JetPack image **must be checked on device**, not guessed. The Zeus firmware was audited; **patch v2 must not be flashed** because simulated serial-buffer corruption could prevent Stop commands. **All motor commands remain prohibited.**
- **Critical sponsor decision:** The live submission should show **Nemotron Bolt E4B performing a genuine multi-step investigation with tool calls** on the 24GB Mac, plus genuine SpacetimeDB reducers/subscriptions. If Nemotron fails to run, do not falsely claim sponsor compliance; report the limitation.

## 1. Problem, buyer, user, and scope

**Problem:** People walk away from workstations at hackathons, shared labs, coworking spaces, and offices. A person can approach an unlocked machine and plug in a device or open unapproved software. Endpoint tools see digital indicators, cameras see physical activity, but a small independent desk unit could correlate both in real time and trigger practical action before the owner returns. A separate but related risk is AI coding agents exceeding authority because of untrusted repo instructions. These threats should be visibly distinguished in the app rather than conflated.

**Primary user:** A developer who explicitly arms their personal Mac while stepping away. **Secondary user:** A trusted teammate/security reviewer who receives incident details and can acknowledge/escalate the event. **In-scope attacker:** A volunteer simulating access to a demo workstation or adding **harmless test fixtures** to a sandbox repo. **Out-of-scope:** malware reversal, real USB malware execution, defeating a locked Mac, stopping an already running malicious program, identifying strangers by face, protecting arbitrary third-party AI agents, physical detention, automatic emergency-services dispatch.

**Value proposition:** One observable incident workflow that pairs independent physical evidence with actual on-host state, generates rapid understandable warnings, protects owned actions, and keeps a trusted human in the loop. It should be more than a remote camera alarm: **a real Mac lock request and a real app-level action deny** are measurable responses.

**Product name:** LOCKI AIRLOCK (inherits LOCKED IN brand). Subsystems: **WATCH** (camera + desktop signals), **GUARD** (deterministic policy), **INVESTIGATE** (Nemotron), **COORDINATE** (SpacetimeDB), **SPEAK** (existing LOCKI personality), **PHYSICAL CONSOLE** (Zeus body + optional Grove controls).

## 2. Requirements: what the system really will and won't do

### P0 - must work for the submission

1. **Explicit arm/disarm:** The authenticated owner chooses `DISARMED`, `ARMED`, or `PANIC / LOCKED`. Arm action captures a baseline USB-device set, current active app, camera healthy status, and user-selected response policy. Do not auto-arm just from vision.
2. **Independent physical evidence:** A **Logitech webcam connected to the Jetson Nano** reports `person_in_zone`, `hand_near_keyboard` if reliable, `zone_clear`, and `camera_unavailable`. Initial detector is a pre-trained lightweight person detector or simpler motion/region detector; no face identification, no emotion/suspicion model, and no fabricated "real suspicion dataset". A user-created 20-40-case consented test set evaluates false alerts.
3. **Real digital evidence from the Mac:** Monitor added/removed USB devices with IOKit/IORegistry (or a deliberately labeled fallback snapshot poll), plus visible app launches using `NSWorkspace.didLaunchApplicationNotification` or a tested process snapshot fallback. Preserve bundle ID/path and signed status where available; don't claim process listing catches all background launches.
4. **Deterministic correlation:** A configured state machine combines arming state, event types, device allowlist and time window. If `ARMED` and an unapproved USB is seen, raise an incident. Physical camera activity near the same time **raises priority**, but is **not required** for a new-device warning. Person in zone **alone** creates a soft alert, not a declaration of maliciousness. A mere remote-support application launch is evidence requiring confirmation, not proof of intrusion.
5. **Immediate response, without model dependency:** Trigger a local audible warning from pre-cached voice and an obvious dashboard/console color before any Nemotron inference. Severity-dependent policy may cause a **consented macOS screen lock** via a tested OS mechanism; collect explicit owner opt-in for automatic lock. Never lock another person's Mac, delete data, or run arbitrary shell commands.
6. **Authoritative realtime backend:** SpacetimeDB owns device enrollment, armed sessions, event ingestion, correlation state, incident lifecycle, reviewer acknowledgements, one-time protected-action approvals, and auditable event ordering. Agent and sensor services are adapters, **not another competing backend**.
7. **NVIDIA Nemotron is operational:** Locally run `nemotron-3.5-bolt:e4b` on the teammate's 24GB M5 Pro. Agent can call allowlisted tools such as `inspect_usb_event`, `inspect_app_metadata`, `read_recent_security_events`, `run_sandbox_tests`, `propose_lock_response`, `prepare_incident_report`, and `request_human_review`; actual tool execution is separately validated. Nemotron explains why it reached a recommendation and distinguishes evidence from conjecture.
8. **NVIDIA NeMo Relay:** Capture *real* model/tool spans and place a **pre-execution allow/deny** check on the managed protected Code Quest tool boundary. No claim that Relay secures arbitrary macOS processes. If Relay integration is incompatible, use an independent deterministic gate and show a real Relay trace on its supported surface, accurately labeled. Never label app JSONL logs as native Relay spans.
9. **Live voice:** Preserve existing Parakeet + Whisper, Silero VAD, Qwen fallback, cached Fish Audio clips, Kokoro and speech queue. Safety-warning playback priority is higher than conversation. `stop talking` silences speech without disarming security. Spoken content is not evidence that a lock actually happened.
10. **Demonstrable recovery:** Incident transitions through acknowledgment, verification, and owner disarm. Authorized changes are reflected on two clients live and persist across reconnections.

### P1 - if P0 is stable

- Camera second view from Zeus ESP32-CAM **only if network topology supports it** without breaking Jetson/M5/SpacetimeDB. The Logitech/Jetson path remains primary.
- Optional Grove physical arm/panic button and two-line LCD status after **electrical compatibility** verified. Button is a local human signal, **not authentication**. Protected actions still require a logged-in authorized user or paired device token.
- Jetson on-device pre-cached voice playback via supported audio output, so a network outage still produces a basic alarm. The JLab Bluetooth speaker may remain paired to Mac for the demo.
- A simple responsive owner notification (trusted contact dashboard or **opt-in** email/webhook through a configured service). Underlying service requires network; no promises without successful delivery.
- Signed Jetson event attestations with stable per-device key for tamper evidence, provided we can protect keys and the time budget permits. NOT a certified secure enclave.

### Explicitly cut

- Wheel motion, autonomous patrol or chasing an intruder; unsafe firmware v2 and no validated stop behavior.
- "Physically keep the hacker from doing anything," forced removal, facial profiling/biometric recognition, remote shell kill-switch, global OS malware quarantine, auto-dial 911/police, aggressive verbal threats.
- Apple **Lockdown Mode** toggles. It is an extreme spyware hardening feature and is **not the same as locking the Mac screen**.
- Actual malware USB sticks, USB keystroke injection demonstrations on real machines, network attacks, secret exfiltration, covert camera recording.
- Large Cosmos model, Isaac Lab, full desktop surveillance, or a new rewrite of the old app architecture.

## 3. Threat and trust model

### Assets

- The developer's logged-in Mac session and active project.
- Tool permissions for the owned Code Quest agent.
- Owner's security settings and incident feed.
- Jetson's optional device secret and physical authorization signal.

### Adversaries

- Opportunistic visitor with brief **physical access to an unlocked** demo Mac.
- A newly introduced USB storage or peripheral that was not in the baseline. **Unknown does not imply malicious**.
- An attacker-controlled README or tool output that attempts to trick an AI agent into an unapproved tool call in the demo sandbox.

### Trust boundaries

1. **Camera -> Jetson**: untrusted pixel data; metadata and confidence are observational. The model never establishes identity or guilt.
2. **Jetson -> SpacetimeDB**: authenticate device gateway, validate schema and sequence number; bound event rate. Merely declaring `person_present` is insufficient for sensitive action approval.
3. **Mac -> SpacetimeDB**: authenticated workstation identity, known device baseline; process and USB events are detections, not prevention.
4. **Nemotron -> tools**: model-proposed intents are untrusted; a hard-coded allowlist validates requests; Relay can gate only integrated call paths.
5. **SpacetimeDB -> Mac lock**: Mac receives an authenticated, narrowly scoped lock request; local policy checks that user previously opted into that response. Jetson cannot directly bypass macOS permissions.
6. **Human -> review**: authenticated owner/reviewer can acknowledge; independent approval on another device is stronger than the agent self-approving, but it cannot overcome full owner-account compromise.

**Security reality:** macOS screen lock prevents casual interactive access, not ongoing background processes. Detection after a USB connects may be too late to prevent a fast malicious device from acting. On Apple Silicon, native USB accessory authorization and normal screen-lock policies should be enabled separately; LOCKI adds detection and response. True systemwide process blocking on macOS requires Apple's restricted **Endpoint Security** entitlement and a properly installed system extension; we will not claim it in this prototype.

## 4. Exact system architecture

```text
LOGITECH USB CAMERA ---------> JETSON NANO --------------------+
                              vision + health + local alert    |
                              (no LLM on Nano)               |
                                                              v
MAC M4: USB + app watcher ------------------------------> SPACETIMEDB
        local Lock Screen action <------ signed/validated ---- reducers
        tool gateway <---------- state+permissions ---------> |  ^
        voice/TTS <----------- incident events ------------- |  |
                                                              |  |
MAC M5: NEMOTRON BOLT E4B <----- case + tool schemas ---------+  |
        NEMO RELAY traces/gates -------> allowed result ----------+
                                                              |
SECOND DASHBOARD <----- live subscriptions -------------------+

ZEUS CAR: parked, motors LOCKED. Mascot enclosure / optional ESP32 view.
USB JLab speaker stays paired to Mac; Jetson can optionally use separate audio.
```

**Network:** Use one actual routed LAN for Jetson/M4/M5/SpacetimeDB. Keep all service ports bound to localhost or LAN as appropriate; token-protect write/command endpoints. The Zeus `Zeus_Car` SSID is an **internetless access point**; if M4 joins it, it may disconnect from the Jetson/M5 LAN. Do not make camera access depend on two incompatible Wi-Fi networks. Recommended P0: Jetson Logitech webcam via USB, Mac + Jetson + M5 on the event's normal LAN or a tested local router. P1: add Zeus camera with second NIC/USB-Ethernet or validated network bridging; no motor WebSocket.

### Responsibility by runtime

| Node | Responsibility | Failure fallback |
|---|---|---|
| Jetson Nano | Logitech vision; physical alarm; local sensor health | Stop publishing events; mark camera unavailable; don't invent a clear scene |
| M4 Air | Existing FastAPI/React integration; USB/process watcher; speech/TTS; screen lock helper; Code Quest | Local lock only on approved policy; keep server alive without Jetson |
| M5 Pro | Nemotron Bolt E4B local Ollama; Relay where supported | Qwen for conversation, but **not a qualifying Nemotron demo** |
| SpacetimeDB server | Live authenticated security state / reducers / incident coordination | Fail closed on *protected agent actions*; local Mac security state remains active if configured |
| Zeus body | Visible physical station; optional extra camera; never moves | Static enclosure without compromising primary surveillance |

## 5. SpacetimeDB: PRIMARY backend, not an alert mirror

Build a TypeScript or Rust SpacetimeDB module using its current SDK and generated client bindings. **Use reducers** for mutations, private tables for sensitive data, and scoped views/subscriptions for the authorized owner and reviewer. Do not mark incident details publicly readable to every client. SpacetimeDB's public tables are visible to subscribers; its private tables are not directly subscribed to.

### Core data schema (proposed typed fields; adapt to SDK actual types)

| Table | Key fields and semantics |
|---|---|
| `Workstations` | `id`, `owner_identity`, `label`, `mac_device_id`, `jetson_device_id`, `policy_id` |
| `Sessions` | `id`, `workstation_id`, `armed_by`, `status`, `armed_at`, `expires_at`, `device_baseline_hash` |
| `DeviceBaselines` (private) | session, hashes of allowed USB IDs; no raw serials on public UI |
| `DeviceEvents` | `event_id`, `device_id`, `session_id`, `kind`, `observed_at`, `received_at`, `sequence`, `confidence`, redacted payload |
| `Incidents` | `id`, `session_id`, `severity`, `status`, `evidence_ids`, `opened_at`, `resolved_at` |
| `Correlations` | incident id + event IDs + deterministic rule identifier + time delta |
| `AgentRuns` | `run_id`, `model_id`, `incident_id`, `steps`, `tool outcomes`, `status` |
| `ProtectedActions` | `id`, `mission_id`, normalized action digest, permission classification, deadline, status |
| `Approvals` (private) | `id`, `action_id`, approver identity, one-time nonce, expiry, consumed time |
| `Acknowledgements` | incident id, reviewer identity, note, action, timestamp |
| `DeviceHealth` | heartbeat, last seq, camera/mic status, latest error, firmware motion lock |
| `AuditEvents` | append-only logical event journal within trusted service, event chain optional |

**Server-owned reducers:**

- `arm_session(owner, workstation, response_policy)` verifies owner and captures baseline hash/expiry.
- `ingest_device_event(auth_device_id, event)` validates schema, session, sequence, rate limit and clock skew; stores event.
- `evaluate_correlations(session)` applies deterministic policy and updates severity/incident within a transaction (or performs evaluation atomically inside ingestion).
- `acknowledge_incident(incident, actor, response)` authenticates reviewer and writes transition.
- `request_workstation_lock(incident, reason)` checks policy, actor and event; queues **single lock request**, never arbitrary commands.
- `propose_protected_action(...)`, `approve_action(...)`, `consume_action_approval(...)`: validate exact digest, authorization identity, TTL and one-time semantics atomically.
- `resolve_incident(...)` requires supported recovery evidence; no LLM self-resolution.

**State-machine example:**

```text
DISARMED --owner_arm--> ARMED
ARMED --person_near_desk--> OBSERVING (soft, no accusation)
ARMED/OBSERVING --unknown_USB_or_policy_app--> SUSPECTED_INTRUSION
SUSPECTED_INTRUSION --owner_lock_policy_and_verified_request--> LOCK_REQUESTED
LOCK_REQUESTED --Mac_reported_success--> SCREEN_LOCKED
SUSPECTED_INTRUSION/SCREEN_LOCKED --reviewer_auth--> REVIEWING
REVIEWING --human_resolution--> RESOLVED
any --camera_lost--> DEGRADED (camera not trusted as evidence)
```

**Identity / subscriptions:** generate separate identities for Mac agent, Jetson publisher, owner, reviewer. Expose only authorized views. Test that anonymous viewers cannot read incidents or invoke reducers. Multiple team dashboards should get the same real incident within an observed latency budget, using subscriptions rather than independent REST copies.

**Fault model:** event ID deduplication, per-device monotonic sequence, heartbeat 3 seconds, time sync/NTP if available; stale camera data ignored; store both observed and received times. Define `camera_offline` and `model_offline` states distinctly. SpacetimeDB restart should reconstruct sessions and incidents. Protected actions must fail closed on DB outage; passive voice can keep speaking cached warnings if locally generated.

## 6. NVIDIA: each piece does work that cannot be replaced by a model name

### 6.1 Jetson Nano + optimized inference (Areeb)

- **Compute role:** Independent Linux edge node, not merely USB webcam passthrough. Run local person/desk-area detector with NVIDIA's JetPack/TensorRT/jetson-inference where compatible with its installed JetPack. Extract low-volume events; don't stream continuous video into cloud or DB.
- **Measure:** event latency from camera capture, actual 720p/640x480 FPS, sustained CPU/GPU/memory, false triggers and dropped frames. Do not promise a specific FPS without measurements.
- **P0 outcome:** unplug the Mac's camera or close the Mac app and Jetson continues detecting zone activity and generating local events/independent alarm; digital correlation still depends on Mac agent.
- **P1:** after physical controls validated, show armed/alert status on separate display. Presence is not authentication.

### 6.2 Nemotron 3.5 Bolt E4B (Dhruv)

- **Why this model:** Locally runnable on Apple Silicon via the **explicit** Ollama tag `nemotron-3.5-bolt:e4b` (NOT bare `:latest`, which selects a much larger model). The E4B download is roughly 7.2-7.9GB and has 8B total/4B active parameters. The M4/16GB already had little free disk and heavy swap: install on the M5/24GB after capacity check.
- **Real multi-step workflow:** (1) load incident data; (2) inspect real USB identifier metadata; (3) inspect app launch/signing metadata; (4) inspect arm policy and previous session; (5) optionally inspect associated sandbox repo changes; (6) produce structured evidence/evaluation; (7) request `notify_owner`, `review` or safe `lock_request` as tools; (8) verify action outcome. All observations logged to SpacetimeDB. A tool may fail; agent must explain failure rather than invent output.
- **Model never decides guilt**, never identifies an individual, never becomes a root shell, never directly calls arbitrary `subprocess` commands or controls motor firmware. Strict JSON schema for tool intents; tools require gate authorization.
- **Qwen3 fallback:** existing local conversation still works if Nemotron cannot run; explicitly mark as fallback and not as NVIDIA-prize proof.
- **Measurement:** tool selection correctness, wrong-tool rate, grounded statements, total investigation time, latency to first useful response, effect on demo reliability.

### 6.3 NeMo Relay (Dhruv)

- Instrument the **managed** Code Quest tool adapter; send actual model/tool spans and correlate trace IDs with SpacetimeDB agent runs.
- Install only a pinned version demonstrated compatible on target Mac; CLI supports Apple Silicon, but Python wheel support depends on the specific release (Claude's report says its environment has a native Python package; independently verify exact installed wheel and runtime). Use genuine conditional-execution guardrails when supported; otherwise keep the deterministic gateway as authoritative.
- Specific test: model attempts `read_demo_secret` or `deploy_dummy_artifact` outside policy -> **callback never runs**. Allowed `run_pytest_in_sandbox` succeeds. Show trace and test assertion.
- Do **not** claim the Relay layer can block USB device driver events, arbitrary apps outside the managed runtime, or malicious shell commands outside its integration path.

### 6.4 NVIDIA Parakeet and optional Jetson inference

- Keep existing offline Parakeet + Whisper ASR as a **speech-input subsystem**, not the only NVIDIA integration. Stop, mute, acknowledge, arm/disarm use rule-based high-priority intents rather than waiting for the reasoning LLM.
- Jetson vision is a core NVIDIA hardware role. Don't add Cosmos during the event just to increase model count.

## 7. Macintosh endpoint integration and what 'lockdown' means

### 7.1 Detector

- **USB events:** IOKit/IORegistry device matching + notifications; store tuple of vendor/product/location or comparable OS-provided identifiers, hash sensitive identifiers before sharing, compare to allowlist at arm. If event-native watcher is too slow to implement, use a measured `ioreg`/`system_profiler` snapshot poll and label its latency honestly. New USB by itself is *unknown*, not malware.
- **App/process events:** native `NSWorkspace.didLaunchApplicationNotification` (visible applications; not all background agents), plus optional periodic `NSWorkspace.runningApplications` sampling. Maintain an allowlist of user-approved development apps and demo's synthetic suspicious remote-support process. Metadata should include executable path and code signing if readily accessible, but do not require invasive security entitlements.
- **Protected agent tool events:** inspect actual Code Quest tool requests through Relay/deterministic gateway. This is the only P0 avenue where we can reliably block an action *before* its callback executes.

### 7.2 Intervention

- **Warn:** queue prerecorded immediate line independent of model: "Security alert. This workstation is armed. Step away from the keyboard. The owner has been notified." No threatening impersonation of law enforcement; "get the fuck out" is available only as an explicitly selected demo character style, but defaults to judge-friendly language to avoid escalating real confrontations.
- **Lock screen:** owner enables auto-lock during arming. macOS has an OS-supported **Control-Command-Q** screen-lock action; implementation must be proved on user's own Mac with consent and correct Accessibility permissions or validated API. Do not confuse `pmset displaysleepnow` with lock; don't assume screen-saver open guarantees password requirement. Always show `lock_requested`, `lock_executed`, and `lock_confirmed` as separate statuses unless actual confirmation can be obtained.
- **Harden owned workflow:** freeze Code Quest/other explicitly integrated protected operations. No promises of blocking new USB enumeration, deleting processes, killing background malware or disconnecting all network connections.
- **Contact:** notify **preconfigured owner/trusted teammate** via authenticated local dashboard; optional opt-in email/SMS/incident webhook if service works. Only a human can choose **Contact campus security**, using a verified contact method entered by the user. **Never automatically call law enforcement or emergency services.**
- **Fail-safe:** owner must always be able to unlock their computer through normal credentials and disarm with authenticated control. Do not create a self-locking loop that immediately locks again while the owner is trying to regain access. Cooldown and grace window after confirmed owner return.

### 7.3 Example correlated policy

| Observed event | Owner armed? | Response |
|---|---|---|
| Person crosses zone, nothing digital | Yes | Soft alert; local log; no accusation or screen lock |
| New unknown USB device | Yes | Immediate audible security warning + incident; if auto-lock opted in, request OS screen lock |
| Unknown USB + person within 10 s | Yes | High-severity correlated incident; owner/trusted reviewer notification; lock request if policy allows |
| Approved peripheral inserted | Yes | Informational event only |
| Unapproved remote-admin app launched | Yes | Incident + owner review; do not forcibly kill app |
| Unapproved Code Quest tool call | Any protected session | **Reject pre-execution** through managed gateway |
| Camera lost but USB monitoring healthy | Yes | Degraded mode; digital detector continues |
| Agent model unreachable | Any | Warnings and state-machine continue, investigation marked pending |

## 8. Voice and character interaction - preserve what exists

**Never delete the working voice pipeline.** The current repo already has `backend/app/voice/`, `backend/app/intelligence/`, a local speech queue and FastAPI dashboard. The newer *local* `feature/code-quest` branch reportedly has 969 passing tests and real Qwen+pytest verification, but is **not on the remote yet**; reconcile locally before migration.

**Speech priorities:** P0 security warning (pre-cached under 400ms target, measure it); P1 short clarifying command/response; P2 Nemotron reasoned response; P3 idle personality/game chatter. A new P0 cancels or ducks a P2 monologue. Show `security_mode=judge` with profanity disabled by default; other personality modes must never interfere with urgent warnings.

**Illustrative dialog:**

- Owner: "LOCKI, arm my desk." LOCKI: "Armed. I'm watching the desk zone and new devices." (Actual arm reducer must succeed.)
- Alert: "New USB device detected while armed. Someone is at the workstation. I am requesting a screen lock." (Speak only if all supported facts are true; not "malware detected".)
- Owner: "What happened?" Nemotron: "A USB device with an unknown identifier appeared 2 seconds after a person entered the zone. That doesn't prove it's malicious. The screen-lock request succeeded; you should verify the device before resuming."
- Owner: "Stop talking." Silence speech, keep monitoring. "Disarm" requires authenticated owner confirmation, not just a voice command in an untrusted room.

**Network/offline:** All inference is local to the team's Macs or Jetson. SpacetimeDB can be on LAN. The JLab speaker pairs to Mac unless compatible USB audio is confirmed on Jetson. Nothing must rely on NVIDIA Build. Microphone input is consent-based and has a clear visible on/off indicator.

## 9. UI requirements: actual security control room

- **Header:** `ARMED / DISARMED / SUSPECTED INTRUSION / SCREEN LOCK REQUESTED / SCREEN LOCK CONFIRMED / DEGRADED`; small source of each state.
- **Realtime three-column layout:** (left) workstation + camera/USB/app health; (center) chronological correlated event timeline with event IDs, timestamps and uncertainty; (right) LOCKI voice transcript, Nemotron investigation steps and approved response actions.
- **Visible policy:** show whether auto-lock is enabled, what apps/devices are allowlisted, whether protected Code Quest actions are gated.
- **Human review:** acknowledge, request more information, resolve, disarm; server-side role checks for each action.
- **Investigations:** every tool call shows input type, redacted arguments, actual success/error, duration and agent reasoning summary; never fake a trace.
- **Connection failures:** camera lost, Jetson offline, model down, SpacetimeDB reconnect visible. No magical green shield when sensors are dead.
- **Demo switch:** choose pre-enrolled harmless USB, synthetic repo malicious README, camera volunteer activity. This controls fixtures ONLY; it must not inject fake production alerts indistinguishable from real ones.

## 10. Team split: three independent major jobs

### AREEB - Embedded systems + Jetson + physical security console (OWNER)

**Deliverable:** Independent Jetson service that boots, processes Logitech live video, publishes authenticated zone events, reports health, optionally speaks a canned warning, and operates a physically mounted status/control module **only after electrical review**.

**Owns:** `edge/jetson/`, `hardware/`, `docs/hardware/`. Provide a short Jetson service API `GET /health`, `GET /metrics`, `POST /local-test-alert` bound only to LAN/localhost with authentication if exposed; websocket or signed publisher client to SpacetimeDB gateway. Owns hardware inventory, model identification, power, frame timing, camera placement, wiring risk assessment, 20-40 human-trial labeling.

**Acceptance:** Jetson model + RAM + JetPack recorded; Logitech camera streams reliably 10 minutes; a real person triggers `person_in_zone` and the event appears on second Mac; CPU/GPU/RAM measured; unplugged camera produces `camera_offline`; no unsafe 5V signals into GPIO; Zeus motors never commanded. Film a 20-second proof clip.

**Non-dependencies:** Can work with Sid's mock event publisher contract before full database exists. If Jetson doesn't boot in first 90 minutes, notify team and switch to a clearly identified fallback webcam-on-Mac while keeping Jetson re-attempt bounded.

### SID - SpacetimeDB + policy engine + cyber dashboard (OWNER)

**Deliverable:** Real SpacetimeDB backend, identity/permissions, reducers, two-client subscriptions, deterministic incident correlation, action workflow, and a polished cyber control-room UI. This is the **majority of our app's primary server logic**.

**Owns:** `spacetime/` or `backend/spacetime/`, `frontend/src/airlock/`, generated SDK client; schema, event contracts, reducer tests, UIs. Keep existing frontend environment; replace the **product surface**, not the existing working voice engine. Expose `ingest_device_event`, session arm/disarm, incident state, reviewer actions, lock request, protected action approval.

**Acceptance:** Two different authenticated clients see the same real incident; client cannot forge reviewer identity; unauthorized session changes rejected; reconnect reproduces state; expired/used approvals refused; no sensitive payload in public tables. Time event-to-dashboard latency from both laptops and record it.

**Non-dependencies:** Use fixture JSON events from first hour, then switch to Areeb's Jetson and Dhruv's Mac endpoints unchanged.

### DHRUV - Mac security agent + NVIDIA intelligence + system integration (OWNER)

**Deliverable:** Real USB / app event monitoring, optional Mac screen-lock action, managed protected tool gateway with Relay, local Nemotron multi-tool investigation, existing audio/voice integration, team merge/testing and actual pitch/demo.

**Owns:** `backend/app/security/`, `backend/app/intelligence/airlock_agent.py`, `backend/app/voice/` integration and `scripts/airlock_*`, tests. Use current `backend/app/runtime.py` and event bus; no new competing orchestration service. Reuse Code Quest's sandboxed test runner and result verification as a separate P1 managed-action security demo.

**Acceptance:** Real USB insertion captured (without reading or executing device files); real test app launch captured; owner-initiated lock preview and later confirmed OS lock tested with explicit consent; Nemotron E4B makes 3+ genuine approved tool requests on M5; negative and positive Relay tool checks; high-priority line audible; full three-device end-to-end trial once components land.

**Non-dependencies:** Build digital event adapters against Sid's typed fixture event contract. Don't wait for camera setup to finish USB and process work.

### Integration protocol: no three people editing the same files

| Interface | Producer | Consumer | Version/contract |
|---|---|---|---|
| `SecurityEventV1` (typed JSON) | Areeb Jetson + Dhruv Mac | Sid reducers | `event_id`, `source`, `kind`, `observed_at`, `seq`, `confidence?`, `metadata`, `schema_version=1` |
| `IncidentUpdateV1` | Sid SpacetimeDB | Dhruv voice/LLM + Areeb local console + React | `incident_id`, `severity`, `state`, `evidence_ids`, `action_required` |
| `LockRequestV1` | Sid reducer | Dhruv Mac agent | `session_id`, `action_id`, `expiry`, `reason`, `owner_policy_hash` |
| `AgentStepV1` | Dhruv Nemotron/Relay | Sid database + dashboard | `run_id`, `step`, `tool_name`, `status`, `evidence_id`, `duration_ms` |

**At hour 0, agree on a JSON Schema and sample fixtures.** Only one code owner per interface; no parallel edits to `backend/app/runtime.py` or `frontend` routing. Merge by feature branch after CI/test checks. State in standups: branch, commit, working real paths, blocked dependencies, next concrete handoff.

## 11. Repository migration - IMPORTANT: DON'T DELETE THE VALUABLE WORK

The existing private repo is `Dhruvjain35/locked-in`. GitHub's currently visible default branch still shows the *older* phone-protecting architecture; Claude reports current Code Quest work in **local `feature/code-quest`, not pushed**. Preserve this before any destructive command.

**Safe sequence (commands to run on the owner Mac after checking clean status):**

```bash
cd /path/to/locked-in
git status --short
git branch --show-current
git log -1 --oneline
# Commit/stash only after reviewing uncommitted work; don't discard it.
git branch archive/pre-airlock-2026-11-07
# Work from current local feature/code-quest or merge it in a controlled way.
git switch -c feature/airlock-integration
# Keep old implementation in Git history; disable obsolete UX through flags.
```

**Keep:** `backend/app/voice`, `backend/app/intelligence` core conversational workers, `backend/app/api`, `backend/app/runtime.py`, existing tests and make targets, `frontend` scaffold, Code Quest's safe pytest/tool validation, event bus, Qwen fallback, NeMo Relay experiment, Jetson and Zeus camera adapters, firmware audit. **Retire from product UI** (do not immediately delete): phone-retreat controls, timed focus-only modes, game boss screen, Arduino motor-enable UI, unsafe patch-v2 workflow; hide behind `PRODUCT_MODE=airlock` and `MOTION_LOCKED=true`. Keep `firmware/` as archived/read-only with an explicit NO FLASH warning.

**Public competition repo:** Organizers require public code for SpacetimeDB track. Publish a reviewed sanitised competition branch/repo only after checking for API keys, captured voice, telemetry, serials, private images and research files. Do not expose private credentials, cached audio with license restrictions, or unrelated projects. `git push` only after user approval. No remote deletion, force push or destructive `git clean -fdx`.

**Proposed directory changes:**

```text
backend/app/security/            # mac_watcher, policy, lock_adapter, tool_gate
backend/app/intelligence/        # retain Qwen; add Nemotron investigation adapter
backend/app/voice/               # retain; add high-priority security announcements
edge/jetson/                     # Logitech capture, detector, event publisher
spacetime/                       # schema, reducers, generated clients, tests
frontend/src/airlock/            # control room, incident timeline, reviewer panel
hardware/                        # wiring/placement notes, photos, measurements
docs/airlock/                     # PRD, demo, evaluation, hardware
scripts/airlock_*.sh             # environment and launch/preflight
```

## 12. 24-hour team schedule (time-boxed)

| Time since kickoff | Areeb | Sid | Dhruv |
|---|---|---|---|
| 0-1 h | Inventory Jetson, power and webcam; no wiring yet | Schema and fixture events; start SpacetimeDB | Safe Git branch; confirm M4 voice and M5 model capacity |
| 1-3 h | Boot Jetson, Logitech 10-min stream, local camera event | Reducers: arm, ingest, incidents; 2-client subscriptions | USB watcher + synthetic process-launch fixture; play warning line |
| 3-5 h | Person-in-zone tests; connect publisher | Realtime dashboard + state machine | Mac lock capability dry-run with owner approval; Nemotron install/latency check on M5 |
| 5-7 h | Sensor reliability + heartbeat and health | Device auth, server-side rules and incident transitions | Nemotron 3+ tool workflow; integrate real security events |
| 7-9 h | Optional Grove status hardware only if safe | Approval and acknowledgment, auth negative tests | Relay tool guard, Code Quest protected action test |
| 9-11 h | Jetson real event -> DB integration | Two-client end-to-end sync and UI polish | High-priority voice and incident response with real DB |
| 11-14 h | Camera false-positive evaluation | Reducer concurrency/replay/failure tests | Full scenario, latency and Mac unlock recovery |
| 14-17 h | Physical enclosure, cable routing, fallback | Security dashboard polish; persistence | Nemotron/Qwen comparison; report claims matched to tests |
| 17-20 h | Demo rehearsal and Jetson reliability soak | Deployment, public repo scrub, Devpost assets | Integration owner: live adversarial scenario and footage |
| 20-24 h | Repairs only | Repairs only | Final submission and backup fallback recording |

**No 3-hour speculative audit loops.** Each block ends with actual proof artifacts and a GO/NO-GO. If Jetson, Nemotron or physical wiring runs over its allotted time, narrow scope rather than risk the core working demo. Follow the event's exact submission deadline, not an assumed 24h starting clock.

## 13. Concrete acceptance tests and demonstration evidence

### Physical and desktop

- **HW-01:** Jetson model, power interface, JetPack, RAM and webcam exact model logged.
- **HW-02:** 10-minute USB webcam stream, live device metrics; unplug recovery tested.
- **PHY-01:** Real person crosses marked desk zone, event logged with observed timestamp; benign walk-bys measured.
- **MAC-01:** Baseline allowed USB does not open high-severity incident.
- **MAC-02:** New harmless USB device during armed session opens an incident; never execute files from device.
- **MAC-03:** Known test application process launch logged; labels distinguish app launch from confirmed compromise.
- **MAC-04:** With explicit opt-in, owner invokes lock on own Mac; logs distinguish request from completed lock; unlock/disarm works.
- **VOX-01:** Real microphone -> ASR -> Nemotron/Qwen conversational response -> audible speaker; P0 warning interrupts P2 answer. Record latency.

### NVIDIA sponsor

- **NV-01:** E4B exact model tag, runtime, version and actual local model response on M5 evidenced; no cloud endpoint.
- **NV-02:** Nemotron calls 3+ **real** approved tools on a real correlated incident; output includes failures/uncertainties.
- **NV-03:** NeMo Relay native trace has start/end with correlation ID, not only custom logs.
- **NV-04:** Protected unauthorized tool callback blocked before execution; harmless allowed callback succeeds.

### SpacetimeDB sponsor

- **ST-01:** Security session and incident are CREATED and mutated through SpacetimeDB reducers, not merely mirrored from FastAPI.
- **ST-02:** Two separate logged-in clients immediately see the same reducer-committed incident.
- **ST-03:** Reducer denies unauthorized acknowledgement and unauthorized action approval.
- **ST-04:** Repeated/expired/modified protected action approval is denied transactionally.
- **ST-05:** Losing and recovering a client connection restores latest state.

### Failures / privacy

- **SEC-01:** Simulated malicious README attempts protected tool; deny without executing command or accessing real secrets.
- **SEC-02:** Camera disconnected -> dashboard says degraded and stops relying on visual events.
- **SEC-03:** Model down -> immediate warning and deterministic Mac policy still function.
- **SEC-04:** No raw camera feed is sent to SpacetimeDB or cloud; retained evidence requires consent and has short retention.
- **SEC-05:** Unapproved voice command cannot disarm physical security while armed.

**Target measurements (goals, not current results):** hardware-event-to-local-warning <= 1.5 s; USB event to dashboard <= 2 s on LAN; owner alert <= 3 s on LAN; false physical alerts <= 1/20 benign trials; accurate known test cases >= 90%; no unauthorised protected tool callbacks executed; release sign-off only after live trials. Results must include sample size, timing method, percentiles and caveats. Record *actual* metrics next to targets in final Devpost.

## 14. Three-minute judge demo and safe script

1. **0:00-0:20:** Real Jetson and Zeus stationary physical station, M4 and M5 dashboards. Show `ARMED`, connected health, and model ID. Owner steps away.
2. **0:20-0:50:** Consent-based volunteer enters desk zone, inserts **a harmless USB device**. Show the OS's genuine device event and independent Jetson camera event. Don't claim it is malware.
3. **0:50-1:15:** LOCKI immediately says a factual warning; SpacetimeDB transitions to `SUSPECTED_INTRUSION` on both screens. Demonstrate screen lock only if previously tested and opt-in.
4. **1:15-2:05:** Nemotron executes an actual multi-tool security investigation; live Relay trace and underlying tool responses on-screen. It says what is uncertain.
5. **2:05-2:35:** Separate protected Code Quest demonstration: a malicious text fixture suggests an unauthorized action. Relay/tool gateway blocks before execution. Then an allowed test command runs.
6. **2:35-3:00:** Authenticated owner acknowledges and resolves the incident. Both devices sync. Show one table with observed detection/latency/false-positive results; state exactly what the prototype does not prevent.

**Alternate fast path:** If physical camera inference fails, retain real USB detection, local warning, Mac opt-in lock, Nemotron real tool workflow, and SpacetimeDB case. Explicitly say camera is degraded. If Bolt fails, do not position the submission as having satisfied the NVIDIA Nemotron track. If SpacetimeDB fails, do not claim its main-track eligibility.

## 15. Review gates / decisions required from owner

- Jetson exact device model, boot image and *correct* power adapter identified before connecting any new power cable.
- Logitech webcam actual device identification and permission.
- Explicit opt-in for microphone, camera, background activity monitoring, and Mac auto-lock.
- Approval before any large model download or deleting existing model caches.
- Approval before any firmware flash, motor instruction, actual external notification or push.
- Decide notification recipient (owner/teammate) and escalation wording; no automatic police call.

## 16. Evidence and authoritative references

1. NVIDIA Nemotron Bolt model and exact E4B Ollama tag: https://ollama.com/library/nemotron-3.5-bolt
2. NVIDIA NeMo Relay GitHub / managed tool middleware: https://github.com/NVIDIA/NeMo-Relay
3. NeMo Relay pre-execution guardrails: https://docs.nvidia.com/nemo/relay/v0.10.0/about-nemo-relay/concepts/middleware
4. NeMo Relay installation and Apple Silicon CLI: https://docs.nvidia.com/nemo/relay/getting-started/installation
5. SpacetimeDB reducers/transactions: https://docs.rs/spacetimedb/latest/spacetimedb/
6. SpacetimeDB table visibility: https://spacetimedb.com/docs/tables/
7. SpacetimeDB client subscriptions: https://spacetimedb.com/docs/clients/subscriptions/
8. NVIDIA Jetson Nano getting started: https://developer.nvidia.com/embedded/learn/get-started-jetson-nano-devkit
9. Apple NSWorkspace app-launch semantics: https://developer.apple.com/documentation/appkit/nsworkspace/didlaunchapplicationnotification
10. Apple IOKit matching overview: https://developer.apple.com/library/archive/documentation/DeviceDrivers/Conceptual/IOKitFundamentals/Matching/Matching.html
11. Apple keyboard shortcut for screen lock: https://support.apple.com/en-us/102650
12. Apple Endpoint Security entitlement requirement: https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.endpoint-security.client
13. OWASP Excessive Agency 2025: https://genai.owasp.org/llmrisk/llm062025-excessive-agency/
14. Grove RGB LCD V4.0 5V I2C specification: https://wiki.seeedstudio.com/Grove-LCD_RGB_Backlight/
15. User-supplied HackUTD 2026 prize and challenge slides; user-supplied hardware photos; team local Code Quest status report (969 passed + 3 skipped). These are **reported context**, not remotely verified current performance.

---

**Project ownership:** Dhruv = integration/NVIDIA/Mac; Areeb = Jetson/physical; Sid = SpacetimeDB/backend/frontend. **Success definition:** real physical event + real Mac event -> authoritative security incident -> immediate audible warning -> verified local action, independent multi-step Nemotron investigation, live second-device review. No stage requires moving the wheels.
