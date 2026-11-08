"""LOCKI's voice in each security mode, out loud, through the real runtime: one voice (Settings.security_voice), the delivery changing by mode.

Speaks the security service's own categories in WATCHFUL, DEFENSIVE, CRITICAL and OWNER_RETURNED, then a NORMAL line, and records for each the
voice that really played, its speed, and the time from asking to audio. No watcher, camera, microphone, model, lock or network: the mode is
set directly, so this checks how LOCKI sounds, not when it decides to speak (that is the security service's, tested in its own suite).

    .venv/bin/python scripts/airlock_voice_modes_check.py --models-dir ../../locked-in/backend/models
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
OUT = Path(__file__).resolve().parents[2] / "data" / "airlock_runs"
STEPS = [("WATCHFUL", "security_warning_presence"), ("DEFENSIVE", "security_warning_interaction"), ("CRITICAL", "security_warning_usb"),
         ("CRITICAL", "security_warning_usb"), ("OWNER_RETURNED", "security_owner_seen"), ("NORMAL", "security_disarmed")]


async def run(args) -> int:
    from app.config import RuntimeSettings, Settings
    from app.runtime import LockedInRuntime
    from app.security.modes import SecurityMode
    from app.telemetry.events import EventType

    volume = subprocess.run(["/usr/bin/osascript", "-e", "get volume settings"], capture_output=True, text=True, check=False).stdout.strip()
    print("macOS output:", volume, flush=True)
    if "output muted:true" in volume:
        print("Muted: unmute and run again.")
        return 2
    finished: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="locki-voice-modes-") as tmp:
        settings = Settings(_env_file=None, data_dir=Path(tmp), models_dir=Path(args.models_dir).resolve(), offline=True, llm_provider="none", robot_driver="mock",
                            mock_autoconnect=False, security_enabled=False, security_lock_mode="dry_run")
        rt = LockedInRuntime(settings, runtime_settings=RuntimeSettings(), persist_settings=False, enable_vision=False, enable_voice=False)
        rt.bus.add_listener(lambda ev: finished.append(dict(ev.payload or {})) if ev.event_type == EventType.VOICE_TTS_FINISHED else None)
        await rt.start()
        try:
            end = time.monotonic() + 120
            while time.monotonic() < end and not (rt.kokoro is not None and rt.kokoro.state == "ready"):
                await asyncio.sleep(0.1)
            rt.security.enabled = True  # security on, as make airlock-mac: one voice from here (no watchers are started)
            rt._sync_local_voice()
            if rt._kokoro_task is not None:
                rt.kokoro.prewarm_cancelled = True  # the full background prewarm: only the urgent lines are needed here
                await asyncio.gather(rt._kokoro_task, return_exceptions=True)
            urgent = [x for x in rt._kokoro_lines() if isinstance(x, tuple)]  # start-up renders these first, at their own speed
            await asyncio.to_thread(rt.kokoro.prewarm, urgent)
            print(f"pre-rendered {len(urgent)} urgent lines at their mode's speed", flush=True)
            import sounddevice as sd

            print(f"voice: {rt.kokoro.voice}; speaking on: {sd.query_devices(kind='output')['name']}\n", flush=True)
            rows = []
            for mode, category in STEPS:
                rt.security.modes.state.current_security_mode = SecurityMode(mode)
                mark = len(finished)
                t0 = time.monotonic()
                text = rt._say(category, force=category.startswith("security_warning"), trace={"kind": "voice_check", "id": f"{mode}-{len(rows)}", "t0": t0})
                for _ in range(400):
                    if len(finished) > mark:
                        break
                    await asyncio.sleep(0.05)
                f = finished[mark] if len(finished) > mark else {}
                row = {"mode": mode, "category": category, "text": text, "speed": rt._delivery_speed(category), "voice": rt.kokoro.voice, "source": f.get("source"),
                       "outcome": f.get("outcome"), "to_audio_ms": f.get("to_audio_ms"), "output_latency_ms": f.get("output_latency_ms"), "tone": rt.style().tone}
                rows.append(row)
                print(f"{mode:15} speed {row['speed'] or 'own':>5}  {row['source']}  to audio {row['to_audio_ms']} ms  | {text}", flush=True)
                await asyncio.sleep(0.6)
            ok = all(r["outcome"] == "finished" and r["source"] in ("kokoro_cache", "kokoro_live") for r in rows) and len({r["voice"] for r in rows}) == 1 \
                and all(r["source"] == "kokoro_cache" for r in rows if r["category"].startswith("security_warning"))
            OUT.mkdir(parents=True, exist_ok=True)
            path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-voice-modes.json"
            path.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "macos_output": volume, "rows": rows, "ok": ok}, indent=1))
            print(("\nPASSED" if ok else "\nNOT PASSED") + f": one voice, warnings pre-rendered at their speed. Saved {path}")
            return 0 if ok else 1
        finally:
            await rt.shutdown()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--models-dir", default=str(Path(__file__).resolve().parents[1] / "models"))
    sys.exit(asyncio.run(run(p.parse_args())))
