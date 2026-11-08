# LOCKI AIRLOCK — judge-day runbook

## Before judging (power and network)

1. The **Jetson** is powered on and on the network. Its camera publisher starts at boot (Areeb).
2. **Mustafa's M5** is awake, on Tailscale, and running the investigator and Ollama (Mustafa). Turn sleep off.
3. The **JBL Go 3** is on and connected to the M4.
4. The **M4** is on the network you tested. If the venue Wi-Fi fails, use the pretested hotspot.
   - Internet is required. SpacetimeDB (maincloud) carries the Jetson's events and the owner console.
   - The M4→M5 link runs over Tailscale, through its Dallas relay on networks that block peer-to-peer.
   - Without Internet, only USB/app monitoring and LOCKI's voice keep working.
5. The M4's volume is up and not muted. The launcher refuses READY while it's muted.

## The one action

Double-click **LOCKI AIRLOCK** on the M4 Desktop (`~/Desktop/LOCKI AIRLOCK.app`).

It:
1. reuses anything already running, or starts the backend and dashboard if they're missing;
2. starts the native alert window (LOCKI Alert.app) if it isn't running, without taking focus;
3. makes sure the M5 tunnel agent is running;
4. checks every component for real;
5. opens your owner dashboard in the owner Chrome window.

It never arms, never turns the camera or microphone on, never grants evidence, and never enables a real screen lock.

## Startup time (measured)

| Situation | Launch → verdict |
|---|---|
| Services already running (the normal judge-day case) | **0.9 s**, 3 runs |
| A dropped M5 tunnel | Restored by the agent in **4–12 s** (tested) |
| Backend not running (cold start) | Not yet measured. It's measured at the next approved restart while disarmed. |

## What READY looks like

A notification: **"LOCKI AIRLOCK — READY. Every required check passed in N s."** The owner dashboard opens. Every one of these has passed:

- M4 backend
- Dashboard
- SpacetimeDB connected and authorized
- USB and app watchers
- Screen lock is dry run
- Jetson camera (jetson-01) online, with a heartbeat under 30 s old
- Nemotron investigator (M5) answering with the key, live evidence reachable, model installed
- Nemotron model actually answers
- Mac volume up and not muted
- Native alert window polling the backend (it covers the main display on a critical event even when the dashboard is hidden). Required once installed; a Mac that never ran install.sh, such as a fallback, gets a warning instead
- Speaker. This one is a note, not a blocker: it says "LOCKI speaks on: JBL Go 3", or warns that it's on the Mac speakers.

Then press **START PROTECTION** in the dashboard and approve the baseline.

## If something isn't ready

The launcher shows a dialog with the exact problem and a **Retry** button.

| Message | What it means | Do this |
|---|---|---|
| Jetson camera offline / old heartbeat | No person detection | Check the Jetson's power and network, wait about 20 s, Retry |
| Nemotron investigator / model not answering | Detection still works; Explain doesn't | Wake the M5 and check it's on Tailscale. The tunnel heals itself within about 15 s. Retry |
| SpacetimeDB not connected | Not protected; the incident record is unavailable | Fix the Internet (switch to the hotspot), Retry |
| Mac output volume muted | Nobody would hear the warnings | Unmute and raise the volume, Retry |
| Speaker: Mac speakers (note) | The JBL isn't connected | Turn the JBL on; macOS switches to it automatically |
| M4 backend / dashboard not answering | Still starting, or crashed | Retry once. If it persists, see "Backend did not start" |
| The dashboard says Unenrolled | The owner browser lost its login | Use **Recover existing owner access** in the dashboard |
| Native alert window not polling | A critical event would only show in the dashboard tab (degraded) | Retry (the launcher starts it). Missing app: `bash native/locki-alert/build.sh` |
| Backend has no native alert support | The running backend is older than this feature | While disarmed: `locki_launch.sh --stop`, then launch again from the updated checkout |

**Backend did not start:** see `~/.locki/logs/backend.log`. Relaunching is safe; the launcher never starts a second copy.

## The judged demo (target 2:35–2:45, hard stop 2:59)

| Clock | Who | Do | The judge sees |
|---|---|---|---|
| 0:00–0:20 | Areeb | One-line intro. Press **START PROTECTION**, approve the baseline | Protection armed; sidebar dots green, including Alert window |
| 0:20–0:40 | Areeb | Switch away: VS Code, or an unrelated Chrome tab | The dashboard is no longer visible |
| 0:40–1:05 | Volunteer | Walk to the desk (the Jetson reports a person), then plug the harmless USB stick within 10 s | The native LOCKI takeover covers the display over VS Code; the warning plays |
| 1:05–1:55 | Areeb | Press **EXPLAIN INCIDENT** on the takeover. The dashboard comes forward with this incident's consent dialog: approve. Nemotron answers and LOCKI speaks it | The evidence and the real Nemotron result in the Investigation view |
| 1:55–2:25 | Areeb | **Stop protection** and confirm | Disarmed; "investigator access revoked for N"; the incident in History; no takeover |
| 2:25–2:40 | Areeb | Closing sentence | Protection off, ready for the next run |

Rules for the run:
- Only a genuine event opens the takeover. A person alone never does. If it hasn't appeared 2 s after the USB stick goes in, say so,
  and point at the dashboard's red card and tab title, the labelled browser fallback.
- If Nemotron fails, the Investigation view says so and the evidence stays. Never claim an answer that wasn't given.
- The prerecorded "LOCKI, what just happened?" may replace the live microphone. Say it's prerecorded; the reply stays genuine.

Measured timings (fill in on every full rehearsal with the real Jetson, USB stick and M5; the demo only "fits" after a real run):

| Run | USB in → takeover visible | USB in → warning audible | Explain → answer spoken | Total |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |

## After judging (safe shutdown)

1. In the dashboard, press **STOP PROTECTION** and confirm the evidence grant is revoked.
2. Optional: stop what the launcher started, including the alert window, with `~/.locki/launcher/locki_launch.sh --stop`. It refuses while protection is armed.
3. Optional, to stop the M5 tunnel: `launchctl bootout gui/$(id -u)/com.locki.airlock.m5-tunnel`.
4. If you used Sid's browser recovery, delete the owner Chrome data folder (`~/.locki-owner-chrome`), or clear its `airlock.token.v1:*` entries.

## One-time setup (already done, or done once before judging)

1. `bash ~/locki-airlock-release/scripts/airlock_launcher/install.sh`. It installs the files, loads the M5 tunnel agent and creates the Desktop app.
2. On the first double-click, paste your owner dashboard address once (from the owner Chrome window's address bar). It contains no password.
3. Keep the investigator key in `~/.locki/investigator.key` (chmod 600). The SSH key stays in `~/.ssh`. Neither is ever in the repo.

Logs are in `~/.locki/logs/`: launcher.log, launch-times.csv, tunnel.log, backend.log and frontend.log.
