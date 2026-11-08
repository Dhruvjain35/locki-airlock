# LOCKI Alert (native macOS takeover helper)

A small Swift 6 + AppKit/SwiftUI agent app (no Dock icon, `LSUIElement`) that covers the primary display with a security
alert when the LOCKI backend says a genuine CRITICAL takeover is active. It only renders what
`GET /api/security/takeover` decides. Dismissing never disarms, deletes evidence or changes authorization.
It implements the shared native-takeover contract (backend <-> native helper <-> dashboard).

## Build and install

```bash
native/locki-alert/build.sh          # swift build -c release -> ~/.locki/alert/LOCKI Alert.app (ad-hoc signed), idempotent
~/.locki/alert/locki-alert-selftest  # state-machine self-test, exit 0 = all pass
```

Binary inside the bundle: `~/.locki/alert/LOCKI Alert.app/Contents/MacOS/locki-alert`. SwiftPM scratch lives in
`~/.locki/alert/.build` (never in the repo). Archivo is copied from `frontend/node_modules/@fontsource-variable/archivo`
when present; otherwise SF Pro is used.

## Run

```bash
open "$HOME/.locki/alert/LOCKI Alert.app"                                   # production: polls 127.0.0.1:8077
"$HOME/.locki/alert/LOCKI Alert.app/Contents/MacOS/locki-alert" --once-check  # JSON status, exits
"$HOME/.locki/alert/LOCKI Alert.app/Contents/MacOS/locki-alert" --rehearsal --delay 2 --auto-dismiss 3
```

| Flag | Default | Purpose |
|---|---|---|
| `--backend URL` | `http://127.0.0.1:8077` (env `LOCKI_ALERT_BACKEND`) | loopback only; anything else exits 2 |
| `--token-file PATH` | `$LOCKI_ALERT_TOKEN_FILE` or `~/.locki/run/alert.token` | re-read after every 401 |
| `--state-dir DIR` | `~/.locki/run` | `alert.lock` (flock), `alert.pid`, `alert-acks.json` (last 200 ids, 0600) |
| `--log PATH` | `~/.locki/logs/alert.log` | JSON lines (launch, connection, shown, visible, shown_settled, report, hide, ack_persisted, exit) |
| `--poll-ms N` | 200 | poll interval |
| `--rehearsal` / `--delay N` | off / 0 | local REHEARSAL sample, no backend contact at all, exits when it closes |
| `--auto-dismiss N` / `--auto-action A` | off / dismiss | test aid: after N s do dismiss, explain, evidence or stop |
| `--test-banner TEXT` | off | amber banner with TEXT on every panel this process shows |
| `--level` / `--behavior` | `screensaver` / `canJoinAllSpaces,fullScreenAuxiliary,stationary,ignoresCycle` | window level tests |
| `--snapshot PATH` | off | ImageRenderer self-render per show (`{n}` = show number); not a screen capture |
| `--no-status-item` | item on | "LOCKI" menu bar item: status, Show last alert, Rehearsal alert, Quit |

A second instance exits 0 immediately (`{"event":"already_running",...}`). SIGTERM/SIGINT/SIGHUP exit cleanly and remove the pid file.

## Behavior (state machine in `Sources/LockiAlertCore/StateMachine.swift`)

```
poll 200 ms ──200──> decision ──> armed? ──no──> hide (disarmed)
     │                              │ session changed ──> hide (session_changed)
     │                              │ same id visible ──> update in place (ack set elsewhere -> hide + persist ack)
     │                              └ live && fresh && unacked && not in ack file && not shown this run ──> SHOW
     │                                (live=false only with rehearsal=true, and then always with the REHEARSAL banner)
     └─404/401/403/5xx/down──> hidden: nothing.  showing: keep record + amber "Connection to LOCKI lost at HH:MM:SS"
```

Reports (`POST /api/security/takeover/report`): `shown` (latency_ms = shown wall ms - detected_at_ms, sent when the
window server reports the panel visible), `dismissed`, `handoff` (explain / evidence / stop). Dismiss and handoff ids
are persisted, so a restart or reconnect never replays them. After every hide except a handoff the app re-activates
whatever app was frontmost before the panel showed.

Handoff (macOS 14+ cooperative activation): `NSApp.activate()`, `NSApp.yieldActivation(to: chrome)`, then
`chrome.activate(from: .current, options: [.activateAllWindows])` on the Chrome whose argv has
`--user-data-dir=$HOME/.locki-owner-chrome`. Otherwise it opens `LOCKI_DASHBOARD_URL` from `~/.locki/launcher.env`
(the only key read) in that profile. If neither exists, the panel stays up and says "Open the LOCKI dashboard".

Window: borderless `[.borderless, .nonactivatingPanel]` NSPanel pre-created at launch on `NSScreen.screens[0]`, opaque, `hidesOnDeactivate=false`,
level `.screenSaver` (1000). `isFloatingPanel` is set before the level because setting it resets the level to `.floating`.

## Tests

- `~/.locki/alert/locki-alert-selftest`: 26 state-machine cases (show threshold, rehearsal banner invariant, one show
  per id, ack persisted across restart, reconnect without re-show, disarm / session change / ack-elsewhere close,
  stale never shows, 404 or down while hidden shows nothing, decoding, lock wording).
- `scripts/mock-backend.py`: contract-shaped mock on a loopback port (refuses 8077) that walks takeover, 503, update,
  disarm, unverified incident, ack elsewhere, ack lost, stale, 404 and token rotation in 11 s. Use it with
  `--backend http://127.0.0.1:18077 --token-file <tmp>/alert.token --state-dir <tmp>/state --test-banner "..."`.
- Needs a manual run: above a fullscreen app and on another Space (requires Spaces switching or fullscreen toggling).
  Make an app fullscreen or switch Space during `--rehearsal --delay 6 --auto-dismiss 3`, then read `shown_settled`
  in the log (`on_active_space`, `cg.above_every_other_app_window`).
