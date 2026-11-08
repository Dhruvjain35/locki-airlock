# AIRLOCK demo mode

Demo mode uses person and desk-interaction observations. Clicking Start demo creates an isolated demo session with its own folder (`data/airlock/demo/<id>/`) and a live camera preview. The owner reviews readiness, arms through the authenticated owner controls, leaves, and reviews a consenting volunteer's activity before disarming. Face enrollment and recognition are not required.

Face identification defaults off, preserving the original PRD. The camera does not read existing owner templates or construct face models, and enrollment and recognition-trial requests are refused. The retained `LOCKEDIN_AIRLOCK_FACE_IDENTIFICATION` setting is only for separately reviewed legacy work outside P0. This repository change has not been activated or verified on the already-running M4 process.

Reset demo and End demo delete only that demo's folder. In the optional legacy path that folder can hold a demo face template. Reset and End never touch:

- the production owner template
- device identities and credentials
- anything in SpacetimeDB

The timings are kept in `data/airlock_runs/demo-<id>.json`.

## Live and rehearsal

| | LIVE | REHEARSAL |
|---|---|---|
| Camera, person detection, configured hand zones | real | real |
| USB devices | real (macOS IOKit) | real, plus a **Fixture USB** button: a pretend arrival labelled FIXTURE, never spoken, never measured |
| SpacetimeDB, speech, Nemotron | real when connected; readiness shows each one | same |
| Labels | "Live: real hardware and services" | "Rehearsal: fixtures labelled", in amber |

A live demo refuses fixtures.

## Start a fresh live demo

### Once, before the event

1. **Models.** Confirm the approved local `efficientdet_lite0.tflite` person model and `hand_landmarker.task` for configured hand zones. Face weights are not required. Software tests use injected inference and need no model downloads.
2. **Zones.** Recommended, and nothing else may be using the camera. Run `.venv/bin/python scripts/airlock_zones.py`. Press `2` and click the corners of the keyboard, then `4` for the USB ports, then `s` to save.
3. **Camera in SpacetimeDB.** This is needed for the physical-plus-USB correlation in the database.
   1. Run `.venv/bin/python scripts/airlock.py camera-setup`. It prints the owner's `enroll_device ... "jetson"` command; run that with your SpacetimeDB login.
   2. Run `.venv/bin/python scripts/airlock.py camera-probe`.
   3. Set `"enabled": true` in `data/airlock_vision/spacetime.json`.
4. **Nemotron.** Keep the tunnel to Mustafa's M5 open on `127.0.0.1:11435`. Readiness shows "Nemotron" when it answers.

### Each demo

1. **Backend.** In terminal 1, run `cd ~/locki-airlock && make airlock-mac VISION=1`. The speaker is on; add `QUIET=1` for silence. The microphone needs your go-ahead: start it from the dashboard's voice panel, or with `LOCKEDIN_VOICE_AUTOSTART=true`.
2. **Dashboard.** In terminal 2, run `cd ~/locki-airlock/frontend && npm run dev`, then open http://127.0.0.1:5174/#demo (or press **Demo** in the top bar).
3. **Start demo.** The camera turns on. The readiness card shows what is real right now.
4. **Review coverage.** Confirm person detection and the configured zones in the preview. No identity is inferred, and there is no face-enrollment step.
5. **Arm.** Say "LOCKI, secure my workstation", and LOCKI asks for confirmation. With SpacetimeDB connected, arm on Sid's dashboard, or run the `arm_session` line from `scripts/airlock.py owner-commands`. Without SpacetimeDB, arm on the demo page with the owner code.
6. **Leave.** The mode returns to NORMAL once nobody is seen.
7. **The volunteer:**
   - **Sits down:** WATCHFUL, and "Hey, someone's at your workstation while it's armed."
   - **Types:** DEFENSIVE. A P0 alert interrupts any reply, and a local incident opens.
   - **Plugs in a harmless USB stick:** CRITICAL. The warning names what the camera saw. If the camera is enrolled, SpacetimeDB's correlated incident follows.
8. **"LOCKI, what happened?"** The investigation runs on Nemotron when reachable. Otherwise the fallback is labelled "not Nemotron".
9. **Come back.** Review the observations while the workstation stays armed. The default pipeline reports activity without identifying who returned.
10. **Ask questions.** "Who was near my workstation?", "Did somebody touch my laptop?" Both are answered from tracked events.
11. **Disarm** on Sid's dashboard, or with the owner code. A spoken "disarm" asks for that confirmation and never disarms by itself.
12. **Reset demo** for the next judge (a new empty demo), or **End demo**.

### One user of each device

- **Camera:** an armed session or a demo in the backend holds `data/airlock_vision/camera.lock`. The bench, zones and face scripts refuse while it is held, and the demo refuses to start beside the LOCKED IN focus camera.
- **USB monitor:** the backend holds `data/security/usb-monitor.lock`. `airlock_watch.py` and the audible check need the backend stopped.

