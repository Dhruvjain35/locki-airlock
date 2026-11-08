"""A real investigation on a real Mac event, with a real model. It opens Calculator hidden, which is on the example watchlist, so the incident is
genuine, then lets the model work through it with the real tools. No microphone, camera or speaker; no lock; nothing leaves this Mac. Results go to
data/airlock_runs/.

    .venv/bin/python scripts/airlock_investigate.py                        # Nemotron through Ollama if it is there, else the Qwen3 fallback
    .venv/bin/python scripts/airlock_investigate.py --require-nemotron     # fail rather than fall back (for the M5 Pro)
    .venv/bin/python scripts/airlock_investigate.py --ollama-url http://<m5-private-address>:11434
    .venv/bin/python scripts/airlock_investigate.py --relay                # also record with real NeMo Relay
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
TEST_APP = "com.apple.calculator"


async def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def swap_used_mb() -> float | None:
    try:
        text = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, check=False).stdout
        return float(text.split("used = ")[1].split("M")[0])
    except (OSError, IndexError, ValueError):
        return None


async def run(args) -> int:
    from app.config import RuntimeSettings, Settings
    from app.runtime import LockedInRuntime
    from app.security.session import ResponsePolicy
    from app.voice.tts import NullTTS

    if subprocess.run(["/usr/bin/pgrep", "-x", "Calculator"], capture_output=True, check=False).returncode == 0:
        print("Calculator is already open, so it cannot be used for this test: close it and run this again.")
        return 2
    with tempfile.TemporaryDirectory(prefix="locki-airlock-") as tmp:
        settings = Settings(
            # With --require-nemotron the conversation model is not needed, so it is not loaded: nothing falls back to it.
            _env_file=None, data_dir=Path(tmp), offline=True, llm_provider="none" if args.require_nemotron else "local", robot_driver="mock", mock_autoconnect=False, security_enabled=True, airlock_model=args.model, airlock_ollama_url=args.ollama_url,
            airlock_fallback_model=not args.require_nemotron, relay_enabled=args.relay,
        )
        rt = LockedInRuntime(settings, runtime_settings=RuntimeSettings(), persist_settings=False, tts_engine=NullTTS(), enable_vision=False, enable_voice=False)
        await rt.start()
        code = 0
        try:
            model_ready = await wait_for(lambda: rt.companion.llm is None or rt.companion.llm.state != "loading", 200)
            # Bolt's first load on the M5 can take a while: the adapter's warm-up waits for it.
            await wait_for(lambda: rt.security._agent_model() is not None and getattr(rt.security._nemotron, "llm", None) is not None and rt.security._nemotron.llm.state != "loading", 240)
            security = rt.security
            model = security._agent_model()
            await wait_for(lambda: security._nemotron is not None and security._nemotron.llm.state != "loading", 200)
            model = security._agent_model()
            print(f"model: {None if model is None else model.identity}; available: {None if model is None else model.available()}; conversation model ready: {model_ready}", flush=True)
            if model is None or not model.available():
                print("no investigation model:", None if model is None else getattr(model, "why_unavailable", None))
                return 3
            if args.require_nemotron and not model.qualifies_as_nemotron:
                print(f"{model.identity} is not Nemotron, and --require-nemotron was given.")
                return 3
            await security.arm("airlock-check-code", ResponsePolicy())
            await asyncio.sleep(0.2)
            subprocess.run(["/usr/bin/open", "-g", "-j", "-b", TEST_APP], check=True)
            if not await wait_for(lambda: security.incident is not None, 10):
                print("the real launch did not raise an incident")
                return 1
            event = next(e for e in security.evidence if e.metadata.get("bundle_id") == TEST_APP)
            print(f"real incident {security.incident['incident_id']}, evidence {event.event_id}: {event.metadata['name']} launched, watchlist {event.metadata['watchlist_category']}", flush=True)
            swap0 = swap_used_mb()
            relay = await security._relay_for_run()
            t0 = time.monotonic()
            try:
                result = await security.agent().investigate(security._incident_for_agent(), model, relay=relay)
            finally:
                await security._relay_done(relay)
            data = result.to_dict()
            relay_file = Path((result.relay or {}).get("events_path") or "")
            if relay_file.is_file():  # Relay's own events, kept with the run: the temporary folder goes when this ends
                OUT.mkdir(parents=True, exist_ok=True)
                kept = OUT / f"{result.run_id}-relay-events.jsonl"
                kept.write_text(relay_file.read_text())
                data["relay_events_file"] = str(kept)
            data["wall_s"] = round(time.monotonic() - t0, 1)
            data["swap_mb_before"], data["swap_mb_after"] = swap0, swap_used_mb()
            data["machine"] = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=False).stdout.strip()
            data["gate"] = rt.tool_gate.snapshot()
            data["at"] = time.strftime("%Y-%m-%d %H:%M")
            memory = getattr(getattr(model, "llm", None), "memory", None)
            if memory is not None:
                data["memory"] = await asyncio.to_thread(memory, 2.0)
            OUT.mkdir(parents=True, exist_ok=True)
            path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{''.join(c if c.isalnum() else '_' for c in model.identity)[:40]}.json"
            path.write_text(json.dumps(data, indent=1, default=str))
            print()
            for s in result.steps:
                print(f"{s.step:2} {s.tool:30} {s.status:7} model {s.model_ms or 0:7.0f} ms tool {s.duration_ms:7.1f} ms  {(s.why or '')[:70]}" + (f"\n     error: {s.error}" if s.error else ""))
            print(f"\nmeasures: {data['measures']}; swap MB {swap0} -> {data['swap_mb_after']}; model memory {data.get('memory')}")
            print(f"status {result.status}; approved tool calls {result.approved_tool_calls}; {data['denied_tool_calls']} denied; {data['failed_tool_calls']} failed; {data['wall_s']} s; first model reply {result.first_model_ms} ms; counts as Nemotron: {result.qualifies_as_nemotron}")
            if result.problems:
                print("problems:", result.problems[:6])
            print("outcome from the record:", result.outcome_checked)
            print("report:", json.dumps(result.report, indent=1) if result.report else None)
            print("relay:", result.relay)
            print("saved", path)
            code = 0 if result.status == "completed" else 1
        finally:
            for e in list(rt.security.evidence):
                if e.metadata.get("bundle_id") == TEST_APP and e.metadata.get("pid"):
                    subprocess.run(["/bin/kill", str(int(e.metadata["pid"]))], check=False)
            await rt.shutdown()
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="nemotron-3.5-bolt:e4b")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--require-nemotron", action="store_true")
    ap.add_argument("--relay", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
