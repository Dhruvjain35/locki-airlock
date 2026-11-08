"""End-to-end check of the native takeover on this Mac, with SIMULATED sensors and the real alert window.

The real backend code (SecurityService, ModeEngine, the takeover decision and its routes) serves 127.0.0.1:8097 from a temporary
data folder, with the test suite's stand-ins: a fake USB watcher, a scripted remote-camera row, a recording voice (nothing is played),
no SpacetimeDB and a dry-run lock. The real LOCKI Alert.app binary is pointed at it with its own token, state folder and log, so it
never meets the production instance. Nothing here touches the live backend on :8077, the real USB watcher, the speaker or SpacetimeDB.

    cd backend && ~/locki-airlock/backend/.venv/bin/python -m tests.native_alert_harness [--app "~/.locki/alert/LOCKI Alert.app"] [--shots DIR]

Prints one JSON report: what showed and when, the detection-to-window latency, and what dismissing, restarting and disarming did.
Not collected by pytest (no test_ prefix): it opens a real window on this Mac's display for a few seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

PORT = 8097
BASE = f"http://127.0.0.1:{PORT}"


def _env(tmp: Path) -> None:
    os.environ.update({
        "LOCKI_ALERT_TOKEN_FILE": str(tmp / "run" / "alert.token"),
        "LOCKEDIN_LLM_PROVIDER": "none", "LOCKEDIN_AIRLOCK_OLLAMA_URL": "http://127.0.0.1:9", "LOCKEDIN_OLLAMA_URL": "http://127.0.0.1:9",
        "LOCKI_INVESTIGATOR_URL": "http://127.0.0.1:9", "LOCKI_INVESTIGATOR_KEY_FILE": str(tmp / "no-investigator.key"),
        "FISH_AUDIO_API_KEY": "", "LOCKEDIN_FISH_AUDIO_API_KEY": "", "FISH_API_KEY": "", "LOCKEDIN_FISH_AUDIO_BASE_URL": "http://127.0.0.1:9",
    })


def _binary(app: Path) -> Path:
    exe = [p for p in (app / "Contents" / "MacOS").iterdir() if os.access(p, os.X_OK)]
    if not exe:
        raise SystemExit(f"no executable inside {app}")
    return exe[0]


def _log_lines(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


async def run(tmp: Path, app_path: Path, shots: Path | None) -> dict:
    import httpx
    import uvicorn

    from app.config import RuntimeSettings, Settings
    from app.main import create_app
    from app.runtime import LockedInRuntime
    from app.security.contracts import SecurityEventV1, SecurityKind, SecuritySource

    from .conftest import RecordingTTS
    from .test_security_monitor import STICK
    from .test_security_service import CODE, Harness

    report: dict = {"sensors": "SIMULATED (fake USB watcher, scripted remote camera row, recording voice, no SpacetimeDB)", "steps": []}
    step = lambda name, **kv: report["steps"].append({"step": name, "t": round(time.time(), 3), **kv})  # noqa: E731

    settings = Settings(_env_file=None, data_dir=tmp / "data", robot_driver="mock", mock_autoconnect=True, port=PORT)
    rt = LockedInRuntime(settings, runtime_settings=RuntimeSettings(), persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False)
    h = Harness(rt)
    server = uvicorn.Server(uvicorn.Config(create_app(settings=settings, runtime=rt), host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    token_file = Path(os.environ["LOCKI_ALERT_TOKEN_FILE"])
    step("backend up", token_file_mode=oct(token_file.stat().st_mode & 0o777) if token_file.exists() else None)

    log = tmp / "alert.log"
    cmd = [str(_binary(app_path)), "--backend", BASE, "--token-file", str(token_file), "--state-dir", str(tmp / "run"), "--log", str(log),
           "--test-banner", "TEST HARNESS · simulated sensors, not a security event"]
    helper = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    async with httpx.AsyncClient(base_url=BASE, timeout=2.0) as c:
        async def takeover() -> dict:
            return (await c.get("/api/security")).json().get("takeover") or {}

        async def until(pred, within: float) -> bool:
            end = time.monotonic() + within
            while time.monotonic() < end:
                if pred(await takeover()):
                    return True
                await asyncio.sleep(0.05)
            return False

        report["native_connected"] = await until(lambda t: (t.get("native") or {}).get("connected"), 8.0)
        step("native alert connected", ok=report["native_connected"])

        await rt.security.arm(CODE)
        session_id = rt.security.session.session_id
        await asyncio.sleep(1.0)
        t = await takeover()
        step("armed, nothing seen", active=t.get("active"), record=bool(t.get("takeover")), mode=rt.security.mode.value)

        now_ms = int(time.time() * 1000)
        person = SecurityEventV1(event_id=str(uuid.uuid4()), device_id="jetson-01", source=SecuritySource.JETSON_VISION, kind=SecurityKind.PERSON_IN_ZONE,
                                 observed_at=now_ms, seq=1, confidence=0.91,
                                 metadata={"camera": "jetson_cam", "inference": "unreported", "armed": True, "session_id": session_id, "zone": "desk"})
        rt.security.apply_remote_camera_event(person, session_id, server_received_ms=now_ms)
        await asyncio.sleep(1.5)
        t = await takeover()
        step("person only", mode=rt.security.mode.value, active=t.get("active"), record=bool(t.get("takeover")), window_shown=bool((t.get("takeover") or {}).get("shown_at_ms")))

        await asyncio.sleep(2.5)  # a person, then a device a few seconds later, as in the demo
        plug_wall = time.time()
        h.usb.plug(STICK)
        shown = await until(lambda t: bool((t.get("takeover") or {}).get("shown_at_ms")), 5.0)
        t = await takeover()
        rec = t.get("takeover") or {}
        report["takeover"] = {k: rec.get(k) for k in ("id", "live", "trigger", "local_mode", "local_mode_verified", "headline", "sentence", "sentence_source", "response")}
        report["latency"] = {"plug_to_detected_ms": round(rec["detected_at_ms"] - plug_wall * 1000) if rec.get("detected_at_ms") else None,
                             "detected_to_window_ms": rec.get("shown_latency_ms"), "plug_to_window_ms": round(rec["shown_at_ms"] - plug_wall * 1000) if rec.get("shown_at_ms") else None}
        step("unknown USB plugged", window_shown=shown, mode=rt.security.mode.value)
        if shots is not None and shown:
            shots.mkdir(parents=True, exist_ok=True)
            await asyncio.sleep(0.4)  # past the entrance animation
            r = subprocess.run(["/usr/sbin/screencapture", "-x", str(shots / "e2e-takeover-main-display.png")], capture_output=True, text=True, timeout=10)
            report["screenshot"] = {"path": str(shots / "e2e-takeover-main-display.png"), "rc": r.returncode, "stderr": r.stderr.strip()[:200]}
        await asyncio.sleep(1.5)

        before = (rt.security.session.session_id, rt.security.mode.value, len(rt.security.evidence), dict(rt.security.incident or {}))
        r = await c.post("/api/security/takeover/report", json={"takeover_id": rec.get("id"), "event": "dismissed", "at_ms": int(time.time() * 1000)},
                         headers={"X-Locki-Alert-Token": token_file.read_text().strip()})
        hidden = await until(lambda t: bool((t.get("takeover") or {}).get("acknowledged_at_ms")), 3.0)
        await asyncio.sleep(1.0)
        after = (rt.security.session.session_id if rt.security.session else None, rt.security.mode.value, len(rt.security.evidence), dict(rt.security.incident or {}))
        step("dismissed (the same report request the Dismiss button sends)", status=r.status_code, acknowledged=hidden, still_armed=rt.security.session is not None,
             session_mode_evidence_incident_unchanged=before == after)

        helper.terminate()
        helper.wait(timeout=5)
        shown_before = sum(1 for e in _log_lines(log) if e.get("event") == "shown")
        helper = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        await asyncio.sleep(3.0)
        shown_after = sum(1 for e in _log_lines(log) if e.get("event") == "shown")
        step("alert window restarted", replayed=shown_after > shown_before, connected=(await takeover()).get("native", {}).get("connected"))

        await rt.security.disarm(CODE)
        cleared = await until(lambda t: not t.get("takeover") and not t.get("armed"), 3.0)
        step("disarmed with the owner code", record_cleared=cleared, armed=rt.security.session is not None)

    helper.terminate()
    try:
        helper.wait(timeout=5)
    except subprocess.TimeoutExpired:
        helper.kill()
    report["helper_exit"] = helper.returncode
    report["alert_log"] = _log_lines(log)[-12:]
    server.should_exit = True
    await serving
    await rt.shutdown()
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=str(Path.home() / ".locki/alert/LOCKI Alert.app"))
    ap.add_argument("--shots", default=None)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="locki-alert-e2e-"))
    (tmp / "run").mkdir(mode=0o700)
    _env(tmp)
    report = asyncio.run(run(tmp, Path(args.app).expanduser(), Path(args.shots) if args.shots else None))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
