# LOCKI AIRLOCK

LOCKI AIRLOCK helps engineers monitor an unattended workstation in a shared workspace. It combines desk-camera observations with Mac USB changes and visible app launches in a shared incident timeline. Owners configure the response; authorized reviewers can inspect and acknowledge incidents. Local warnings run independently of the investigation model; an ordinary macOS screen lock requires the owner's response policy and local consent.

Built for HackUTD 2026 by Dhruv Jain, Siddartha Kodithyala, Areeb Hirani and Mustafa Hassan.

**Status: integration prototype.** Source is available for the Mac runtime, dashboard, database module, Jetson adapter and Nemotron investigator service. Running-device configuration, physical accuracy, actual lock/unlock behavior and model integration require the evidence listed in [release acceptance](docs/airlock/RELEASE_ACCEPTANCE.md).

## What it does

- Authenticated arming captures a fresh device baseline and freezes the session's response policy. Disarming requires owner authority; speech alone grants no permission.
- Mac watchers report USB changes and visible app launches. An optional camera reports desk presence and configured zone observations.
- Deterministic rules create incidents for unapproved digital observations and correlate nearby physical observations. Presence alone does not request an automatic lock. An unfamiliar device or person is not proof of malicious intent.
- The Mac produces local warnings, while authorized consoles share incidents, evidence, acknowledgements and recovery state. Configured phone notifications report their actual delivery stage.
- Nemotron investigates through scoped, read-only evidence tools. A separate managed-workflow gate requires authorization before selected protected callbacks execute; it does not control arbitrary apps or agents.

## Architecture

```text
Mac USB/app monitors ────┐
                        ├──► SpacetimeDB ──► owner / reviewer consoles
Jetson or desk camera ──┘      sessions, incidents, permissions
                                   │
                                   └──► scoped evidence ──► Nemotron investigator
Mac runtime: local voice + consented screen-lock adapter
```

SpacetimeDB owns enrollment, session state, incident rules and action permissions. The Mac applies approved responses; the model explains evidence and cannot establish guilt. See the [database contract](spacetime/CONTRACT.md) and [device contracts](docs/airlock/CONTRACTS.md) for the exact boundaries.

## Product boundaries

This prototype detects and reports selected events. It does not promise theft prevention, complete process monitoring, malware detection, recovery of copied data, or protection for tools outside its managed gateway. Someone using an already-open application may produce no new app-launch event. Temporal correlation does not identify who performed an action.

Face identification is disabled by default. Real screen locks are opt-in; the default is a dry run, and a request is distinct from confirmed OS lock. Screen locking is not shutdown. Local presence speech still needs evaluation against the desired quiet behavior around coworkers. Robot motion and firmware changes are outside this product's operating scope.

The [product specification](docs/airlock/PRODUCT.md) separates intended behavior from current capability. [Scenarios](docs/airlock/SCENARIOS.md) and the [test plan](docs/airlock/TEST_PLAN.md) define the failures and recovery paths to evaluate; planned cases are not passing results.

## Getting started

For a local database and dashboard rehearsal, install Node 24.22.1+ in the Node 24 line and SpacetimeDB CLI 2.14.1, then run:

```sh
npm ci --prefix spacetime
npm ci --prefix frontend
make airlock
```

Open the URL printed by the launcher. This path uses explicitly labelled synthetic observations; it does not start a camera, microphone, model, robot or real screen-lock adapter. Follow the [local integration guide](docs/airlock/README.md) for identities, two-console review and persistence.

For the physical Mac, camera, voice and investigator paths, follow [setup and operation](docs/airlock/SETUP.md).

## Repository layout

| Path | Responsibility |
|---|---|
| `backend/app/` | Mac security runtime, vision, voice and conversation |
| `frontend/` | Owner/reviewer console, Mac demo view and generated bindings |
| `spacetime/` | Authoritative state, incident rules, permissions and scoped views |
| `edge/jetson/` | Independent camera publisher adapter |
| `services/nemotron-investigator/` | Scoped investigation service, tools and integration API |
| `scripts/` | Setup, bridges, publishers, adapters and verification utilities |
| `examples/` | Managed-workflow and sandbox examples |
| `docs/airlock/` | Product behavior, operation, contracts and acceptance evidence |

## Tests and contributing

Use the [software check commands](docs/airlock/SETUP.md#software-checks), [physical accuracy protocol](docs/airlock/ACCURACY_TRIALS.md) and [release gates](docs/airlock/RELEASE_ACCEPTANCE.md). Automated software checks do not substitute for real hardware, audio, model or recovery trials.

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for branches, review and private-data exclusions. Historical migration notes remain in [docs/MIGRATION.md](docs/MIGRATION.md) and `docs/legacy/`. Authorship, upstream provenance and licensing are recorded in [SOURCE_NOTICE.md](SOURCE_NOTICE.md).
