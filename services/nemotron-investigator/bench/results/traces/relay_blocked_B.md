# Investigation `inv_0a672a70ccf14cc297d0`  [TEST DATA]

> TEST DATA: fixture evidence from fixtures/scenarios, NOT read from the live LOCKI database

**TEST DATA. Official severity high (rule unrecognized_person_usb). Nemotron's evidence review: medium risk, medium confidence. The official high severity stands.**

- Incident: `incident:fixture-b-person-then-usb`
- Official severity (LOCKI rule, authoritative): **high** (rule `unrecognized_person_usb`); effective severity **high** (official_severity)
- Model: `nemotron-3.5-bolt:e4b` (local Ollama on the M5)
- Status: **completed_with_warnings**
- Evidence source: FIXTURE B_unknown_person_then_usb (not a live database read) (live=False)
- Total 10709 ms; model 10642 ms; tools 3.6 ms; first tool decision 323.47 ms

## What Nemotron was given

System prompt (abridged to first line):
> You are the LOCKI AIRLOCK security investigator, running locally on NVIDIA Nemotron.

User message (the entire task input):
> Investigate incident incident:fixture-b-person-then-usb.

Plus the JSON schemas of 7 read-only tools. No evidence was pre-assembled.

## Step-by-step

### Step 1: Nemotron inference 267 ms (prompt 987 tok, output 24 tok)
- Selected `get_incident` with arguments `{"incident_id":"fixture-b-person-then-usb"}`
  - Executed `get_incident`: **ok** in 0.40 ms (arguments canonicalized): incident unrecognized_person_usb/high, 2 linked events
    - evidence: `incident:fixture-b-person-then-usb`

### Step 2: Nemotron inference 387 ms (prompt 1328 tok, output 26 tok)
- Selected `get_session_status` with arguments `{"incident_id":"fixture-b-person-then-usb"}`
  - Executed `get_session_status`: **ok** in 0.37 ms (arguments canonicalized): session ARMED at incident time
    - evidence: `sess:fx-df7e70e5021544f4834b`

### Step 3: Nemotron inference 333 ms (prompt 1667 tok, output 26 tok)
- Selected `get_incident_evidence` with arguments `{"incident_id":"fixture-b-person-then-usb"}`
  - Executed `get_incident_evidence`: **ok** in 0.34 ms (arguments canonicalized): 2/2 linked events retrieved
    - evidence: `evt:fx-1e0acb09a5258301f7d8`, `evt:fx-4328f1695475437bcbd4`

### Step 4: Nemotron inference 551 ms (prompt 2174 tok, output 42 tok)
- Selected `get_recent_usb_events` with arguments `{"incident_id":"fixture-b-person-then-usb","limit":10,"window_s":1800}`
  - Executed `get_recent_usb_events`: **policy_denied** in 0.71 ms (arguments canonicalized): blocked by NeMo Relay policy before execution: policy locki.usb_lookback_policy: window_s=1800 exceeds the 600 s USB lookback allowed without human approval; a request with window_s of at most 600 is permitted

### Step 5: Nemotron inference 297 ms (prompt 2393 tok, output 42 tok)
- Selected `get_recent_vision_events` with arguments `{"incident_id":"fixture-b-person-then-usb","limit":10,"window_s":1800}`
  - Executed `get_recent_vision_events`: **ok** in 0.72 ms (arguments canonicalized): 4 vision events (of 4)
    - evidence: `evt:fx-619ea6eea2096aeae1ba`, `evt:fx-29774085924ca50857a0`, `evt:fx-b79ddf4a4f695088e2be`, `evt:fx-1e0acb09a5258301f7d8`

### Step 6: Nemotron inference 560 ms (prompt 3098 tok, output 42 tok)
- Selected `get_recent_application_events` with arguments `{"incident_id":"fixture-b-person-then-usb","limit":10,"window_s":1800}`
  - Executed `get_recent_application_events`: **ok** in 0.44 ms (arguments canonicalized): 1 application events (of 1)
    - evidence: `evt:fx-d0596f2bd1b01ee7c47d`

### Step 7: Nemotron inference 357 ms (prompt 3487 tok, output 28 tok)
- Selected `get_device_baseline_status` with arguments `{"incident_id":"fixture-b-person-then-usb"}`
  - Executed `get_device_baseline_status`: **ok** in 0.61 ms (arguments canonicalized): 2 known USB devices; 1/1 nearby USB events not in baseline; camera=ok (device_health)
    - evidence: `evt:fx-4328f1695475437bcbd4`, `health:fx-d19e16132e28fb7b1424`, `health:fx-9a1a4996d956a7368459`, `sess:fx-df7e70e5021544f4834b`, `base:fx-73f931df0a26ad17809f`

