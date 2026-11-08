# LOCKI AIRLOCK documentation

Start with the [root README](../../README.md): what AIRLOCK is, how to run it and what has been verified. This page lists every doc by purpose. **Current reference** describes `main`. **Runbooks** are procedures. **Evidence** is dated and can be superseded. **Historic** docs come from LOCKED IN.

## Current reference

| Doc | Covers |
|---|---|
| [PROJECT_MAP.md](PROJECT_MAP.md) | Components and owners, one incident end to end, repository layout, machines and ports, manual runs, offline behaviour, safety rules, software checks |
| [spacetime/README.md](../../spacetime/README.md), [spacetime/CONTRACT.md](../../spacetime/CONTRACT.md) | The SpacetimeDB module, and its exact reducers, rows and views |
| [CONTRACTS.md](CONTRACTS.md) | Mac and camera messages, metadata keys, health values, security modes |
| [INVESTIGATOR_INTEGRATION.md](INVESTIGATOR_INTEGRATION.md) | Owner evidence grants, the investigator's views, official severity versus the model's view |
| [Investigator README](../../services/nemotron-investigator/README.md), [API contract](../../services/nemotron-investigator/INTEGRATION.md) | Mustafa's Nemotron investigator service |
| [edge/README.md](edge/README.md) | Areeb's Jetson runtime `locki_edge`, including its alerts |
| [ARM_DELEGATION.md](ARM_DELEGATION.md) | One-use, owner-authorized voice ARM |
| [LOCK_ADAPTER.md](LOCK_ADAPTER.md) | The optional screen lock, dry run by default |
| [VISION.md](VISION.md) | The Mac's optional desk camera and its face setting |
| [TEAMMATE_INTERFACE.md](TEAMMATE_INTERFACE.md), [SecurityEventV1.schema.json](SecurityEventV1.schema.json), [contracts/](contracts/) | Publisher interface, event schema, generated schemas and fixtures (`make airlock-contracts`) |
| [CODE_QUEST.md](CODE_QUEST.md) | The managed coding exercise and its protected callback |
| [DECISIONS.md](DECISIONS.md) | Design decisions and the claims ledger (2026-11-07) |
| [PRD.md](PRD.md) | Original requirements. A historical plan; it does not override the source |
| [VOICE.md](VOICE.md), [edge/jetson/README.md](../../edge/jetson/README.md), [MAC_CAPTURE.md](MAC_CAPTURE.md), [PUBLISH_STREAM.md](PUBLISH_STREAM.md) | Retained alternatives: Sid's standalone voice service, earlier Jetson adapter, Mac bridge and native-app publisher. Never run one beside its active counterpart |

## Runbooks

