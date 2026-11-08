# Native alert window (LOCKI Alert.app)

When LOCKI decides a genuine critical event happened while protection is armed, a native macOS window covers the main display above
whatever app is in front: VS Code, an unrelated Chrome tab, Finder. Nobody has to open the dashboard first. It is a visual security
response, not an OS lock: it never locks the screen, logs out, kills processes or blocks the network.

## How it works

```
Jetson person ─┐                         ┌──────────────────────────────┐
Mac USB watch ─┼─> SecurityService ─────>│ ModeEngine (unchanged)        │  the only local security authority
SpacetimeDB ───┘        │                └──────────────────────────────┘
                        └─> takeover.py   read-only: should the takeover show, for which evidence, with which outcomes
                                 │
   GET  /api/security/takeover  <── every 200 ms, X-Locki-Alert-Token ── LOCKI Alert.app (Swift, AppKit, no Dock icon)
   POST /api/security/takeover/report  <── shown / dismissed / hand-off ──┘
   GET  /api/security (gains "takeover")  <── owner dashboard, every second, and at once when it comes to the front
```

- **One authority.** The backend's ModeEngine and SpacetimeDB decide. `backend/app/security/takeover.py` only reads what the
  SecurityService already holds and says whether the takeover applies. The window renders that decision and nothing else.
- **Local authentication.** The backend writes a fresh random token to `~/.locki/run/alert.token` (0600, folder 0700) at every
  start. Both takeover routes need it, and refuse any request that carries an `Origin` header, so no web page can read them,
  open an alert or dismiss one. The usual loopback-only guard still runs first. The alert window never listens on a port.
- **No credentials in the window.** It holds no owner identity and no investigator key. Explain, View evidence and Stop protection
  hand off to the owner's authenticated dashboard, which runs the same owner-confirmed flows as its own buttons.
- **Dismissing hides the view only.** It is recorded as an acknowledgement for that takeover id (in the backend and in
  `~/.locki/run/alert-acks.json`). It never disarms, deletes evidence or changes a grant.

## When it shows

| Situation | Takeover |
|---|---|
| Armed, nobody near | No |
| WATCHFUL: a person at the desk (Jetson) | No. Amber on the dashboard and the existing spoken line |
| DEFENSIVE: interaction with protected equipment | No. Orange on the dashboard and the existing spoken line |
| CRITICAL: this Mac's own watcher reported an unknown USB device, this session, under 2 minutes old | **Yes** |
| A correlated SpacetimeDB incident (person + device) for this session, severity high or critical, under 2 minutes old | **Yes**. If the local mode is not CRITICAL, it says "Server-confirmed incident · local CRITICAL not verified" |
| A rehearsal fixture | Only with the amber **REHEARSAL** banner, never as a live event |
| Another session's or workstation's event, a stale replay after reconnecting, a disarmed session, a camera outage, an unknown app | No |

One takeover per id. The id stays the same when the local USB fact is later confirmed by SpacetimeDB's incident (the incident's
`digital_event_id` is the Mac's own USB event id), so the window never appears twice for one device.

## What it shows

**LOCKI AIRLOCK · CRITICAL SECURITY EVENT · Unauthorized device activity detected**, then one sentence built only from the
record: SpacetimeDB's own summary when the incident exists, otherwise the measured gap between the Jetson's person report and the
USB connection as this Mac received them, otherwise "An unknown USB device was connected while protection was armed."

- **Evidence detected:** person at desk (if any), the USB device and when, the incident and its server severity, workstation and
  session, sensor health and where each fact came from.
- **Security response:** warning audio (played / failed / muted / not repeated / pending / unknown, from the voice's own
  outcome), LOCKI tools (frozen / not frozen / not configured), screen lock (not locked / dry run / requested / confirmed, from the
  lock adapter), monitoring (running / degraded / stopped), NVIDIA Nemotron (investigating / answered / failed / available /
  unavailable), incident evidence (recorded / local record only / unavailable). Nothing says contained, isolated, blocked or
  locked unless the lock adapter confirmed a real lock.