### Step 8: Nemotron inference 971 ms (prompt 4079 tok, output 50 tok)
- Selected `finish_investigation` with arguments `{"reason":"The incident involved an unrecognized person near a desk followed by an unknown USB mass storage device, and all available read-only tools have been exhausted to confirm the nature of the threat."}`
  - Executed `finish_investigation`: **finished** in 0.00 ms: model chose to stop: The incident involved an unrecognized person near a desk followed by an unknown USB mass storage device, and all available read-only tools have been exhausted to confirm the nature of the threat.

### Final assessment call (finalize): 6919 ms, 880 output tokens, constrained JSON decoding

## Assessment (after deterministic grounding)

**Official severity:** high (LOCKI rule; never changed by Nemotron)

**Nemotron's risk (its own view):** medium (confidence medium). An unrecognized person was observed near a desk immediately before an unknown mass storage USB device was connected while the system was armed.

**Voice view text (what LOCKI speaks):** Test data only, not the live LOCKI database. LOCKI rated this incident high severity. A hand was observed near a USB port. A USB device classified as mass storage was connected with an unknown classification. Not available to the investigation: USB events. Nemotron's evidence review: medium risk, medium confidence. The official high severity stands. This needs human review.

**Summary (deterministic_due_to_flags):** Official severity high. Nemotron assessed risk medium with medium confidence. A hand was observed near a USB port. A USB device classified as mass storage was connected with an unknown classification. Human review is required.

**Observed facts (each cites retrieved evidence):**
- A hand was observed near a USB port. [evt:fx-1e0acb09a5258301f7d8]
- A USB device classified as mass storage was connected with an unknown classification. [evt:fx-4328f1695475437bcbd4]
- A face was recognized as unrecognized. [evt:fx-29774085924ca50857a0]
- A person was detected near the desk. [evt:fx-619ea6eea2096aeae1ba]
- The Finder application was activated during the incident window. [evt:fx-d0596f2bd1b01ee7c47d]

**Possible explanations (inferred):**
- (medium) The unknown USB device was connected by an unrecognized individual.
- (low) The USB device is not confirmed to be malware based on read-only data.
- (low) The hand near the port does not confirm the act of insertion.

**Missing information (unknown):**
- The identity of the person who connected the device cannot be established.
- The specific contents or intent of the unknown USB device are unknown.
- The reason for the 'unrecognized_person_usb' rule trigger is not fully detailed.
- The 'get_recent_usb_events' tool failed with a policy denial.
- No application events directly indicate malicious activity.
- [system] USB events: get_recent_usb_events returned no_successful_call; that evidence is absent

**Recommended actions (all require human approval; none executed by the agent):**
- Review physical access logs for the workstation.: Determine if authorized personnel were present.
- Quarantine the workstation for forensic imaging.: Preserve the state of the system.
- Check for subsequent system behavior changes.: Look for post-incident activity.

**Advisories:**
- `model_risk_below_official_severity`: Nemotron rated risk 'medium', below the official severity 'high'; the official severity stands (effective_severity keeps it)
- `summary_replaced_by_grounding`: the model's summary tripped a grounding rule and was replaced

Validation: 5/5 facts grounded, citation accuracy 1.0, 1 flags.

## Evidence timeline (rebuilt deterministically from retrieved evidence)

| t (s from incident) | time (UTC) | evidence | event |
|---|---|---|---|
| -1800.0 | 2026-11-07T21:35:31.880Z | `base:fx-73f931df0a26ad17809f` | baseline snapshot (camera=online) |
| -1800.0 | 2026-11-07T21:35:31.880Z | `sess:fx-df7e70e5021544f4834b` | session armed |
| -92.0 | 2026-11-07T22:03:59.880Z | `evt:fx-619ea6eea2096aeae1ba` | vision: person_detected |
| -88.5 | 2026-11-07T22:04:03.380Z | `evt:fx-29774085924ca50857a0` | vision: face_unrecognized |
| -41.0 | 2026-11-07T22:04:50.880Z | `evt:fx-b79ddf4a4f695088e2be` | vision: desk_interaction |
| -6.2 | 2026-11-07T22:05:25.680Z | `evt:fx-1e0acb09a5258301f7d8` | vision: hand_near_usb_port |
| -1.5 | 2026-11-07T22:05:30.380Z | `health:fx-9a1a4996d956a7368459` | health: vision=ok |
| -0.8 | 2026-11-07T22:05:31.080Z | `health:fx-d19e16132e28fb7b1424` | health: usb_watcher=ok |
| -0.3 | 2026-11-07T22:05:31.580Z | `evt:fx-4328f1695475437bcbd4` | usb: usb_device_connected (Ultra) |
| +0.0 | 2026-11-07T22:05:31.880Z | `incident:fixture-b-person-then-usb` | incident raised: rule=unrecognized_person_usb severity=high |
| +9.0 | 2026-11-07T22:05:40.880Z | `evt:fx-d0596f2bd1b01ee7c47d` | application: app_activated (Finder) |

