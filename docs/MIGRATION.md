# How this repository was made

Created on 2026-11-08 as a fresh repository. **It has no history from `Dhruvjain35/locked-in`.** The files are a snapshot of git objects
(`git archive`, so no working-directory files and no `.git`). The snapshot is the two AIRLOCK lanes merged, then pruned of robot-only files.
Every commit here is new.

## Sources

| Lane | Owner | Source | Commit | State when collected |
|---|---|---|---|---|
| Mac backend, CV, security modes, demo, conversation | Dhruv | `~/locked-in`, branch `feature/airlock-dhruv` | `294c1ff9f3ec` | clean: no modified or untracked source, no stash, no merge in progress |
| Voice (incident-to-voice, Bluetooth tail fix, per-mode delivery, reply guard) | Dhruv | `~/locked-in-sid-bridge`, branch `feature/airlock-sid-bridge` | `8818488` | clean. **Already contained in `294c1ff`** (an ancestor), so no separate import and no older voice code |
| SpacetimeDB module, dashboard, bridges, CI, Jetson adapter | Sid | `github.com/Dhruvjain35/locked-in`, branch `feature/airlock-sid` | `00f3c44ec5ad` | remote branch, newest commit "one-use owner-session ARM delegation" |
| Edge runtime | Areeb | `feature/airlock-edge` (last known `80072cd`) | — | **not collected**: not on this Mac, on GitHub or in `Dhruvjain35/Locki`; Areeb's Mac not reachable from here |
| Mission Control | Areeb | `feature/airlock-mission-control` (`~/locki-airlock-mission-control`) | — | **not collected**: the worktree doesn't exist on this Mac |
| Nemotron investigator service | Mustafa | Mustafa's M5 | — | **not collected**: SSH to the M5 needs a password; only the Ollama tunnel is open |

Other worktrees were checked and add nothing:

- The CV session's two scratch worktrees are an ancestor of `294c1ff` and patch-equivalent commits.
- The `:8077` backend runs older code (`a1f029f`).

The merge base of the two lanes is `6078e56`. The merge had seven conflicts. They and four semantic conflicts are resolved as proposals in
[INTEGRATION_DECISIONS.md](INTEGRATION_DECISIONS.md).

## Left out of the snapshot

| Removed | Why |
|---|---|
| `firmware/` and `backend/tests/test_firmware_patch.py` | Zeus Car robot firmware, archived for AIRLOCK (no flash, no motor commands) |
| `TASKS.md`, `.impeccable.md`, `docs/ARCHITECTURE.md` | LOCKED IN task list, design notes and robot architecture |
| `docs/ROBOT_TRACKING.md`, `docs/HACKUTD_STRATEGY.md`, `docs/MILESTONE4_VERIFICATION.md`, `docs/FIRMWARE_AUDIT.md` | Robot-era documents nothing references |

The robot-era docs that retained code still cites moved to `docs/legacy/`, with their references updated. The backend's robot, focus and
Code Quest modules stay, because the FastAPI runtime, voice and vision still import them. They're off by default (`LOCKEDIN_ROBOT_DRIVER=mock`).

Never imported:

- `.env` files and `data/`, which hold device identities, SpacetimeDB tokens, the camera token, the owner's face template, enrollment and
  demo folders, and recordings
- `backend/models/`
- virtual environments and `node_modules`
- Ollama files and model weights

## Secrets audit (before the first commit)

The audit script checked every file in the snapshot (508 files, 6.9 MB) and printed paths only, never values.

- **Live secrets by value: 0 found.** It checked 7 values on this Mac: the Mac's and the camera's SpacetimeDB tokens, the device salt, the
  Fish Audio key, the CLI and GitHub tokens.
- **Patterns: 0 real.** It looked for private keys, JWTs, GitHub, Tailscale, AWS, Slack and `sk-` keys, assigned secrets and personal emails.
  The only hits are fake values in redaction tests (`backend/tests/test_mission_workspace.py`) and a test that checks `face_embedding` is
  refused.
- **Files: 0 found.** None of: model weights, images, audio, video, database dumps, embeddings, `data/` directories, files over 1 MB.
- **Addresses.**
  - No Tailscale address appears.
  - `192.168.4.1` is the robot's fixed access-point address.
  - The other `192.168.1.x` values are test cases for the private-address allow-list.
  - One docstring example became `<m5-private-address>`.

No credential needs rotating.

## Tests on this snapshot

The tests ran with the M4's existing Python environment and the pinned models.

| Suite | Result |
|---|---|
| Backend, full (`pytest`) | **1507 passed, 7 skipped, 0 failed** |
| SpacetimeDB module (`tsc --noEmit`, `vitest`) | types OK, **111 passed** |
| Dashboard (`vitest`, `vite build`) | **22 passed**, build OK |
| TS bridges and adapters (CI's 7 files) | **99 passed** |
| Sid's Nemotron transport at its new path (`unittest`) | **10 passed** |
| Jetson adapter (`unittest`) | **22 passed** |
| `make -n airlock`, `airlock-cloud`, `airlock-mac` | each expands to its own side's command; `SPACETIME_URL` reaches the backend as the URL |
| `CONTRACTS.md` | both previous texts present whole |

These are software checks. Hardware status is in the README under "Verified so far".
