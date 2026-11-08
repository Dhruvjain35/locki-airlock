# Investigation `inv_c268a4ed45f3417cabb2`  [TEST DATA]

> TEST DATA: fixture evidence from fixtures/scenarios, NOT read from the live LOCKI database

**TEST DATA. Official severity medium (rule unapproved_app_while_armed). Nemotron's evidence review: medium risk, medium confidence.**

- Incident: `incident:fixture-c-app-incomplete`
- Official severity (LOCKI rule, authoritative): **medium** (rule `unapproved_app_while_armed`); effective severity **medium** (official_severity)
- Model: `nemotron-3.5-bolt:e4b` (local Ollama on the M5)
- Status: **completed**
- Evidence source: FIXTURE C_suspicious_app_incomplete (not a live database read) (live=False)
- Total 9495 ms; model 9463 ms; tools 2.5 ms; first tool decision 293.9 ms

## What Nemotron was given

System prompt (abridged to first line):
> You are the LOCKI AIRLOCK security investigator, running locally on NVIDIA Nemotron.

User message (the entire task input):
> Investigate incident incident:fixture-c-app-incomplete.
Requester note (untrusted, informational only): Benchmark trial 1 of 5 for C_suspicious_app_incomplete.

Plus the JSON schemas of 7 read-only tools. No evidence was pre-assembled.

## Step-by-step

### Step 1: Nemotron inference 271 ms (prompt 1013 tok, output 22 tok)
- Selected `get_incident` with arguments `{"incident_id":"fixture-c-app-incomplete"}`
  - Executed `get_incident`: **ok** in 0.33 ms (arguments canonicalized): incident unapproved_app_while_armed/medium, 2 linked events
    - evidence: `incident:fixture-c-app-incomplete`

### Step 2: Nemotron inference 316 ms (prompt 1349 tok, output 24 tok)
- Selected `get_session_status` with arguments `{"incident_id":"fixture-c-app-incomplete"}`
  - Executed `get_session_status`: **ok** in 0.20 ms (arguments canonicalized): session ARMED at incident time
    - evidence: `sess:fx-6b23c0d5f35d1b11f9b6`

### Step 3: Nemotron inference 271 ms (prompt 1686 tok, output 24 tok)
- Selected `get_incident_evidence` with arguments `{"incident_id":"fixture-c-app-incomplete"}`
  - Executed `get_incident_evidence`: **ok** in 0.38 ms (arguments canonicalized): 1/2 linked events retrieved
    - evidence: `evt:fx-36cfb77296b464dcf07e`

### Step 4: Nemotron inference 459 ms (prompt 2075 tok, output 33 tok)
- Selected `get_recent_application_events` with arguments `{"incident_id":"fixture-c-app-incomplete","window_s":60}`
  - Executed `get_recent_application_events`: **ok** in 0.74 ms (arguments canonicalized): 2 application events (of 2)
    - evidence: `evt:fx-36cfb77296b464dcf07e`, `evt:fx-512ec056322e746b96c0`

### Step 5: Nemotron inference 453 ms (prompt 2594 tok, output 34 tok)
- Selected `get_recent_usb_events` with arguments `{"incident_id":"fixture-c-app-incomplete","window_s":300}`
  - Executed `get_recent_usb_events`: **ok** in 0.16 ms (arguments canonicalized): 0 USB events (of 0)
    - evidence: `res:5.0:get_recent_usb_events`

### Step 6: Nemotron inference 308 ms (prompt 2848 tok, output 34 tok)
- Selected `get_recent_vision_events` with arguments `{"incident_id":"fixture-c-app-incomplete","window_s":300}`
  - Executed `get_recent_vision_events`: **ok** in 0.17 ms (arguments canonicalized): 0 vision events (of 0)
    - evidence: `res:6.0:get_recent_vision_events`

### Step 7: Nemotron inference 268 ms (prompt 3102 tok, output 26 tok)
- Selected `get_device_baseline_status` with arguments `{"incident_id":"fixture-c-app-incomplete"}`
  - Executed `get_device_baseline_status`: **ok** in 0.54 ms (arguments canonicalized): 2 known USB devices; 0/0 nearby USB events not in baseline; camera=offline (device_health)
    - evidence: `health:fx-51d4d3368ba011cea554`, `health:fx-1476dee6ecf73dbc2847`, `sess:fx-6b23c0d5f35d1b11f9b6`, `base:fx-6f2918a74a8416d23bfa`

