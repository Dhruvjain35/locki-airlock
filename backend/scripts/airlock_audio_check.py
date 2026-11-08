"""The security warning, out loud, on a real event. A watched app (Calculator, opened hidden) is launched while a temporary local session is armed:

  1. The P0 warning plays on the selected speaker. It is timed from macOS reporting the launch to the audio starting.
  2. A LOCKI reply is speaking on the normal P2 reply path when a second real launch happens. The warning must cut it off and play.

No lock (dry run, and automatic locking is off), no microphone, camera or robot, no model, and nothing is sent anywhere. The reply in step 2 is fixed text,
because a second copy of the language model would not fit in memory next to the running backend; the queue treats it exactly as a model reply.
Close Calculator first. Results go to data/airlock_runs/.

    .venv/bin/python scripts/airlock_audio_check.py
    .venv/bin/python scripts/airlock_audio_check.py --interrupt-only   # only step 2
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
OUT = Path(__file__).resolve().parents[2] / "data" / "airlock_runs"
TEST_APP = "com.apple.calculator"
REPLY = ("I'm keeping an eye on things while you're away. Your workstation is armed, nothing unusual has happened in this session, and I'll tell you the moment a new device "
         "or a watched app shows up. Meanwhile, if you want, I can walk you through what each part of the dashboard means, one panel at a time.")


async def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def launch() -> None:
    subprocess.run(["/usr/bin/open", "-g", "-j", "-n", "-b", TEST_APP], check=True)


def close() -> None:
    subprocess.run(["/usr/bin/pkill", "-x", "Calculator"], check=False)


async def run() -> int:
    from app.config import RuntimeSettings, Settings
    from app.runtime import LockedInRuntime
    from app.security.session import ResponsePolicy
    from app.telemetry.events import EventType
    from app.voice.personality import LIBRARY
    from app.voice.tts import P2_REPLY

    if subprocess.run(["/usr/bin/pgrep", "-x", "Calculator"], capture_output=True, check=False).returncode == 0:
        print("Calculator is open: close it and run this again.")
        return 2
    volume = subprocess.run(["/usr/bin/osascript", "-e", "get volume settings"], capture_output=True, text=True, check=False).stdout.strip()
    print("macOS output:", volume, flush=True)
    if "output muted:true" in volume:
        print("The Mac's output is muted, so nothing would be heard. Unmute it and run this again.")
        return 2
    finished: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="locki-audio-") as tmp:
        settings = Settings(_env_file=None, data_dir=Path(tmp), offline=True, llm_provider="none", robot_driver="mock", mock_autoconnect=False, security_enabled=True, security_lock_mode="dry_run")
        rt = LockedInRuntime(settings, runtime_settings=RuntimeSettings(), persist_settings=False, enable_vision=False, enable_voice=False)
        rt.bus.add_listener(lambda ev: finished.append(dict(ev.payload or {}, _mono=time.monotonic())) if ev.event_type == EventType.VOICE_TTS_FINISHED else None)
        await rt.start()
        results: dict = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "macos_output": volume, "lock_mode": rt.security.lock.mode}
        try:
            warning = LIBRARY["security_warning_app"][0].text
            kokoro = rt.kokoro
            ready = await wait_for(lambda: kokoro is not None and kokoro.state == "ready" and kokoro.clip_for(warning) is not None, 120)
            print(f"local voice ready with the warning pre-rendered: {ready}", flush=True)
            import sounddevice as sd

            device = sd.query_devices(kind="output")
            print("speaking on:", device["name"], flush=True)
            results["output"] = {"name": device["name"], "default_low_latency_s": device.get("default_low_output_latency")}
            # A reply that really makes sound before the warning arrives: rendered ahead, as a recent reply would be.
            await asyncio.to_thread(kokoro.prewarm, [REPLY])
            print("reply pre-rendered:", kokoro.clip_for(REPLY) is not None, flush=True)
            await rt.security.arm("audio-check-code", ResponsePolicy(auto_lock=False))
            await asyncio.sleep(0.5)

            # 1. A real launch, and the warning out loud
            mark = len(finished)
            t0 = time.monotonic()
            first, got = None, True
            if "--interrupt-only" not in sys.argv:
                launch()
                got = await wait_for(lambda: any((f.get("trace") or {}).get("kind") == "security" for f in finished[mark:]), 15)
                close()
                first = next((f for f in finished[mark:] if (f.get("trace") or {}).get("kind") == "security"), None)
            results["alert"] = None if first is None else {k: first.get(k) for k in ("text", "outcome", "source", "source_label", "to_audio_ms", "output_latency_ms", "duration_ms", "detail")}
            if "--interrupt-only" in sys.argv:
                first = {"outcome": "finished", "source": "skipped", "to_audio_ms": None}
            print(f"\n1. Real launch to warning audio: {first and first.get('to_audio_ms')} ms (+ device latency {first and first.get('output_latency_ms')} ms), "
                  f"outcome {first and first.get('outcome')}, source {first and first.get('source_label')}, opened-to-done {round((time.monotonic() - t0) * 1000)} ms", flush=True)
            if not got:
                print("   the warning did not finish within 15 s", flush=True)

            # 2. The warning cuts off a LOCKI reply
            await asyncio.sleep(2.0)
            rt.security._last_warned.clear()  # the same warning within 15 s is normally not repeated; this is a second test, not a repeat
            mark = len(finished)
            rt.speaker.say(REPLY, priority=P2_REPLY, category="generated")
            started = await wait_for(lambda: rt.speaker.speaking and rt.speaker._current_utt is not None and rt.speaker._current_utt.req.text == REPLY, 20)
            await asyncio.sleep(1.5)
            t1 = time.monotonic()
            launch()
            got = await wait_for(lambda: any((f.get("trace") or {}).get("kind") == "security" for f in finished[mark:]), 15)
            close()
            reply = next((f for f in finished[mark:] if f.get("text") == REPLY), None)
            alert = next((f for f in finished[mark:] if (f.get("trace") or {}).get("kind") == "security"), None)
            results["interrupt"] = {"reply_started": started, "reply": None if reply is None else {k: reply.get(k) for k in ("outcome", "source", "duration_ms")},
                                    "alert": None if alert is None else {k: alert.get(k) for k in ("outcome", "source", "to_audio_ms", "output_latency_ms", "duration_ms")},
                                    "alert_finished_after_launch_ms": None if alert is None else round((alert["_mono"] - t1) * 1000)}
            print(f"2. Reply speaking: {started}; reply outcome: {reply and reply.get('outcome')} after {reply and reply.get('duration_ms')} ms; "
                  f"warning outcome {alert and alert.get('outcome')}, real launch to warning audio {alert and alert.get('to_audio_ms')} ms (+ device {alert and alert.get('output_latency_ms')} ms)", flush=True)
            if not got:
                print("   the warning did not finish within 15 s", flush=True)

            audible_reply = bool(reply and reply.get("source") not in ("text_only", "none", None))
            print(f"   the reply really made sound before it was cut off: {audible_reply} (source {reply and reply.get('source')}, cut after {reply and reply.get('duration_ms')} ms)", flush=True)
            results["locks"] = list(rt.security.locks)
            results["ok"] = bool(first and first.get("outcome") == "finished" and first.get("source") not in ("text_only", "none") and audible_reply and reply.get("outcome") == "interrupted"
                                 and alert and alert.get("outcome") == "finished" and alert.get("source") not in ("text_only", "none"))
            print(f"\nlock attempts: {len(rt.security.locks)} (dry run, automatic locking off)", flush=True)
            OUT.mkdir(parents=True, exist_ok=True)
            path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-audio-check.json"
            path.write_text(json.dumps(results, indent=1, default=str))
            print(("PASSED" if results["ok"] else "NOT PASSED") + f": saved {path}")
            return 0 if results["ok"] else 1
        finally:
            close()
            await rt.shutdown()


if __name__ == "__main__":
    unknown = [a for a in sys.argv[1:] if a != "--interrupt-only"]
    if unknown:  # this plays sound, so anything unexpected (even --help) prints usage instead
        print(__doc__)
        print(f"unknown option(s): {' '.join(unknown)}; nothing was played")
        sys.exit(0 if unknown in (["--help"], ["-h"]) else 2)
    sys.exit(asyncio.run(run()))
