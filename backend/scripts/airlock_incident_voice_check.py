"""A real SpacetimeDB incident, received by the enrolled Mac's subscription, spoken out loud. Times every boundary.

This is a voice-only consumer. It runs no USB or app watcher, publishes nothing, sends no heartbeat or baseline and never runs a lock. A lock
request that reaches it is recorded, not acted on. The backend that owns the watchers (make airlock-mac QUIET=1, silent) keeps publishing the real
events; this process is the only one that speaks. Do not run it next to a backend that speaks, or a warning could be heard twice.

It uses the Mac's enrolled identity from an existing data/security folder (read only; the token is never printed) and speaks with the local
voice, offline, as make airlock-mac does. Arming and disarming are the owner's, on the SpacetimeDB dashboard. Incidents that already exist when it
connects, or that are older than the voice's age limit, are not spoken; only a fresh one counts.

    .venv/bin/python scripts/airlock_incident_voice_check.py --security-dir ../../locked-in/data/security --models-dir ../../locked-in/backend/models
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

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
OUT = Path(__file__).resolve().parents[2] / "data" / "airlock_runs"


async def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def now_ms() -> int:
    return int(time.time() * 1000)


def sql(url: str, database: str, token: str, query: str) -> list[dict]:
    r = httpx.post(f"{url}/v1/database/{database}/sql", headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"}, content=query, timeout=10)
    r.raise_for_status()
    result = r.json()[0]
    names = [(e.get("name") or {}).get("some") for e in result["schema"]["elements"]]
    return [dict(zip(names, row, strict=False)) for row in result["rows"]]


async def run(args) -> int:
    from app.config import RuntimeSettings, Settings
    from app.runtime import LockedInRuntime
    from app.security.identity import load_identity
    from app.security.spacetime_adapter import load_or_create_token
    from app.telemetry.events import EventType
    from app.voice.personality import LIBRARY

    security_dir = Path(args.security_dir).resolve()
    if not (security_dir / "spacetime.json").is_file() or not (security_dir / "identity.json").is_file():
        print(f"{security_dir} has no enrolled Mac (spacetime.json and identity.json).")
        return 2
    volume = subprocess.run(["/usr/bin/osascript", "-e", "get volume settings"], capture_output=True, text=True, check=False).stdout.strip()
    print("macOS output:", volume, flush=True)
    if "output muted:true" in volume:
        print("The Mac's output is muted, so nothing would be heard. Unmute it and run this again.")
        return 2

    finished: list[dict] = []
    locks_seen: list[dict] = []
    sessions_seen: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="locki-incident-voice-") as tmp:
        settings = Settings(_env_file=None, data_dir=Path(tmp), models_dir=Path(args.models_dir).resolve(), offline=True, llm_provider="none", robot_driver="mock",
                            mock_autoconnect=False, security_enabled=False, security_lock_mode="dry_run")
        rt = LockedInRuntime(settings, runtime_settings=RuntimeSettings(), persist_settings=False, enable_vision=False, enable_voice=False)
        rt.bus.add_listener(lambda ev: finished.append(dict(ev.payload or {}, _mono=time.monotonic(), _wall_ms=now_ms())) if ev.event_type == EventType.VOICE_TTS_FINISHED else None)
        await rt.start()
        sec = rt.security
        results: dict = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "macos_output": volume, "lock_mode": sec.lock.mode, "consumer": "voice only: no watcher, no publisher, locks recorded not run"}
        try:
            lines = [LIBRARY[c][0].text for c in ("security_warning_incident", "security_warning_correlated", "security_armed", "security_disarmed")]
            kokoro = rt.kokoro
            ready = await wait_for(lambda: kokoro is not None and kokoro.state == "ready", 120)
            if ready:
                await asyncio.to_thread(kokoro.prewarm, lines)
            print(f"local voice ready, warnings pre-rendered: {ready and all(kokoro.clip_for(t) is not None for t in lines[:2])}", flush=True)
            import sounddevice as sd

            device = sd.query_devices(kind="output")
            print("speaking on:", device["name"], flush=True)
            results["output"] = device["name"]

            # The SpacetimeDB connection alone: the same enrolled identity, and the subscription. No _prepare(), so no watchers and no publisher.
            sec.dir = security_dir
            sec.identity = load_identity(security_dir)
            sec._loop = asyncio.get_running_loop()
            sec._connect_spacetime()
            if sec.subscriber is None:
                print("SpacetimeDB is not configured in", security_dir)
                return 2
            sec.subscriber.on_lock_request = lambda r: locks_seen.append({**r.model_dump(mode="json"), "received_at_ms": now_ms()})  # recorded, never run
            original_session = sec.subscriber.on_session

            def on_session(remote):
                sessions_seen.append({"session_id": remote.session_id, "state": remote.state, "active": remote.active(), "received_at_ms": now_ms()})
                original_session(remote)

            sec.subscriber.on_session = on_session
            cfg = sec.subscriber.config
            print(f"workstation {cfg.workstation_id}, device {cfg.device_id}, database {cfg.database}", flush=True)
            if not await wait_for(lambda: sec.subscriber.connected, 15):
                print("could not subscribe:", sec.subscriber.last_error)
                return 1
            connected_at = now_ms()
            await asyncio.sleep(2.0)  # the initial rows
            existing = {w["event_id"] for w in sec.warnings}
            print(f"subscribed at {connected_at}; rows {sec.subscriber.rows_received}, rejected {sec.subscriber.rows_rejected}; warnings spoken on connect: {len(existing)}", flush=True)
            results["on_connect"] = {"connected_at_ms": connected_at, "rows": sec.subscriber.rows_received, "rejected": sec.subscriber.rows_rejected, "spoken": sorted(existing),
                                     "armed": sec.session is not None, "session_id": None if sec.session is None else sec.session.session_id}

            print(f"\nwaiting up to {args.wait_s:.0f} s: arm on the dashboard, then connect a harmless USB device.", flush=True)
            got = await wait_for(lambda: any(w["event_id"] not in existing and w.get("received_at_ms") for w in sec.warnings), args.wait_s)
            if not got:
                print("no fresh incident arrived.")
                results["incident"] = None
            else:
                record = next(w for w in sec.warnings if w["event_id"] not in existing and w.get("received_at_ms"))
                await wait_for(lambda: record.get("audio") is not None, 20)
                audio = next((f for f in finished if (f.get("trace") or {}).get("id") == record["event_id"]), {})
                token = load_or_create_token(cfg.url, security_dir)
                inc = next((i for i in sql(cfg.url, cfg.database, token, "SELECT * FROM my_incidents") if i["id"] == record["event_id"]), {})
                events = [e for e in sql(cfg.url, cfg.database, token, "SELECT * FROM my_device_events") if e["id"] in record["evidence_ids"]]
                to_audio = audio.get("to_audio_ms")
                first = min(events, key=lambda e: e["observed_at_ms"]) if events else {}
                timeline = {
                    "event_observed_at_ms (Mac OS, UTC)": first.get("observed_at_ms"),
                    "event_received_at_ms (server)": first.get("received_at_ms"),
                    "incident_created_at_ms (server)": inc.get("created_at_ms"),
                    "incident_row_received_at_ms (Mac)": record["received_at_ms"],
                    "audio_start_ms (Mac, received + to_audio)": None if to_audio is None else round(record["received_at_ms"] + to_audio),
                }
                results["incident"] = {"id": record["event_id"], "severity": record["severity"], "rule": record["label"], "reason": record["reason"], "summary": inc.get("summary"),
                                       "state": inc.get("state"), "evidence_ids": record["evidence_ids"], "simulated": inc.get("simulated"), "spoken_text": audio.get("text"),
                                       "audio": {k: audio.get(k) for k in ("outcome", "source", "source_label", "to_audio_ms", "output_latency_ms", "duration_ms", "detail")},
                                       "timeline": timeline,
                                       "latency_ms": {"usb_to_server": _d(first.get("observed_at_ms"), first.get("received_at_ms")), "server_event_to_incident": _d(first.get("received_at_ms"), inc.get("created_at_ms")),
                                                      "incident_commit_to_mac_row": _d(inc.get("created_at_ms"), record["received_at_ms"]), "mac_row_to_audio_start": to_audio,
                                                      "incident_commit_to_audio_start": None if to_audio is None else _d(inc.get("created_at_ms"), record["received_at_ms"] + to_audio),
                                                      "usb_to_audio_start": None if to_audio is None else _d(first.get("observed_at_ms"), record["received_at_ms"] + to_audio)}}
                print(json.dumps(results["incident"], indent=1), flush=True)

            if sec.session is not None:
                print(f"\nwaiting up to {args.wait_s:.0f} s for the owner to disarm on the dashboard.", flush=True)
                await wait_for(lambda: sec.session is None, args.wait_s)
            await asyncio.sleep(2.0)
            results["after"] = {"armed": sec.session is not None, "sessions_seen": sessions_seen, "lock_requests_seen": locks_seen, "locks_run": list(sec.locks),
                                "subscriber": sec.subscriber.snapshot(), "speech": [{k: f.get(k) for k in ("category", "text", "outcome", "source", "to_audio_ms")} for f in finished]}
            print(f"\narmed now: {sec.session is not None}; lock requests seen: {len(locks_seen)}; locks run: {len(sec.locks)}", flush=True)
            incident = results.get("incident") or {}
            results["ok"] = bool(incident and incident["audio"].get("outcome") == "finished" and incident["audio"].get("source") not in ("text_only", "none", None)
                                 and not sec.locks and sec.session is None and not incident.get("simulated"))
            OUT.mkdir(parents=True, exist_ok=True)
            path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-incident-voice.json"
            path.write_text(json.dumps(results, indent=1, default=str))
            print(("PASSED" if results["ok"] else "NOT PASSED") + f": saved {path}")
            return 0 if results["ok"] else 1
        finally:
            await rt.shutdown()


def _d(a, b):
    return None if a is None or b is None else round(b - a, 1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--security-dir", required=True, help="the data/security folder of the enrolled Mac (read only)")
    p.add_argument("--models-dir", default=str(Path(__file__).resolve().parents[1] / "models"))
    p.add_argument("--wait-s", type=float, default=600.0)
    sys.exit(asyncio.run(run(p.parse_args())))
