# AIRLOCK integration plan

Ownership:

| Area | Owner |
|---|---|
| Computer vision, security modes, demo mode, the Mac's security adapter (`backend/app/security`, `backend/app/vision/airlock`, `frontend/src/demo`) | Dhruv's camera session |
| Voice delivery, dialogue, `runtime.style()` | the voice session (`feature/airlock-sid-bridge`) |
| SpacetimeDB module, authorization, correlation, `frontend/src/airlock` | Sid |
| Jetson deployment | Areeb |
| Nemotron service | Mustafa's M5 |

Nothing below is pushed or merged without Dhruv's approval.

## 1. Voice branch into `feature/airlock-dhruv` (approved on condition of a fast-forward)

- **Ancestry:** `17266a1` (the head of `feature/airlock-dhruv`) is an ancestor of `8818488` (the head of `feature/airlock-sid-bridge`), so a fast-forward is valid. There are 5 commits on top. All are authored by Dhruv Jain, and none has a Co-Authored-By trailer.
  - `90326f2` incident-to-voice
  - `cd3b8de` Bluetooth tail
  - `703d1ff` one voice per mode
  - `1c4faa6` reply guard
  - `8818488` owner-return delivery
- **Verified before the fast-forward:** the full suite runs on `8818488` in a throwaway checkout. Local work is preserved: the working tree is clean, and the fast-forward only adds commits.
- **Method:** `git merge --ff-only 8818488` on `feature/airlock-dhruv`. No merge commit and no push.

## 2. Controlled cutover: scratch backend to the integrated `feature/airlock-dhruv`

Until the cutover, `:8077` runs from the scratch checkout `scratchpad/integration`. That checkout is the voice branch plus the trials commit, the same code as the integrated branch except for test-only and script-only commits.

1. **Check it is safe:** no armed session (`my_sessions` all disarmed, workstation `active_session_id` empty), and no live test in progress.
2. **End the running demo:** camera off, the demo folder deleted, timings saved.
3. **Stop the scratch backend.** This releases the camera and USB-monitor locks, and the SpacetimeDB subscription closes.
4. **Start one backend** from `~/locked-in` (integrated): `make airlock-mac VISION=1`.
5. **Verify:**
   - `GET /api/security/demo` readiness: speaker, USB monitor, SpacetimeDB, models
   - exactly one `uvicorn` process
   - the USB lock holder's pid is that process
6. **Remove** the scratch worktrees.

Rollback is to stop it and restart the scratch checkout. That checkout stays on disk until the integrated backend has run a rehearsal.

## 3. Sid's branch (`origin/feature/airlock-sid`) and `feature/airlock-dhruv`: 7 conflicts, not merged

This comes from a dry run (`git merge --no-commit`, aborted) in a throwaway checkout. The merge base is `6078e56`, and Sid has 15 commits since.

| File | Conflict | Proposed resolution | Decides |
|---|---|---|---|
| `backend/app/intelligence/airlock_agent.py` | **add/add, different modules under one name.** Sid's is a bounded Nemotron HTTP transport called by `scripts/airlock_nemotron.ts` (his TS adapter owns tools and publishing). Ours is the Python investigator: 10 gated tools, NeMo Relay, deterministic report checks. `service.py` imports it. | Rename Sid's to `backend/app/intelligence/nemotron_transport.py` and update the path in `scripts/airlock_nemotron.ts`; keep ours. **Decide which investigator runs in the demo.** Ours has run real Nemotron investigations from this Mac; two investigators on one incident would be a duplicate consumer. | Sid and Dhruv |
| `Makefile` | Both define `airlock`. Sid's starts SpacetimeDB and his dashboard; ours starts the Mac backend with security, camera and voice. | Keep Sid's `airlock`, `airlock-cloud`, `airlock-build` and `airlock-verify`. Rename ours to `airlock-mac` (`VISION=1`, `QUIET=1`, `LOCK=` unchanged). Union `.PHONY`. `setup` and `preflight`: keep ours (`scripts/setup_mac.sh`, which checks the Mac) and add Sid's `uv sync` line if it is missing. | Sid and Dhruv |
| `frontend/src/App.tsx` | Sid's `App` routes to `AirlockApp` (his dashboard) by default, and to `LegacyApp` with `?product=legacy`. Ours adds the demo view via `#demo` and a Demo button. | Keep Sid's router. Our LOCKED IN dashboard is `LegacyApp`, with the demo at `?product=legacy#demo`. Later, a "Demo" link in `AirlockApp` (Sid's file) points there. | Sid |
| `backend/app/security/__init__.py` | add/add, a one-line docstring each | Take ours: the package is the Mac adapter. | Dhruv |
| `docs/airlock/CONTRACTS.md` | Both added sections | Keep both whole: Sid's "Mac views" section, plus our camera mapping and "Security mode and visual state". | Both |
| `README.md`, `firmware/README.md` | Documentation edits on both sides | Merge both texts by hand; no code. The firmware warnings stay intact. | Dhruv |

**Cherry-picking instead of merging.** If the team keeps one integration branch, these of Sid's commits are needed on the Mac side:

- the Mac-views commit `144434e` (already deployed; it only changes his module)
- his module and docs commits

Nothing of ours needs to go into his branch, apart from the rename above if he agrees.

## 4. Requests for Sid

- **Enroll our M4 camera** (owner reducer; the owner runs it). The identity was verified on 2026-11-07: the token in `data/airlock_vision/` authenticates as `c200c3469734...` (`GET /v1/identity/<id>/verify` returned 204). It is separate from the Mac's `c200b00c80d0...`.
  ```
  spacetime call --server maincloud --no-config locki-w91bw enroll_device '"dhruv-m4-air"' '"cam-e2d5d60c513c"' '"c200c34697345ca09abb33ead410578af099974512d4b0bc092971bd64393587"' '"jetson"' '"M4 built-in webcam (dev camera)"'
  ```
  - It publishes only `person_in_zone`, `person_cleared`, `camera_offline` and `camera_online`, and reports `report_health` for component `camera` every 4 s.
  - Metadata is `label` (the zone), `captureMode` (`mac_webcam:mac_mediapipe`, the real capture path) and `detail`.
  - It never publishes identity, images or embeddings.
  - Areeb's `mac-dev-cam-01` stays registered; this is a different camera with a different identity.
- **Voice arming** (proposal in `CONTRACTS.md`, "Voice-initiated arming").

## 5. Measured so far, and what is not

The real-camera numbers are in `VISION.md` and `DEMO.md`. Not yet measured:

- identity on real enrolled faces (this needs consenting volunteers, using the Trials panel)
- a camera-triggered audible warning (the 2-minute test in `DEMO.md`)
- anything on the Jetson
