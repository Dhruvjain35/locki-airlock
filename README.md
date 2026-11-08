# LOCKI AIRLOCK

LOCKI AIRLOCK guards a workstation while its owner is away. The Mac watches USB devices, app launches and, when armed, the desk camera. A
SpacetimeDB module decides incidents, sessions and lock requests. LOCKI speaks a warning at once, changes its delivery with the security mode,
and answers questions from the recorded evidence. An NVIDIA Nemotron model investigates incidents through approved, read-only tools.

Built for HackUTD 2026 by Dhruv Jain, Siddartha Kodithyala, Areeb Hirani and Mustafa Hassan.

> **Status: integration snapshot, not a finished release.** Areeb's edge runtime and Mission Control, and Mustafa's Nemotron investigator
> service, could not be reached when this repository was created. See [What is not in this snapshot](#what-is-not-in-this-snapshot).


## Existing M4 owner access

For an **Unenrolled** console at `http://127.0.0.1:5174/?client=operator`, use
**Recover existing owner access**. It validates an existing saved/local owner
credential against the requested workstation before connecting, preserves the
operator slot, and never creates a workstation or grants a role. Follow the
[exact M4 recovery, live arming and evidence-approval steps](docs/airlock/OWNER_ACCESS.md).
The workstation panel shows the committed session ID and explicit real/synthetic
provenance. Owner evidence grants use the dedicated incident-scoped contract.

## How it works

```
Mac (M4)                                                SpacetimeDB module locki-w91bw (maincloud)
 IOKit USB / NSWorkspace apps ─SecurityEventV1─▶ SecurityService ─HTTP reducers─▶ ingest_device_event, report_health, record_baseline
 desk camera (MediaPipe) ─▶ person, zones, interaction │  ▲                         │ decides: sessions, incidents, lock requests
        │                                              │  └── WebSocket my_* views ◀┘ (scoped to this Mac's identity)
        ▼                                              ▼
 modes.py: NORMAL → WATCHFUL → DEFENSIVE → CRITICAL ─▶ voice (one Kokoro voice, delivery by mode, P0 queue)
                                                       │
 Nemotron Bolt E4B (Ollama on the M5 over Tailscale+SSH) ◀─ investigator: gated read-only tools, structured report
Jetson / second camera ──ingest_device_event (role jetson)──▶ module (correlates USB + person_in_zone within 10 s)
Browser ──▶ Sid's security dashboard (default route) · Mac backend view and live demo (?product=legacy, #demo)
```

- **The module decides.** Arming, incidents and lock requests belong to SpacetimeDB. The Mac only carries messages, and it reads only the views
  scoped to its own identity (`my_sessions`, `my_incidents`, `my_device_events`, `my_lock_requests`).
- **The local warning never waits.** A USB warning plays from the voice cache before the server answers, so it waits for neither the module
  nor Nemotron.
- **Contracts:** [spacetime/CONTRACT.md](spacetime/CONTRACT.md) (reducers, rows, views) and [docs/airlock/CONTRACTS.md](docs/airlock/CONTRACTS.md)
  (Mac and camera messages, metadata keys, health values, security modes).

## Repository layout

| Path | What | Owner |
|---|---|---|
| `backend/app/security/` | Mac watchers, security service, SpacetimeDB adapter, security modes, demo mode, lock adapter (dry run) | Dhruv |
| `backend/app/vision/airlock/` | Desk camera: person tracking and interaction zones; legacy face code is disabled by default | Dhruv |
| `backend/app/voice/` | Speech queue, Kokoro / Fish Audio, Bluetooth tail fix, per-mode delivery (`delivery.py`), personality lines | Dhruv |
| `backend/app/intelligence/` | Conversation, AIRLOCK investigator (`airlock_agent.py`), Sid's Nemotron transport (`nemotron_transport.py`) | Dhruv, Sid |
| `backend/app/` (other) | FastAPI runtime kept from LOCKED IN: API, bus, telemetry, voice input, and the robot and focus modules it still imports | Dhruv |
| `frontend/src/airlock/`, `frontend/src/airlock-bindings/` | Security dashboard and generated SpacetimeDB bindings | Sid |
| `frontend/src/demo/`, `frontend/src/components/` | Mac backend view and the live demo | Dhruv |
| `spacetime/` | SpacetimeDB TypeScript module: enrollment, roles, sessions, correlation, approvals, lock requests, views | Sid |
| `edge/jetson/` | Jetson camera adapter (Sid's adapter; Areeb's newer runtime is not here yet) | Areeb, Sid |
| `scripts/` | TypeScript bridges, publishers, lock adapter, Nemotron tool adapter, Mac setup | Sid, Dhruv |
| `examples/` | Sandbox case for the protected-tool gate, Code Quest template | Dhruv |
| `docs/airlock/` | Contracts, demo, vision, voice, NVIDIA, handoff, evidence | all |
| `docs/legacy/` | LOCKED IN robot-era docs that the retained code still cites | — |

Integration choices are recorded in [docs/INTEGRATION_DECISIONS.md](docs/INTEGRATION_DECISIONS.md). The sources of this snapshot are in
[docs/MIGRATION.md](docs/MIGRATION.md).

## Hardware and roles

| Machine | Role |
|---|---|
| **M4 MacBook (Dhruv)** | The guarded workstation: backend on `127.0.0.1:8077`, USB and app watchers, desk webcam, speaker (tested with a JLab GO Party over Bluetooth), dashboard dev server on `:5174` |
| **M5 MacBook Pro (Mustafa)** | Ollama with `nemotron-3.5-bolt:e4b`. The M4 reaches it over Tailscale through an SSH tunnel; Ollama itself listens on loopback only |
| **NVIDIA Jetson (Areeb)** | Second camera publishing `person_in_zone` / `camera_*` events as an enrolled `jetson` device |
| **SpacetimeDB maincloud** | Database `locki-w91bw` at `https://maincloud.spacetimedb.com` |

## Setup (M4)

You need macOS on Apple silicon, Python 3.12 with [uv](https://docs.astral.sh/uv/) and Node 24. Owner commands also need the SpacetimeDB CLI 2.14.1.

```sh
git clone https://github.com/Dhruvjain35/locki-airlock.git ~/locki-airlock
cd ~/locki-airlock
make preflight                # checks this Mac without installing anything
make setup                    # pinned backend + frontend deps and the models (backend/models/, checked against models.sha256)
npm ci --prefix spacetime     # only if you work on the module
cp .env.example .env          # then fill in the AIRLOCK block; never commit .env
```

**SpacetimeDB identity.** The Mac creates its own identity on first connect. It keeps the token in `data/security/` (git-ignored, owner-only).
To connect it, run `cd backend && .venv/bin/python scripts/airlock.py spacetime-setup`. The owner then enrolls it as role `mac`. For the
camera, `scripts/airlock.py camera-setup` prints the owner's `enroll_device ... "jetson"` command.

**Nemotron tunnel (M4 → M5).** Put both Macs on the team tailnet, then open the tunnel and leave it running:

```sh
ssh -N -L 11435:127.0.0.1:11434 <user>@<m5-tailscale-name>
```

Then set `LOCKEDIN_AIRLOCK_OLLAMA_URL=http://127.0.0.1:11435`. Use the names from `tailscale status`, never a fixed address in committed config.

**Voice.** `LOCKEDIN_SECURITY_VOICE=am_onyx` speaks every security line in local Kokoro, offline. Pick the output device in the dashboard's
voice settings. Fish Audio is optional: it needs `FISH_AUDIO_API_KEY` in `.env`, and it's skipped while the one security voice is on.

**Camera.** `LOCKEDIN_AIRLOCK_VISION=true`, or `make airlock-mac VISION=1`, turns the desk camera on only while armed or in a demo. Draw the
zones once with `cd backend && .venv/bin/python scripts/airlock_zones.py`: `2` marks the keyboard, `4` the USB ports, `s` saves.

## Live demo (one reliable procedure)

Before you start, check that nothing else is using the camera or the USB monitor, and run only one backend.

```sh
# terminal 1: the M5 tunnel (see above)
# terminal 2: preflight (no microphone, speaker, camera or real lock; each line says REAL or NOT RUN)
cd ~/locki-airlock && make airlock-check
# terminal 2: the Mac backend, with security on, the camera on while armed, and locks as dry runs
make airlock-mac VISION=1 SPACETIME_URL=https://maincloud.spacetimedb.com SPACETIME_DB=locki-w91bw OLLAMA=http://127.0.0.1:11435
# terminal 3: the dashboard
cd ~/locki-airlock/frontend && npm run dev
#   Sid's security dashboard:   http://127.0.0.1:5174/
#   Mac view and live demo:     http://127.0.0.1:5174/#demo
```

Then follow "Each demo" in [docs/airlock/DEMO.md](docs/airlock/DEMO.md):

1. Start the demo and review camera and monitoring readiness. Face enrollment is not required.
2. Arm on Sid's dashboard, and leave the desk.
3. Bring in a volunteer to sit down, type, and plug in a harmless USB stick.
4. Ask "LOCKI, what happened?"
5. Come back and review the observations. The workstation stays armed until authenticated disarm.
6. Disarm on the dashboard.

**Security desk on a second Mac (Areeb's).** On that Mac, after cloning: `npm ci --prefix frontend && make desk`, then open
http://127.0.0.1:5180/?client=desk&view=desk. It is the dashboard as static files that talk only to SpacetimeDB, never to the M4's backend, and it
listens on 127.0.0.1 only. The desk creates its own identity. The owner enrolls it with **Enroll identity → Grant human reviewer access**, with
approval unchecked. It can view this workstation's incidents, evidence and investigation, and acknowledge them. Never make the M4's Vite server
listen beyond 127.0.0.1: its `/api` proxy reaches the backend's routes.

**Arming and disarming.** The authenticated owner may arm directly or choose **Authorize next voice ARM** on the dashboard. The owner reviews
one enrolled Mac, its fresh baseline/app, approved apps and duration, then keeps the console connected. The Mac can consume that one-use
permission within 30 seconds when told “LOCKI, watch my setup.” It confirms only after fresh database readback. Disarming requires the owner.
See [existing M4 integration](docs/airlock/M4_VOICE_ARM.md). If only the M4 CLI is authenticated, the optional
[local owner consent window](docs/airlock/OWNER_CONSENT.md) keeps that credential on the M4; no token enters voice and no second voice service is needed.


## Offline and degraded behaviour

| If this is missing | What happens |
|---|---|
| SpacetimeDB unreachable | Local warnings and modes still run. Events queue and retry when the server is unreachable or returns 5xx; a reducer error is final and recorded |
| Nemotron / M5 unreachable | The investigation falls back to a local model, labelled "not Nemotron", or reports it's unavailable. Warnings still play |
| Camera unavailable | The camera reports `camera_offline` and health `degraded` or `unavailable`. USB and app monitoring continue |
| No internet | `LOCKEDIN_OFFLINE=true` (set by `make airlock-mac`): Fish Audio and model hubs are never contacted, and Kokoro speaks |

## Safety limits

- **Screen locks are dry runs** unless the owner starts the backend with `LOCK=real`, after agreeing to a test with someone there to unlock.
- **Face identification is disabled by default.** The primary camera uses person and zone observations, preserving the PRD's no-biometric-recognition scope.
- **Voice never authorizes.** Arming and disarming need the owner's authenticated action.
- **Fixtures stay labelled.** Demo fixtures and simulated events are marked as such, and the live demo refuses them.
- **Existing face templates stay unused by default.** Legacy face code is retained outside P0, behind a separate optional setting. Camera events carry no images,
  identities or embeddings, and USB serials leave the Mac only as salted hashes.
- **One consumer per device.** Locks on the camera and the USB monitor stop a second watcher. Sid's standalone `app.security.voice_service`
  is a second voice path, and `mac_observer.py` is a second USB poller. Don't run them beside `make airlock-mac`
  ([decision 6](docs/INTEGRATION_DECISIONS.md)).

## Measured performance (real runs)

| Path | Measured |
|---|---|
| USB insertion: server commit → Mac row (WebSocket) | 301 ms |
| Mac row → audio start | 222 ms |
| Commit → audio, total | 523 ms (live incident `23000b77…`, 2026-11-07) |
| Local event → audio (from the cache) | 133–335 ms |
| Bluetooth output latency (JLab GO Party) | +466 ms, padded so the last syllable is heard |
| Nemotron investigation of a real app launch | 4.4 s, 4 tool calls (run-ba05ba8e294e) |

Camera numbers are in [docs/airlock/VISION.md](docs/airlock/VISION.md) and [docs/airlock/DEMO.md](docs/airlock/DEMO.md).

## Verified so far

| Check | Status |
|---|---|
| Unit and contract tests | Counts at this snapshot are in [docs/MIGRATION.md](docs/MIGRATION.md) |
| Real M4 hardware | Team reports: USB (IOKit), app launches, webcam person detection, audible warnings over Bluetooth. The new face-disabled default has not been activated or verified on the running M4 |
| Live SpacetimeDB | Current owner-armed session and committed non-simulated Jetson person rows independently read back; Mac USB/audio timings remain team reports. Separate M4 camera enrollment is still an owner action |
| Real Nemotron inference | Team reports one investigation from the M4 through the M5 (above); current host artifacts and scoped Mustafa access remain unverified |
| Real Jetson hardware | Team reports operation; stored non-simulated person events verified. Hardware inventory, soak, unplug recovery and accuracy trials remain open |
| Real screen lock | **Not run** (dry run only, by design) |

## What is not in this snapshot

- **Areeb's edge runtime and Mission Control.** These are `feature/airlock-edge` (last known commit `80072cd`) and
  `feature/airlock-mission-control`. Neither was on this Mac, on GitHub or in the team's earlier repository, and Areeb's Mac couldn't be
  reached. `edge/jetson/` holds Sid's earlier adapter only.
- **Mustafa's Nemotron investigator service.** It couldn't be reached, because SSH to the M5 needs a password. The Mac's investigator and Sid's
  transport are in this repository. Model weights and Ollama files never belong in git.

To add a missing lane, follow "Adding your lane" in [CONTRIBUTING.md](CONTRIBUTING.md). It copies a snapshot of your files without the old history.

## Tests

Run from the repository root with Python 3.12, uv and Node.js 24. Install the locked
software test dependencies first:

```sh
uv sync --project backend --frozen --group dev --extra vision
npm ci --prefix frontend --no-fund
npm ci --prefix spacetime --no-fund
```

The vision extra supplies the locked OpenCV package used to decode synthetic
frames and verify markers in the tests. Model weights are separate and are not
downloaded by this setup; tests requiring them skip when the files are absent.

```sh
LOCKEDIN_MODELS_DIR="$PWD/backend/models" backend/.venv/bin/python -m pytest -q backend/tests   # full backend suite
npm run check --prefix spacetime && npm test --prefix spacetime                     # module types and transactional tests
npm test --prefix frontend && npm run build --prefix frontend                       # dashboard
node frontend/node_modules/tsx/dist/cli.mjs --test scripts/*.test.ts scripts/tests/*.test.ts   # bridges and adapters
PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s edge/jetson/tests            # Jetson adapter
```

CI (`.github/workflows/airlock.yml`) runs the software checks. Physical camera, USB, audio, model inference and screen-lock checks are opt-in
acceptance runs, and CI never fakes them.