| Doc | Use it to |
|---|---|
| [Judge-day runbook](../../scripts/airlock_launcher/RUNBOOK.md) | Start everything with the one-action launcher, read READY, recover, shut down safely |
| [New-Mac setup](../../scripts/airlock_launcher/NEW_MAC_SETUP.md) | Install a new protected Mac with its own identities (not yet verified on Areeb's Mac) |
| [edge/DEPLOY.md](edge/DEPLOY.md) | Deploy the Jetson publisher (its PENDING markers predate the live `jetson-01` run) |
| [OWNER_ACCESS.md](OWNER_ACCESS.md) | Recover owner access, arm and approve evidence (uses M4 example values) |
| [M4_VOICE_ARM.md](M4_VOICE_ARM.md), [OWNER_CONSENT.md](OWNER_CONSENT.md) | Run voice ARM, and keep a CLI-only owner credential on its own Mac |
| [DEMO.md](DEMO.md) | Run the Mac camera demo mode at `#face-setup`. It predates the launcher and the Jetson run |
| [ACCURACY_TRIALS.md](ACCURACY_TRIALS.md) | Run consented volunteer trials (protocol only; no results yet) |
| [Local prototype runbook](#local-prototype-runbook) | Rehearse on a private local SpacetimeDB with synthetic events (below) |

## Evidence and records

These are dated. Where they disagree with the root README's verification table, the README is newer.

| Doc | Records |
|---|---|
| [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md), [PRD_STATUS.md](PRD_STATUS.md) | Requirement-by-requirement acceptance and status, last updated before the command-center merge |
| [evidence/](evidence/) | Verification receipts (JSON) and the accuracy-trial template |
| [INTEGRATION_DECISIONS.md](../INTEGRATION_DECISIONS.md), [MIGRATION.md](../MIGRATION.md) | How the lanes were merged and the repository assembled |
| [HANDOFF.md](HANDOFF.md), [M4_INTEGRATION.md](M4_INTEGRATION.md), [INTEGRATION.md](INTEGRATION.md), [NVIDIA.md](NVIDIA.md) | Point-in-time handoffs and coordination, superseded in places |
| [Investigator log](../../services/nemotron-investigator/CONTEXT.md) | Mustafa's dated log of the M5 service, including its live runs |

## Historic (LOCKED IN)

AIRLOCK still runs on LOCKED IN's runtime, so these stay, and code cites several of them. AIRLOCK's requirements and no-motor, no-flash rules supersede their operating instructions. Provenance and licensing: [SOURCE_NOTICE.md](../../SOURCE_NOTICE.md).

- [docs/legacy/](../legacy/): robot-era design, hardware, safety and test docs, and the pre-migration README.
- [MODELS.md](../MODELS.md) and [NVIDIA_EVALUATION.md](../NVIDIA_EVALUATION.md): still describe the voice and speech-recognition stack AIRLOCK uses.
- [AUDIO_TESTS.md](../AUDIO_TESTS.md), [VOICE_PERSONALITY.md](../VOICE_PERSONALITY.md), [VOICE_TESTS.md](../VOICE_TESTS.md), [OFFLINE.md](../OFFLINE.md), [PERFORMANCE.md](../PERFORMANCE.md), [M5_BOLT.md](../M5_BOLT.md) and [benchmarks/](../benchmarks/): voice, offline, performance and model notes from LOCKED IN.

To contribute, see [CONTRIBUTING.md](../../CONTRIBUTING.md). Its branch flow names a `develop` branch that origin does not have yet.

## Local prototype runbook

AIRLOCK's authority is the TypeScript module in `spacetime/`. The React console reads caller-scoped SpacetimeDB views. FastAPI's existing voice interface remains a separate optional telemetry connection. The default demo uses explicitly labelled synthetic observations and does not start a camera, model, microphone, notification, robot or screen-lock adapter.

### Start the local prototype

Requirements: SpacetimeDB CLI **2.14.1**, Node **24.22.1+ in the Node 24 line**, and the project npm dependencies. The launch script first uses `~/.local/share/locki-airlock/tools/node_modules/node/bin/node`, then the current Node; override with `AIRLOCK_NODE_BIN`.

```bash
npm ci --prefix spacetime
npm ci --prefix frontend
make airlock
```

Open the Vite URL printed by the command, normally `http://127.0.0.1:5174`. `make airlock` reuses a healthy local database or starts one with persistent files under `data/airlock-spacetime/`, publishes with `--delete-data=never`, generates the SDK into the tracked `frontend/src/airlock-bindings/` (check `git status` afterwards), builds the UI and starts Vite. Stopping it terminates only a database process it started; an existing server is left running. Data is retained.

Useful individual commands:

```bash
scripts/airlock_dev.sh server      # persistent DB in foreground
make airlock-build                 # check, nondestructive publish, generate, UI build
scripts/airlock_dev.sh frontend    # UI only
make airlock-verify                # real DB integration scenarios
scripts/airlock_dev.sh status
```

`AIRLOCK_SPACETIME_URI` defaults to `http://127.0.0.1:3000`; `AIRLOCK_DATABASE` defaults to `locki-airlock-sid`. The build helper deliberately publishes only to a loopback host. LAN sharing needs an explicit server listen address, a reachable server URL in each console's connection settings, and `AIRLOCK_VITE_HOST=0.0.0.0`. Keep the Vite and database ports reachable only on the intended network. Never share Vite this way on a Mac that runs the AIRLOCK backend: Vite proxies `/api` and `/ws` to it. Use the static security desk (`make desk`) there instead; see [running the Mac by hand](PROJECT_MAP.md#running-the-mac-by-hand).

The earlier local `locki-airlock` fixture database is retained. Cloud CLI login changed the publishing identity, so the offline workflow uses a fresh owned `locki-airlock-sid` database rather than deleting or attempting to seize the old one.

The team's cloud module is `locki-w91bw` at `https://maincloud.spacetimedb.com`. To use its existing deployment without republishing it, run `make airlock-cloud`, or:

```bash
AIRLOCK_SPACETIME_URI=https://maincloud.spacetimedb.com AIRLOCK_DATABASE=locki-w91bw \
  scripts/airlock_dev.sh frontend
```

The same environment before `scripts/airlock_dev.sh verify` runs the independent tests against that real cloud module. Test runs create uniquely named fixture workstations in the team database and preserve existing data, so prefer the local database for routine runs.

### Two distinct authorized consoles

1. Open `/?client=operator` and choose **Register a workstation**. This identity becomes its owner.
2. Open `/?client=reviewer` in a second browser context or on a second computer. Copy its **public identity**.
3. In the owner console, choose **Enroll identity**, grant that identity the reviewer role, and optionally grant protected-action approval rights.
4. Arm a **synthetic session** from the owner console (**Advanced arm…**, Synthetic). Send the labelled fixture **Person + unknown USB · correlated** from the synthetic rehearsal panel. Both clients should show the same committed incident ID.
5. Acknowledge from the reviewer, resolve from the owner, reload or reconnect, and inspect the server audit order.

Profiles create separate token-storage slots, not roles. The server enrollment determines authority. A new identity has no existing workstation access. Tokens are bearer credentials; copy only public identities when enrolling another person. This local prototype uses host-issued identities, not a deployed organizational SSO or account-recovery system.

### Independent verification

`scripts/airlock_verify.ts` creates seven independent identities and unique workstation and session IDs, and uses the real generated SDK and HTTP API. It never resets an existing database. It verifies private-read isolation, control and publisher roles, USB baseline behaviour, camera soft observations, 14 correlation boundary and order vectors, server-timer health expiry, replay conflicts, stale and malformed events, workstation and session separation, approval permissions, digest, expiry and one-time use, actual managed callbacks, revocation, and reconnect.

The correlation vectors wait about 21 seconds once so every observation genuinely occurs inside its armed session. The timing report measures **local USB reducer publication to each independently subscribed SDK cache** with 10 ms cache polling. It does not measure hardware detection, rendering, LAN latency or a second physical laptop.

Each run writes `data/airlock-verification/<run>/report.json`. The sibling `checkpoint.private.json` contains a saved owner token with mode `0600`, and is intentionally under the git-ignored `data/`. To verify disk persistence, stop and restart the same SpacetimeDB server with the same data directory, then run:

```bash
"${AIRLOCK_NODE_BIN:-$HOME/.local/share/locki-airlock/tools/node_modules/node/bin/node}" \
  frontend/node_modules/tsx/dist/cli.mjs scripts/airlock_verify.ts \
  --check-checkpoint data/airlock-verification/RUN/checkpoint.private.json
```

The check writes a sanitized `persistence-check.json` beside the private checkpoint. The actual coordinated local server restart was verified for run `verify-1794085398044-bb08ed7e`: the same token recovered 29 committed incidents.
