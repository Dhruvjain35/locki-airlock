# LOCKI AIRLOCK judging runbook (target 2:40)

This covers the timed run. Power-on, launch, READY and shutdown are in [`scripts/airlock_launcher/RUNBOOK.md`](../../scripts/airlock_launcher/RUNBOOK.md).

Everything on screen is the real dashboard on real data. The only rehearsal path is the **Synthetic rehearsal** section under Advanced controls, which labels every row as synthetic and never appears in a live session.

## Before judges arrive (not timed)

1. Launch with `scripts/airlock_launcher/locki_launch.sh` or the **LOCKI AIRLOCK** app, and wait for READY. It reuses healthy services and starts only what's missing.
   - Manual fallback on the M4:
     - backend: `cd <backend checkout> && LOCKEDIN_SPACETIME_REMOTE_CAMERAS=jetson-01 LOCKEDIN_LOCAL_LLM_BACKEND=ollama LOCKEDIN_OLLAMA_URL=http://127.0.0.1:11435 LOCKEDIN_AIRLOCK_OLLAMA_URL=http://127.0.0.1:11435 make airlock-mac`
     - dashboard: `cd <frontend checkout>/frontend && VITE_SPACETIMEDB_URI=https://maincloud.spacetimedb.com VITE_SPACETIMEDB_DATABASE=locki-w91bw npm run dev`
   - Keep `LOCK` and `VISION` unset, so screen lock stays dry run and the camera stays off.
2. Open the owner dashboard from the **LOCKI AIRLOCK** app, or from the saved owner address. That address carries `client=owner-recovered-…`; a bare `127.0.0.1:5174` opens the wrong browser slot and shows Unenrolled. Then pick **owner menu → Presentation mode**, or append `&present=1` to that address. Signed in as **Owner**, it shows **PROTECTION OFF**, three green indicators (Mac, Jetson, SpacetimeDB) and the strip Protect → Detect → Contain → Explain → Recover.
3. Speaker: the JBL is the macOS default output and is **not muted**. The dashboard shows a red "Voice output is muted" line otherwise.
4. If an amber "investigator still has read access" line shows, click **Revoke access** so the run starts clean.
5. Have one harmless USB stick ready that was **unplugged** when protection starts.

## The run

| Time | Who | Action | What the judge sees |
|---|---|---|---|
| 0:00–0:20 | Presenter | "LOCKI watches your desk while you're away, using real sensors and NVIDIA Nemotron to explain what happened." Click **START PROTECTION**, glance at the baseline, click **Start protection**. | PROTECTED, a live session timer, NORMAL, and the flow at **Protect**. |
| 0:20–0:45 | Volunteer | Walks up to the desk and stays in the Jetson's view. | Within about a second: WATCHFUL (amber), the Jetson tile pulses, and LOCKI says "person at the desk". Flow at **Detect**. |
| 0:45–1:10 | Volunteer | Plugs in the USB stick within 10 s of being seen. | The red **CRITICAL SECURITY EVENT** view: "Unknown USB device connected while a person was detected at the workstation", the evidence timeline, the priority warning, screen lock "Not locked (dry run)" and protected tools as reported. Flow at **Contain**. |
| 1:10–2:05 | Presenter | Click **EXPLAIN INCIDENT**, then **Approve and explain**. | Investigating real evidence (about 15 s live), then Nemotron's grounded findings with evidence IDs, LIVE badge and inference time. LOCKI speaks the answer. Flow at **Explain**. |
| 2:05–2:35 | Presenter | Click **STOP PROTECTION**, then **Stop protection**. | "Workstation disarmed" and "Nemotron access revoked". The incident stays in **History**. Flow at **Recover**. |
| 2:35–2:40 | Presenter | "Local physical sensing, evidence-grounded AI, and the owner stays in control." | Calm PROTECTION OFF, ready for the next run. |

**Honesty rules for the presenter:**
- WATCHFUL means someone is near, never an intruder.
- DEFENSIVE needs a real interaction seen by the local camera. With the M4 camera off it won't appear, so don't claim it.
- The red view is a visual response. It is not an OS lock.

**If the USB comes more than 10 s after the person was seen,** SpacetimeDB records a USB-only incident. The view then says only "Unknown USB device connected…" and never mentions a person.

## Failure paths, all honest

- **Nemotron slow or unreachable.** The panel shows the real reason and offers Retry. The red view, evidence and Stop all keep working.
- **Jetson offline.** Its indicator goes amber or red. USB protection still works. The dashboard never shows an intrusion it didn't see.
- **Reload during the demo.** A dismissed red view stays dismissed, and old incidents never reopen it. Over 10 minutes old, or from another session: no takeover.
- **Backup voice input.** Investigation → **Prerecorded voice · live AI response** → add `locki-what-happened.wav` → **Send to LOCKI**. This uses real speech-to-text and a fresh answer. Recordings can never arm or disarm.

## Measured on real hardware (M4, session 9184dd15, 2026-11-08)

- Jetson camera → SpacetimeDB: 306–325 ms. SpacetimeDB → M4: 69 ms.
- Person warning, from detection to start of audio: 0.49–0.71 s (local Kokoro voice).
- Correlated USB incident `incident:193f2c67…`: high severity, both warnings played.
- Nemotron live investigation `inv_1581…`: 15.3–15.7 s, LIVE evidence, 4 grounded findings.

These figures are not yet measured on Areeb's Mac. Freeze the judging SHA only after a physical run there passes.