## Feature status (A to P)

| | Feature | Status |
|---|---|---|
| A | Legacy face enrollment | Retained outside P0 and disabled by default. Scripted tests opt in explicitly. |
| B | Conversation, microphone, voice | The voice session's domain. The speaker works; the microphone starts only with your go-ahead. |
| C | Voice arming | Live intent ("secure my workstation", "keep an eye on my computer", "protect my setup"). Confirmation is on the dashboard. |
| D | Person detection and interaction zones | Team reports M4 webcam person detection; calibrated zones and the new default need a coordinated merged-host trial. |
| E | NORMAL → WATCHFUL → DEFENSIVE → CRITICAL | Software tested with scripted camera events; a live volunteer run of this default is pending. OWNER_RETURNED belongs to the optional legacy face path. |
| F | Immediate warnings with P0 interruption | Live. Measured: 143–335 ms from the event to audio, plus 466 ms Bluetooth (JLab). |
| G | Real USB detection | Live (macOS IOKit). |
| H | Camera plus USB correlation in SpacetimeDB | Mac USB incidents are live. The camera's events need the camera device enrolled (owner command) and enabled. |
| I | Dashboard timeline, confidence, evidence, health | Demo view: local timeline, readiness and measurements. The database incident timeline is on Sid's dashboard. |
| J | Nemotron investigation | Ran earlier for a real app launch (run-ba05ba8e294e, 4 tool calls, 4.4 s). The camera evidence tool is added. It needs the M5 tunnel. |
| K | Legacy owner recognition | Retained outside P0 and disabled by default; not required for arming or disarming. |
| L | Follow-up questions | Live: deterministic answers from tracked events, plus the observed summary in every model turn. |
| M | Owner-controlled USB trust enrollment | **Not built.** Arming takes the connected devices as the baseline; there is no "trust this device" flow yet. |
| N | Voice disarm with owner authorization | Live. A voice never disarms; the owner confirms on the dashboard, or with the code locally. |
| O | Readiness and degraded indicators | Live readiness card: camera, models, zones, USB monitor, SpacetimeDB, camera-to-database, speaker, microphone, investigator, screen lock. |
| P | Safe protected-tool and lock demos | The lock is a dry run (a real lock needs your explicit approval). The protected tool gate is live (the README scenario on real NeMo Relay). Approval UI is Sid's. |

## Historical M4 team measurements (built-in webcam, 640x480)

These earlier reported measurements do not validate the new face-disabled default. Face timings describe only the archived optional feature and do not count toward P0.

| | p50 | p95 | n |
|---|---|---|---|
| Person detection, per frame | 10.8–11.3 ms | 12.1–12.2 ms | hundreds of frames |
| Legacy face detection, outside P0 | 2.2–2.4 ms | 2.7–3.3 ms | hundreds |
| Capture to event | 10.3–12.2 ms | 12.2 ms | 3 |
| Arrival to `person_in_zone` | 281 ms | | 1 |
| Event to mode change | under 0.1 ms | | several |
| Demo start (camera already set up) | 71 ms | | 1 |
| Preview stream | about 15 fps to the browser; 30 fps processed | | 6 s |
| Enrollment duration, owner recognition after enrollment, visitor identification | not measured: nobody has enrolled yet | | 0 |
| Event to speech, investigation | measured earlier outside the demo (see above); recorded per demo once run there | | |

The Jetson has not been run. Every number here is from this M4.

## What each team needs to do

- **Voice (`locked-in-sid-bridge`).** Done on their branch: per-mode delivery (am_onyx, speed by mode), evidence-only variants, a reply guard using `tools.py`'s checks, and the Bluetooth tail padding. Still needed: rebase onto `feature/airlock-dhruv`. The `security.mode` payload is add-only, so their consumers keep working.
- **Sid.** Two things to do:
  - Enroll the camera as role `jetson`. The owner runs the command, and the probe then confirms its kinds.
  - Confirm supported person and zone-interaction kinds against `CONTRACTS.md`; face states remain outside P0.

  Two things to confirm:
  - Arm and disarm buttons for the demo on his dashboard.
  - Whether the dashboard should also show LOCKI's mode. Today that is on our local bus; a reducer would be needed to store it.
- **Areeb (edge).** Run the person/zone pipeline on the Jetson with an on-device person detector, and publish the supported kinds as role `jetson`. Verify the merged Mac's subscribed remote-camera path with an actual accepted event; enrollment alone is not that proof.
- **Mustafa (M5).** Keep `nemotron-3.5-bolt:e4b` loaded behind the tunnel on `127.0.0.1:11435`.
- **Mission Control.** For preflight, `GET /api/security/demo` has a `readiness` block of real states. For traces, use the demo timeline and `data/airlock_runs/demo-<id>.json`.
