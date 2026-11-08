"""Milestone 5.6: a real person talks to LOCKED IN (docs/NVIDIA_EVALUATION.md, human test).

    cd backend && .venv/bin/python scripts/human_conversation_test.py --output "WF-C510"

Push-to-talk in this terminal: Enter starts recording from the Mac's microphone, Enter again
stops. The sentence goes through the real runtime exactly as in the app: both recognisers
(Parakeet + whisper.cpp) hear the same audio, the companion answers (local Qwen3-4B, Kokoro,
cached Fish Audio clips; offline, no new Fish requests), the reply plays on --output. The
robot is simulated. After each turn you confirm or correct what you said and judge the reply,
so every number in the summary is checked against what you actually said.

Commands at the prompt: Enter = talk, r = a simulated confirmed reach (labelled simulated),
q = finish. Audio and transcripts stay in data/human_tests/<run>/ (git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import bench_local_ai as bl  # noqa: E402
import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from app.config import RuntimeSettings, Settings  # noqa: E402
from app.runtime import LockedInRuntime, VisionEvent  # noqa: E402
from app.telemetry.events import EventSource, EventType  # noqa: E402
from app.voice.intents import IntentClassifier  # noqa: E402
from app.voice.transcription import make_transcriber  # noqa: E402

SR = 16000
CLF = IntentClassifier()


def intent(text: str | None) -> str:
    return str(CLF.classify(text).name) if (text or "").strip() else "(nothing)"


def speech_end_s(audio: np.ndarray) -> float | None:
    """Seconds into the clip where speech last stopped (Silero VAD), or None if no speech."""
    if len(audio) < 512:
        return None
    probs = bl.silero_probs(audio.astype(np.float32))
    last = max((i for i, p in enumerate(probs) if p >= 0.5), default=None)
    return None if last is None else (last + 1) * 512 / SR


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


DRY: list[Path] = []  # --dry-run: these files stand in for the microphone (no capture, no sound)


class SilentOut:
    last_start_mono: float | None = None
    last_latency_ms: float | None = None

    async def play(self, pcm, sample_rate: int, device=None, volume: float = 1.0) -> None:
        self.last_start_mono = time.monotonic()
        await asyncio.sleep(len(pcm) / sample_rate)


async def record(device) -> tuple[np.ndarray, float, float]:
    """Record until Enter. Returns (audio, start monotonic, release monotonic)."""
    if DRY:
        audio = bl.load_wav(DRY.pop(0))[0]
        t0 = time.monotonic()
        await asyncio.sleep(len(audio) / SR)
        await ask("  ● (dry run: file played as speech) press Enter ")
        return audio, t0, time.monotonic()
    chunks: list[np.ndarray] = []
    stream = sd.InputStream(samplerate=SR, blocksize=480, channels=1, dtype="float32", device=device,
                            callback=lambda data, frames, t, status: chunks.append(data[:, 0].copy()))
    stream.start()
    t0 = time.monotonic()
    await ask("  ● recording… press Enter when you are done ")
    released = time.monotonic()
    stream.stop()
    stream.close()
    return (np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)), t0, released


async def wait_reply(rt, turn: dict, timeout_s: float = 20.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if turn.get("reply_to_audio_ms") is not None and not rt.speaker.speaking and not rt.speaker._items:
            return
        await asyncio.sleep(0.05)


def memory_now() -> dict:
    import subprocess

    swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout.strip()
    free = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True).stdout.strip().splitlines()
    return {"swap": swap, "memory_free": free[-1] if free else None}


async def main_async(args) -> int:
    run_dir = ROOT / "data" / "human_tests" / (time.strftime("%Y%m%d-%H%M%S") + ("-dry-run" if args.dry_run else ""))
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=ROOT / "data", robot_driver="mock", mock_autoconnect=True, offline=True, llm_provider="local",
                        stt_engine=args.stt)
    rs = RuntimeSettings()
    rs.voice.output_device = args.output or None
    rt = LockedInRuntime(settings, runtime_settings=rs, persist_settings=False, enable_vision=False, enable_voice=False)
    if args.dry_run:
        rt.speaker.engine.audio = SilentOut()
        DRY.extend(Path(f) for f in args.dry_run)
    await rt.start()
    stt = make_transcriber(settings)
    parts = [stt, *(getattr(stt, k) for k in ("whisper", "parakeet") if hasattr(stt, k))]
    print("Loading the recognisers, the language model and the voice (about 20 s)…")
    t0 = time.monotonic()
    while (any(x.state == "loading" for x in parts) or rt.companion.llm is None or rt.companion.llm.state == "loading"
           or (rt.kokoro and rt.kokoro.state == "loading")) and time.monotonic() - t0 < 180:
        await asyncio.sleep(0.2)
    if rt._kokoro_task is not None:
        await rt._kokoro_task
    rt.vision_status = {"running": False, "camera": "off"}
    print(f"Ready: speech {stt.describe().get('engine')} ({', '.join(f'{k} {getattr(stt, k).state}' for k in ('whisper', 'parakeet') if hasattr(stt, k))}), "
          f"model {rt.companion.llm.state if rt.companion.llm else 'none'}, voice → {args.output or 'system default output'}")
    print("Talk naturally: start a session in your own words, ask things, make excuses, change your mind, ask for quiet, end it.")
    turns, events, mem = [], [], [memory_now()]
    n = 0
    try:
        while True:
            cmd = (await ask(f"\n[{n + 1}] Enter = talk, r = simulated reach, q = finish › ")).lower()
            if cmd == "q":
                break
            if cmd == "r":
                before = len(rt.behavior.decisions)
                await rt.handle_vision_event(VisionEvent("confirmed", {"track": 900 + n}, confidence=0.9, frame_mono=time.monotonic(), simulated=True))
                await asyncio.sleep(4.0)
                new = list(rt.behavior.decisions)[before:]
                spoke = any(d.speak for d in new)
                said = next((d.said for d in reversed(new) if d.speak), None)
                print(f"  (simulated reach) robot {'said: ' + repr(said) if spoke else 'stayed silent'} — {new[-1].reason if new else 'no decision'}")
                verdict = await ask("  Was speaking / staying silent right here? [y/n] ")
                events.append({"kind": "simulated_reach", "spoke": spoke, "said": said, "reason": new[-1].reason if new else None, "right": verdict.lower().startswith("y")})
                continue
            rt.bus.publish(EventType.VOICE_SPEECH_STARTED, EventSource.VOICE, {"mode": "push_to_talk"})
            audio, started, released = await record(args.input)
            n += 1
            if len(audio) < SR * 0.2:
                print("  (too short, ignored)")
                n -= 1
                continue
            rt._stt_busy(True)
            try:
                res = await asyncio.to_thread(stt.transcribe, audio)
            finally:
                rt._stt_busy(False)
            res = res or {"text": "", "confidence": 0.0, "roundtrip_ms": None}
            r = await rt.handle_utterance(res["text"], source="voice", stt_confidence=res.get("confidence", 0.0), stt_ms=res.get("roundtrip_ms"),
                                          heard_mono=released)
            turn = rt.voice_turns[-1] if rt.voice_turns else {}
            await wait_reply(rt, turn)
            end_s = speech_end_s(audio)
            speech_end = started + end_s if end_s is not None else None
            reply_ms = turn.get("reply_to_audio_ms")
            rd = res.get("readings") or {}
            print(f"  heard  (combined): {res['text']!r}  [{res.get('decided_by', '-')}]")
            if rd:
                print(f"         whisper.cpp : {(rd.get('whisper') or {}).get('text')!r}")
                print(f"         Parakeet    : {(rd.get('parakeet') or {}).get('text')!r}")
            print(f"  robot: {r.reply!r}  · route {turn.get('route')} · action {(r.data.get('action_request') or {}).get('name')}"
                  f" · release → voice {reply_ms} ms")
            ref = await ask("  What did you actually say? (Enter = the combined text is exactly right) › ") or res["text"]
            ok = (await ask("  Was the robot's reply/action right? [y] / [n] wrong / [s] should have stayed quiet › ")).lower()[:1] or "y"
            natural = await ask("  How natural, 1-5 (Enter = skip) › ")
            note = await ask("  Note (Enter = none) › ")
            wav = run_dir / f"turn-{n:02d}.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
            row = {
                "n": n, "reference": ref, "combined": res["text"], "decided_by": res.get("decided_by"), "confidence": res.get("confidence"),
                "whisper": (rd.get("whisper") or {}).get("text"), "parakeet": (rd.get("parakeet") or {}).get("text"),
                "wer_combined": bl.wer(ref, res["text"]), "wer_whisper": bl.wer(ref, (rd.get("whisper") or {}).get("text") or "") if rd else None,
                "wer_parakeet": bl.wer(ref, (rd.get("parakeet") or {}).get("text") or "") if rd else None,
                "intent_ref": intent(ref), "intent_combined": intent(res["text"]), "intent_whisper": intent((rd.get("whisper") or {}).get("text")),
                "route": turn.get("route"), "reply": r.reply, "action": (r.data.get("action_request") or {}).get("name"),
                "judged": {"y": "right", "n": "wrong", "s": "should have stayed quiet"}.get(ok, ok), "natural": int(natural) if natural.isdigit() else None,
                "note": note or None, "audio_s": round(len(audio) / SR, 2), "stt_ms": res.get("roundtrip_ms"),
                "release_to_voice_ms": reply_ms,
                "speech_end_to_voice_ms": None if (speech_end is None or reply_ms is None) else round(released + reply_ms / 1000 - speech_end, 3) * 1000,
                "release_after_speech_ms": None if speech_end is None else round((released - speech_end) * 1000),
                "reply_source": turn.get("reply_source"), "wav": wav.name,
            }
            turns.append(row)
            (run_dir / "turns.jsonl").open("a").write(json.dumps(row) + "\n")
            if n % 5 == 0:
                mem.append(memory_now())
    finally:
        mem.append(memory_now())
        stt.close()
        await rt.shutdown()

    def p50(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.median(xs)) if xs else None

    cmds = [t for t in turns if t["intent_ref"] not in ("unknown", "(nothing)")]
    summary = {
        "turns": len(turns), "stt_engine": args.stt,
        "wer_mean": {k: round(statistics.fmean(t[f"wer_{k}"] for t in turns if t[f"wer_{k}"] is not None), 4) if turns else None
                     for k in ("combined", "whisper", "parakeet")},
        "exact": {k: sum(1 for t in turns if t[f"wer_{k}"] == 0) for k in ("combined", "whisper", "parakeet")},
        "commands": len(cmds), "intent_right": {"combined": sum(t["intent_combined"] == t["intent_ref"] for t in cmds),
                                                "whisper": sum(t["intent_whisper"] == t["intent_ref"] for t in cmds)},
        "judged": {k: sum(1 for t in turns if t["judged"] == k) for k in ("right", "wrong", "should have stayed quiet")},
        "natural_mean": round(statistics.fmean(t["natural"] for t in turns if t["natural"]), 2) if any(t["natural"] for t in turns) else None,
        "release_to_voice_ms_p50": p50([t["release_to_voice_ms"] for t in turns]),
        "speech_end_to_voice_ms_p50": p50([t["speech_end_to_voice_ms"] for t in turns]),
        "model_turns_speech_end_to_voice_ms_p50": p50([t["speech_end_to_voice_ms"] for t in turns if t["route"] == "converse"]),
        "events": events, "memory": mem, "run_dir": str(run_dir.relative_to(ROOT)),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + json.dumps({k: v for k, v in summary.items() if k not in ("events", "memory")}, indent=1))
    print(f"Saved in {run_dir.relative_to(ROOT)} (git-ignored). Thank you!")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="MacBook Air Microphone", help="input device name (default: the Mac's microphone)")
    p.add_argument("--output", default="", help='output device name, e.g. "WF-C510" (default: the system output)')
    p.add_argument("--stt", default="parakeet+whisper", choices=["parakeet+whisper", "whisper.cpp"])
    p.add_argument("--dry-run", nargs="+", default=None, help="WAV files instead of the microphone, silent output (to check the script)")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