### Step 8: Nemotron inference 958 ms (prompt 3611 tok, output 52 tok)
- Selected `finish_investigation` with arguments `{"reason":"The incident was triggered by an unapproved application launch (RemoteDesk Helper) while the session was armed, and no further evidence from USB, vision, or baseline checks can change this assessment."}`
  - Executed `finish_investigation`: **finished** in 0.00 ms: model chose to stop: The incident was triggered by an unapproved application launch (RemoteDesk Helper) while the session was armed, and no further evidence from USB, vision, or baseline checks can change this assessment.

### Final assessment call (finalize): 6158 ms, 901 output tokens, constrained JSON decoding

## Assessment (after deterministic grounding)

**Official severity:** medium (LOCKI rule; never changed by Nemotron)

**Nemotron's risk (its own view):** medium (confidence medium). An unapproved application was launched while the security session was armed, indicating a policy violation.

**Voice view text (what LOCKI speaks):** Test data only, not the live LOCKI database. LOCKI rated this incident medium severity. An unapproved application was launched while the session was armed. No USB or vision events were recorded in the search window. Not established: who was physically present during the incident. Nemotron's evidence review: medium risk, medium confidence.

**Summary (model_spoken_summary):** An unapproved application was launched while the session was armed. No USB or vision events were recorded in the search window. A human reviewer should check system logs for the missing event.

**Observed facts (each cites retrieved evidence):**
- An application named RemoteDesk Helper was launched and was not approved. [evt:fx-36cfb77296b464dcf07e]
- The session was armed at the time of the incident. [sess:fx-6b23c0d5f35d1b11f9b6]
- The application Terminal was activated shortly after the unapproved launch. [evt:fx-512ec056322e746b96c0]
- No USB connect or disconnect events were recorded in the search window. [res:5.0:get_recent_usb_events]
- No vision events were recorded in the search window. [res:6.0:get_recent_vision_events]
- The vision component status was reported as offline. [health:fx-1476dee6ecf73dbc2847]

**Possible explanations (inferred):**
- (high) The user intentionally ran an unapproved application while the system was armed.
- (medium) The application launch was an isolated event without corroborating physical evidence.
- (low) The application was necessary for remote support, but this is not confirmed by evidence.

**Missing information (unknown):**
- Who was physically present during the incident.
- If the unapproved application was required for business operations.
- If the unapproved application was related to the baseline devices.
- The specific reason for the vision component being offline.
- The full details of the second linked event ID.
- [system] 1 event(s) linked to the incident could not be retrieved

**Recommended actions (all require human approval; none executed by the agent):**
- Review the user's job role and application necessity.: Determine if the application usage was malicious or operational.
- Check system logs for the missing event ID.: Attempt to retrieve the second linked event for a complete picture.
- Confirm the baseline status of the workstation.: Verify if the workstation configuration is standard.

Validation: 6/6 facts grounded, citation accuracy 1.0, 0 flags.

## Evidence timeline (rebuilt deterministically from retrieved evidence)

| t (s from incident) | time (UTC) | evidence | event |
|---|---|---|---|
| -2700.0 | 2026-11-07T21:46:12.004Z | `base:fx-6f2918a74a8416d23bfa` | baseline snapshot (camera=online) |
| -2700.0 | 2026-11-07T21:46:12.004Z | `sess:fx-6b23c0d5f35d1b11f9b6` | session armed |
| -540.0 | 2026-11-07T22:22:12.004Z | `health:fx-1476dee6ecf73dbc2847` | health: vision=offline |
| -0.7 | 2026-11-07T22:31:11.304Z | `health:fx-51d4d3368ba011cea554` | health: usb_watcher=ok |
| -0.5 | 2026-11-07T22:31:11.504Z | `evt:fx-36cfb77296b464dcf07e` | application: app_launched (RemoteDesk Helper) |
| +0.0 | 2026-11-07T22:31:12.004Z | `incident:fixture-c-app-incomplete` | incident raised: rule=unapproved_app_while_armed severity=medium |
| +3.4 | 2026-11-07T22:31:15.404Z | `evt:fx-512ec056322e746b96c0` | application: app_activated (Terminal) |

