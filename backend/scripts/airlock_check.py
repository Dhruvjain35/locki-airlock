"""AIRLOCK's end-to-end check on this Mac, using real OS calls and the real runtime. Each line says whether it was REAL or a STAND-IN.

    make airlock-check

What it does: reads the real USB registry, opens Calculator hidden and closes it, runs a real dry-run lock request, renders the warning with the real local
voice without playing it, and runs the real gate on real Relay and pytest. It does not use the microphone, camera or speaker, does not lock the screen, sends nothing
off this Mac, and downloads nothing. A physical USB insertion, audible speech, a real lock and Nemotron itself need a person or another Mac: they are listed as NOT RUN.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS: list[tuple[str, str, str]] = []
TEST_APP = "com.apple.calculator"


def report(status: str, label: str, detail: str = "") -> None:
    RESULTS.append((status, label, detail))
    print(f"[{status:7}] {label}" + (f": {detail}" if detail else ""), flush=True)


async def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def main() -> int:
    from app.config import RuntimeSettings, Settings
    from app.missions.relay import installed as relay_installed
    from app.runtime import LockedInRuntime
    from app.security.contracts import SecurityEventV1
    from app.security.mac_lock import screen_lock_setting
    from app.security.mac_usb import iokit_snapshot, ioreg_snapshot
    from app.security.session import ResponsePolicy
    from app.voice.tts import NullTTS

    if subprocess.run(["/usr/bin/pgrep", "-x", "Calculator"], capture_output=True, check=False).returncode == 0:
        print("Calculator is open, and this check opens and closes it: close it and run this again.")
        return 2

    salt = b"check"
    a, b = sorted(d.fingerprint(salt) for d in iokit_snapshot()), sorted(d.fingerprint(salt) for d in ioreg_snapshot())
    report("REAL" if a == b else "FAIL", "USB registry read two ways agrees", f"{len(a)} devices")

    with tempfile.TemporaryDirectory(prefix="locki-airlock-") as tmp:
        settings = Settings(_env_file=None, data_dir=Path(tmp), offline=True, llm_provider="none", robot_driver="mock", mock_autoconnect=True, security_enabled=True)
        rt = LockedInRuntime(settings, runtime_settings=RuntimeSettings(), persist_settings=False, tts_engine=NullTTS(), enable_vision=False, enable_voice=False)
        await rt.start()
        try:
            security = rt.security
            monitor = security.monitor
            report("REAL" if monitor.usb_watcher.method == "iokit_notification" else "WARN", "USB watcher", monitor.usb_watcher.method)
            report("REAL" if monitor.app_watcher.method == "nsworkspace_notification" else "WARN", "App watcher", monitor.app_watcher.method)
            report("REAL", "Lock mode", f"{security.lock.mode}. macOS says password after sleep is {screen_lock_setting() or 'unknown'}")
            if security.lock.mode != "dry_run":
                report("FAIL", "This check only runs in dry-run mode")
                return 1

            await security.arm("airlock-check-code", ResponsePolicy(auto_lock=True, auto_lock_on_watchlist_app=True))
            report("REAL" if security.session else "FAIL", "Armed a local session", f"baseline {len(security.session.baseline)} devices, source {security.session.source}")

            t0 = time.monotonic()
            subprocess.run(["/usr/bin/open", "-g", "-j", "-b", TEST_APP], check=True)
            seen = await wait_for(lambda: security.incident is not None, 10)
            event = next((e for e in security.evidence if e.metadata.get("bundle_id") == TEST_APP), None)
            if not seen or event is None:
                report("FAIL", "A real app launch raised an incident")
                return 1
            SecurityEventV1.model_validate(event.model_dump())
            report("REAL", "App launch became a valid SecurityEventV1", f"seen {round((time.monotonic() - t0) * 1000)} ms after opening it, seq {event.seq}, signature {event.metadata['signature']['status']}")
            subprocess.run(["/bin/kill", str(int(event.metadata["pid"]))], check=False)

            await wait_for(lambda: bool(security.warnings) and security.warnings[-1].get("audio") is not None, 5)
            warning = security.warnings[-1] if security.warnings else None
            report("REAL" if warning and warning["spoken"] else "FAIL", "Warning queued with no model and no network", None if warning is None else f"{warning['queued_ms']} ms after the OS report. Speech engine here is silent, so nothing played")

            await wait_for(lambda: any(x["status"] == "LOCK_DRY_RUN" for x in security.locks), 5)
            statuses = [x["status"] for x in security.locks]
            report("REAL" if "LOCK_DRY_RUN" in statuses and "LOCK_CONFIRMED" not in statuses else "FAIL", "Lock request ran every check, as a dry run", " then ".join(statuses))
            locked = security.lock.is_locked()
            report("REAL" if locked is False else "FAIL", "The OS says the screen is not locked", f"locked={locked}")

            r = await rt.handle_utterance("Disarm", source="text", stt_confidence=1.0)
            report("REAL" if security.session is not None and not r.ok else "FAIL", "A spoken disarm changes nothing", r.reply)
            r = await rt.handle_utterance("Stop talking", source="text", stt_confidence=1.0)
            report("REAL" if rt.speaker.muted and monitor.running and security.session is not None else "FAIL", "Stop talking silences speech and monitoring carries on")
            rt.set_muted(False)

            r = await rt.handle_utterance("What happened?", source="text", stt_confidence=1.0)
            report("REAL" if r.reply and security.investigations and security.investigations[-1]["status"] == "pending_model" else "WARN", "With no model, what happened comes from the record", (r.reply or "")[:140])

            motion = [e for e in rt.bus._history if "motion" in e.event_type.value and e.event_type.value.startswith(("robot.", "safety."))]
            report("REAL" if not motion and not rt.transport.is_moving else "FAIL", "No motion of any kind", f"{len(motion)} motion events; simulated robot only")

            await security.disarm("airlock-check-code")
            report("REAL" if security.session is None else "FAIL", "The owner's code disarms")
            security.publisher.flush()
            snap = security.publisher.snapshot()
            report("REAL" if snap["last_delivery"]["status"] == "local_only" else "WARN", "Events stayed on this Mac", f"{snap['published']} published to transport {snap['transport']}: {snap['last_delivery']['status']}")
        finally:
            subprocess.run(["/usr/bin/pkill", "-x", "Calculator"], check=False)
            await rt.shutdown()

    try:
        from app.voice.kokoro_tts import KokoroTTS
        from app.voice.personality import LIBRARY

        settings = Settings(_env_file=None)
        with tempfile.TemporaryDirectory(prefix="locki-voice-") as tmp:
            voice = KokoroTTS(settings.models_dir, Path(tmp))
            await asyncio.to_thread(voice.load)
            line = LIBRARY["security_warning_usb"][0].text
            t0 = time.monotonic()
            voice.prewarm([line])
            clip = voice.clip_for(line)
            report("REAL" if clip is not None else "WARN", "The real local voice renders the warning, not played", f"{round((time.monotonic() - t0) * 1000)} ms on first render. A rendered clip then starts without this delay")
    except Exception as exc:
        report("WARN", "Local voice render", f"{type(exc).__name__}: {exc}"[:160])

    if relay_installed():
        script = Path(__file__).with_name("airlock_relay_demo.py")
        out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120, check=False)
        report("REAL" if out.returncode == 0 else "FAIL", "Malicious README on real NeMo Relay and real pytest", next((line for line in out.stdout.splitlines() if line.startswith("Relay's own events")), "no result")[:110])
    else:
        report("WARN", "NeMo Relay is not installed", "scripts/setup_mac.sh --relay")

    for item in ("a physical USB insertion (make airlock-watch, with your OK)", "audible speech and real microphone input", "a real screen lock (needs your OK and LOCKEDIN_SECURITY_LOCK_MODE=real)", "Nemotron Bolt itself (M5 Pro: scripts/airlock_investigate.py --require-nemotron)", "SpacetimeDB and the Jetson (Sid's and Areeb's parts)"):
        report("NOT RUN", item)

    failed = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\n{sum(r[0] == 'REAL' for r in RESULTS)} real checks passed, {len(failed)} failed, {sum(r[0] == 'WARN' for r in RESULTS)} warnings, {sum(r[0] == 'NOT RUN' for r in RESULTS)} not run.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
