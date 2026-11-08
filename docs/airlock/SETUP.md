# LOCKI AIRLOCK setup and operation

These are the Mac and hardware operating instructions previously held in the root README. For the local database/dashboard rehearsal, start with the [local integration guide](README.md). Review [release acceptance](RELEASE_ACCEPTANCE.md) before treating a source feature or a historical measurement as verified on the running machines.

The separate [Nemotron investigator service](../../services/nemotron-investigator/README.md) is present in this repository. Its [integration contract](../../services/nemotron-investigator/INTEGRATION.md) covers the service API and live evidence access. The Ollama tunnel below is the preserved direct-model path; service deployment, model readiness and authorized evidence scope must be checked independently.

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

## Run the Mac and live demo

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

Then follow "Each demo" in [docs/airlock/DEMO.md](DEMO.md):

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
See [existing M4 integration](M4_VOICE_ARM.md). If only the M4 CLI is authenticated, the optional
[local owner consent window](OWNER_CONSENT.md) keeps that credential on the M4; no token enters voice and no second voice service is needed.


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
  ([decision 6](../INTEGRATION_DECISIONS.md)).

## Historical measurements and current verification

Earlier team measurements and their limits remain in [VISION.md](VISION.md), [DEMO.md](DEMO.md), and the [acceptance record](RELEASE_ACCEPTANCE.md). A historical timing, stored event or imported service does not prove the current physical host is configured or working. Record the running source/configuration and actual hardware-to-response evidence for a new trial.

## Software checks

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
