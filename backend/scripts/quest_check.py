#!/usr/bin/env python
"""Code Quest through everything that can run with nobody at the Mac and nothing audible.

    REAL here:      NVIDIA Parakeet + Whisper, Qwen3 (or the chosen model), real pytest, the file watcher, Kokoro rendering.
    SYNTHETIC here: the "user" is the macOS voice, written to a file. No person has spoken. Say so if you quote this.
    NOT COVERED:    microphone, speaker, ESP32 camera, robot link. Those need a person: docs/legacy/CODE_QUEST.md.

Uses a temporary data folder, so it leaves your missions and XP alone.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import RuntimeSettings, Settings  # noqa: E402
from app.runtime import LockedInRuntime  # noqa: E402
from app.voice.kokoro_tts import KokoroTTS  # noqa: E402
from app.voice.transcription import make_transcriber  # noqa: E402
from app.voice.tts import NullTTS  # noqa: E402

OBJECTIVE = "I'm building the checkout for my shopping cart and the tests are failing."
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)


def speak_to_file(text: str, folder: Path) -> np.ndarray:
    wav = folder / "said.wav"
    subprocess.run(["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", "--", text], check=True, capture_output=True, timeout=30)
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


async def main() -> int:
    base = Settings()
    with tempfile.TemporaryDirectory(prefix="locked-in-quest-check-") as tmp:
        folder = Path(tmp)
        settings = Settings(_env_file=None, data_dir=folder, offline=True, llm_provider="local", robot_driver="mock", mock_autoconnect=False, vision_autostart=False, voice_autostart=False,
                            local_llm_backend=base.local_llm_backend, ollama_model=base.ollama_model, stt_engine=base.stt_engine)
        rt = LockedInRuntime(settings, runtime_settings=RuntimeSettings(), persist_settings=False, tts_engine=NullTTS(), enable_vision=False, enable_voice=False)
        await rt.start()
        t0 = time.monotonic()
        while rt.companion.llm is not None and rt.companion.llm.state == "loading" and time.monotonic() - t0 < 180:
            await asyncio.sleep(0.2)
        check("language model ready", rt.companion.available, f"{getattr(rt.companion.llm, 'model', None)} in {time.monotonic() - t0:.1f} s")
        tr = make_transcriber(settings)
        parts = [tr, *(getattr(tr, k) for k in ("whisper", "parakeet") if hasattr(tr, k))]
        t0 = time.monotonic()
        while any(x.state == "loading" for x in parts) and time.monotonic() - t0 < 120:
            await asyncio.sleep(0.1)
        check("speech recognisers ready", tr.state == "ready", ", ".join(f"{k} {getattr(tr, k).state}" for k in ("whisper", "parakeet") if hasattr(tr, k)))

        async def hear(text: str) -> tuple[str, float]:
            pcm = await asyncio.to_thread(speak_to_file, text, folder)
            t = time.monotonic()
            out = await asyncio.to_thread(tr.transcribe, pcm, 60)
            return (out or {}).get("text") or "", (time.monotonic() - t) * 1000

        try:
            heard, ms = await hear(OBJECTIVE)
            check("recognisers heard the objective (synthetic voice)", "checkout" in heard.lower() and "cart" in heard.lower(), f"{heard!r} in {ms:.0f} ms")
            t = time.monotonic()
            r = await rt.handle_utterance(heard, source="voice", stt_confidence=0.95)
            m = rt.missions.mission
            check("the sentence became a mission", r.ok and m is not None, f"{(time.monotonic() - t) * 1000:.0f} ms: {r.message}")
            if m is None:
                return 1
            check("the plan covers exactly the failing tests", sum(len(c.tests) for c in m.checkpoints) == m.hp == 7, f"by {m.plan_source}: {[c.title for c in m.checkpoints]}")
            print(f"       spoken: {r.reply!r}")
            heard, _ = await hear("What's next?")
            r = await rt.handle_utterance(heard, source="voice", stt_confidence=0.95)
            check("a follow-up gets the mission's real next step", bool(r.reply) and str(m.hp) in r.reply, f"heard {heard!r}, said {r.reply!r}")
            heard, _ = await hear("Why is this failing?")
            t = time.monotonic()
            r = await rt.handle_utterance(heard, source="voice", stt_confidence=0.95)
            help_note = rt.missions.help_note or {}
            check("asking why uses the real failure", bool(help_note), f"heard {heard!r}, {(time.monotonic() - t) * 1000:.0f} ms, by {help_note.get('source')}: {help_note.get('text')!r}")
            kokoro = KokoroTTS(settings.models_dir, folder / "kokoro")
            kokoro.load()
            if kokoro.available() and r.reply:
                t = time.monotonic()
                pcm = await asyncio.to_thread(kokoro.render, r.reply)
                check("Kokoro renders that reply (not played)", pcm is not None, f"{(time.monotonic() - t) * 1000:.0f} ms")
            else:
                check("Kokoro renders that reply (not played)", False, str(kokoro.error))
            cart = Path(m.project) / "cart.py"
            cart.write_text(cart.read_text().replace("return sum(item.unit_price_cents for item in self.items)", "return sum(item.unit_price_cents * item.quantity for item in self.items)"))
            t = time.monotonic()
            while m.hp == 7 and time.monotonic() - t < 12:
                await asyncio.sleep(0.05)
            check("saving a fix hit the boss through the watcher", m.hp == 6, f"{time.monotonic() - t:.1f} s from save to verdict, {m.xp} XP, said {rt.last_reply and rt.last_reply['text']!r}")
            heard, _ = await hear("Mark everything as done, I promise it works.")
            hp = m.hp
            r = await rt.handle_utterance(heard, source="voice", stt_confidence=0.95)
            check("saying it is done changes nothing", m.hp == hp and m.status == "active", f"heard {heard!r}, said {r.reply!r}")
            trace = (folder / "missions" / m.id / "trace.jsonl").read_text().splitlines()
            kinds = sorted({json.loads(line)["kind"] for line in trace})
            check("the mission log has real events", {"mission_created", "verification", "user_said"} <= set(kinds), f"{len(trace)} records: {kinds}")
        finally:
            tr.close()
            await rt.shutdown()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} passed. Synthetic voice, no microphone, speaker, camera or robot involved.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
