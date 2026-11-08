# Product behavior

Status: source snapshot `1bbbfbf`; integration and physical acceptance remain open.

## Problem

Engineers leave shared desks with running jobs, attached development boards and
authenticated applications. Someone else can interfere with that work. Ordinary
collaboration creates many of the same observations: a friend approaches, a dock
reconnects, or a teammate plugs in a board. Airlock must distinguish an observed
event from permission to act and from evidence of harm.

The baseline is a normally locked workstation. Airlock adds an event timeline,
desk context and owner review. Leaving the session unlocked is not a prerequisite
for running a job; compatibility with screen locking must be tested for each
supported workflow.

## Current implementation

The Mac watches USB changes and visible application launches. Its camera pipeline
reports presence and configured interaction zones; face identification is disabled
by default. The included Jetson adapter supplies presence and camera health, not
identity, permission or theft detection.

SpacetimeDB owns sessions, frozen device/application policy, incidents and lock
requests. Person presence alone is soft evidence. An unapproved USB observation
can trigger an incident; presence within ten seconds can raise its severity.
That timing does not establish who connected the device. Local speech can run
before a server response. Nemotron reviews evidence through scoped, read-only
tools; its explanation does not authorize an action.

Screen locks default to dry runs. Actual locking requires the configured owner
policy and local consent; a request is not confirmation. See the
[implementation contract](CONTRACTS.md) and [release record](RELEASE_ACCEPTANCE.md).

## Expected behavior

These are acceptance requirements, including changes still needed below.

| Situation | Required response |
|---|---|
| A friend passes or talks at the desk | Remain quiet. Presence alone must not accuse, lock or escalate. |
| A friend needs access | Owner uses authenticated disarm/rearm and an appropriate OS account. Friendship does not grant permission; task-scoped guest grants are not implemented. |
| An unapproved USB device appears | Report the device event and apply the agreed policy. Do not claim malware, data access or a culprit. Include benign dock resets and development boards in policy trials. |
| A compile, remote session or hardware job is running | Preserve the job. Test normal lock behavior; never substitute shutdown, process termination or a USB reset. |
| A camera, host, database or model goes offline | Name the lost capability, retain available monitoring and show stale state as unverified. Reconnection must not replay an expired action. |

## Open issues

- [Local presence handling](../../backend/app/security/service.py) can still speak
  when someone reaches the desk. Server soft-evidence rules do not make this path quiet.
- The [dashboard](../../frontend/src/airlock/AirlockApp.tsx) labels an incident
  **“Suspected intrusion.”** Replace unsupported intent claims across the UI and speech.
- An unlocked, authenticated owner console lets its current operator use owner
  controls. Decide which actions need fresh authentication or a separate trusted device.
- Existing terminals and browser sessions can be misused without launching an app
  or attaching USB. Launch monitoring cannot cover this; Apple's notification also
  excludes some background applications. [Apple API](https://developer.apple.com/documentation/AppKit/NSWorkspace/didLaunchApplicationNotification).
- Validate monitoring and recovery while the Mac is actually locked, including
  workload completion and independent confirmation of lock state.

## Hardware and value

Use the fixed Jetson/Logitech view and existing speaker. The reported speaker
microphone remains unverified. Reuse the dismantled Zeus enclosure or a state light
only if practical. Its vendor provides separate lighting examples, not an Airlock
status integration. Show armed, degraded and incident states with text as well as
color; stale data must clear any healthy indication. Motion needs measured coverage
benefits over a fixed mount before it earns a role. [SunFounder lighting](https://docs.sunfounder.com/projects/zeus-car/en/latest/get_started/ar_car_light.html).

The value hypothesis is faster, useful review with fewer interruptions. Compare
native protection alone against native protection plus Airlock. Apple already
provides accessory authorization; managed endpoint products provide device-control
policies. Airlock must demonstrate additional value beyond those controls.
[Apple accessories](https://support.apple.com/en-us/102282),
[Microsoft device control](https://learn.microsoft.com/en-us/defender-endpoint/mac-device-control-overview).

Record outcomes using [TEST_PLAN.md](TEST_PLAN.md) and [SCENARIOS.md](SCENARIOS.md).