- **Buttons:** EXPLAIN INCIDENT, View evidence, Dismiss alert view, Stop protection.

## Launching, stopping, restoring

| Do | Command |
|---|---|
| Build (once, or after a change) | `bash native/locki-alert/build.sh` (installs `~/.locki/alert/LOCKI Alert.app`) |
| Start everything | double-click LOCKI AIRLOCK.app, or `~/.locki/launcher/locki_launch.sh` (starts or reuses the alert window) |
| Check | `~/.locki/launcher/locki_launch.sh --check` (where the alert window is installed, READY needs it polling; a Mac without it shows a warning and stays usable) |
| Start only the alert window | `open -g -j ~/.locki/alert/LOCKI\ Alert.app` (a second copy exits on its own) |
| Quit the alert window | `kill "$(cat ~/.locki/run/alert.pid)"` (protection is unaffected) |
| Stop LOCKI (disarmed only) | `~/.locki/launcher/locki_launch.sh --stop` |

## Limits

- It runs in the logged-in user's session. It cannot appear on the login window or the lock screen, and macOS does not allow it to.
- It does not stop a device or a program that has already acted, and it does not block system shortcuts.
- Measured behavior across apps, Spaces and fullscreen apps is in the test record below; where macOS refused, it says so.

## Test record (2026-11-08, Areeb's MacBook Pro, macOS 26.5)

Automated:

| Suite | Result |
|---|---|
| `backend/tests/test_security_takeover.py` (threshold, person-only, unknown USB, correlated HIGH, session/workstation scoping, fixtures, stale replays, one id per incident, disarm, dismiss never disarms, honest response states, token/Origin/404) | 27 passed |
| Existing security, API and local-only suites with the change | 451 passed |
| `~/.locki/alert/locki-alert-selftest` (show threshold, rehearsal banner, one show per id, acks across restart, reconnect, disarm, stale, 404/down) | 26 passed |
| Frontend (`npx vitest run`, includes `takeover.test.ts`) | 173 passed; `tsc` clean; production build clean |

On the real display (rehearsal or test-banner content, so nothing looked like a real event):

| Case | Front app before | Panel above every other window (CGWindowList) | Show → visible |
|---|---|---|---|
| Unrelated Chrome window | Chrome | yes, level 1000 | 83 ms |
| VS Code | VS Code | yes | 76 ms |
| Finder | Finder | yes | 102 ms |
| Hand-off (Explain) | Finder | yes; the owner Chrome was frontmost 0.4 s later | 107 ms |
| End to end: real backend code, simulated USB watcher and camera row, real alert window (`backend/tests/native_alert_harness.py`) | (desktop) | yes | **283 ms** from the backend's detection to the window (includes the ≤200 ms poll) |

The end-to-end run also showed: nothing for armed-and-quiet or for a person alone (WATCHFUL); the dismiss request left the session,
mode, evidence and incident untouched and protection armed; restarting the alert window did not replay the alert; disarming cleared
it. Idle cost: 0.7 % CPU averaged over 30 s, 46–59 MB resident.

Not yet verified (each needs a person at the Mac):
- A macOS fullscreen app, another Space, an external display. Run the manual check below.
- The hand-off from a real click (the run above used a timer); Chrome activation from a click should be at least as reliable.
- The live chain: real USB stick, real Jetson person, real SpacetimeDB incident, audible warning on the JBL, real Nemotron answer.
  Measure it in the full rehearsal (runbook timing table).
- The login window and lock screen: not supported by macOS for a user-session app, so not claimed.

Manual check for Spaces and fullscreen apps (a labelled REHEARSAL panel, its own state folder, so it never meets the real one):

```bash
"$HOME/.locki/alert/LOCKI Alert.app/Contents/MacOS/locki-alert" --rehearsal --delay 8 --auto-dismiss 4 \
  --state-dir /tmp/locki-alert-manual --log /tmp/locki-alert-manual.log
# within 8 s: put an app in fullscreen (green button), or swipe to another Space. Then:
grep shown_settled /tmp/locki-alert-manual.log | tail -1   # on_active_space and cg.above_every_other_app_window must be true
```
