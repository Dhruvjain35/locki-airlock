"""Milestone 5.5: NVIDIA speech candidates against the current local stack, on this Mac
(docs/NVIDIA_EVALUATION.md).

    .venv/bin/python scripts/bench_nvidia.py asr [--limit N]     # accuracy, latency, start-up, memory
    .venv/bin/python scripts/bench_nvidia.py asr-load            # the same under language-model + vision load

Engines: whisper.cpp base.en through the app's own worker process (the current default) and
the NVIDIA models through NeMo-Speech.cpp (`nemo-speech serve` on 127.0.0.1, local GGUF files),
inside the macOS sandbox with every outbound connection except localhost blocked. Nothing is
played aloud and no microphone or camera is opened: the audio is macOS voices, the Milestone 4
speaker-to-microphone recording and LibriSpeech read speech (data/asr_eval/, see the docs).
Results: docs/benchmarks/m55-<what>-<time>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import wave
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_local_ai as bl  # noqa: E402

ROOT, BACKEND, MODELS = bl.ROOT, bl.BACKEND, bl.MODELS
NEMO = MODELS / "nemo-speech"
NEMO_BIN = NEMO / "runtime-0.3.0" / "bin" / "nemo-speech"
NEMO_MODELS = NEMO / "models" / "nvidia"
ASR_GGUF = {
    "nemotron-3.5": NEMO_MODELS / "nemotron-3.5-asr-streaming-0.6b" / "1c8deaecc64b91f034d73e08dd8b64625eb3395d" / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf",
    "parakeet-v3": NEMO_MODELS / "parakeet-tdt-0.6b-v3" / "541d1f99c6b0c3cd0b11a95167540bb8edefd82b" / "parakeet-tdt-0.6b-v3.q8_0.gguf",
}
HUMAN = ROOT / "data" / "asr_eval" / "librispeech_dummy"
SANDBOX = (
    '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
    "(allow network-outbound (remote unix-socket))"
    '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
)

# Written before any engine ran; what each clip says is the reference transcript.
CONVO_PHRASES = [
    "Um, okay, so can you lock me in for like thirty minutes?",
    "Yo, gimme my phone back real quick.",
    "Wait, no, not twenty, make it forty-five.",
    "I was gonna say, uh, never mind.",
    "Hold on, I'm gonna grab some water.",
    "Nah, I'm good, keep going.",
    "Bro, why are you like this?",
    "Can you, like, chill for a sec?",
    "So I'm working on my history essay, it's due Friday.",
    "Honestly I think I'm done for today.",
    "How much longer do I got?",
    "Okay, I'm back, what did I miss?",
    "Stop. Stop! I said stop.",
    "Emergency stop right now.",
    "Turn the camera off for a minute.",
    "Mute the mic please.",
    "I finished, like, three more problems.",
    "Is it just me or is this taking forever?",
    "Be real with me, am I doing okay?",
    "I need, um, the calculator, it's on my phone.",
    "Let's call it here.",
    "Can I check one text? It's my mom.",
    "Pause it, I gotta use the bathroom.",
    "Resume the session.",
    "Don't roast me today, I'm tired.",
    "Make it harder.",
    "You can talk again now.",
    "What was I supposed to be doing?",
    "Okay so basically, uh, the derivative of x squared is two x.",
    "Wait wait wait, what?",
]
CONVO_VOICES = ["Eddy (English (US))", "Flo (English (US))"]
BABBLE_VOICES = ["Moira", "Karen", "Grandpa (English (US))"]
BABBLE_TEXT = [
    "The bus was late again this morning so we walked the whole way to campus.",
    "Did you see the game last night, the second half was unbelievable.",
    "I think the library closes at ten on weekdays but not on Sundays.",
]
NOISE_PHRASES = bl.STT_PHRASES[:16]


# ---------------------------------------------------------------------------------- audio sets
def wav_bytes(audio: np.ndarray, sr: int = 16000) -> bytes:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def at_level(x: np.ndarray, db: float) -> np.ndarray:
    return (x * (10 ** (db / 20) / (np.sqrt(np.mean(x**2)) + 1e-9))).astype(np.float32)


class Noise:
    def __init__(self, tmp: Path, seed: int = 11, talkers: tuple[list, list] = (BABBLE_VOICES, BABBLE_TEXT)):
        self.rng = np.random.default_rng(seed)
        talk = [bl.say_16k(t, v, tmp) for v, t in zip(*talkers, strict=True)]
        n = max(len(t) for t in talk)
        self.babble_bed = sum(np.pad(t, (0, n - len(t))) for t in talk).astype(np.float32)

    def __call__(self, kind: str, n: int) -> np.ndarray:
        rng = self.rng
        if kind == "white":
            return rng.normal(0, 1, n).astype(np.float32)
        if kind == "keyboard":
            x = np.zeros(n, dtype=np.float32)
            for i in rng.integers(0, max(1, n - 200), max(1, n // 1600)):
                x[i : i + 160] += rng.normal(0, 1, 160).astype(np.float32) * np.exp(-np.arange(160) / 25)
            return x
        if kind == "hum":
            t = np.arange(n) / 16000
            return (np.sin(2 * np.pi * 120 * t) + 0.5 * np.sin(2 * np.pi * 240 * t) + 0.3 * rng.normal(0, 1, n)).astype(np.float32)
        if kind == "babble":  # three other people talking at once, from a random offset
            start = int(rng.integers(0, max(1, len(self.babble_bed) - 1)))
            return np.resize(np.roll(self.babble_bed, -start), n).astype(np.float32)
        if kind == "tones":  # music-like: a new pitch every half second
            t = np.arange(n) / 16000
            f = 220 * 2 ** (rng.integers(0, 12, 1 + n // 8000) / 12)
            return np.concatenate([np.sin(2 * np.pi * f[k] * t[k * 8000 : (k + 1) * 8000]) for k in range(len(f))])[:n].astype(np.float32)
        raise ValueError(kind)


def build_sets(tmp: Path, limit: int | None = None) -> dict[str, list[tuple[str, str, np.ndarray]]]:
    """name -> [(label, reference text, 16 kHz float32 audio)]. Same audio for every engine."""
    sets: dict[str, list] = {"core": bl.stt_test_set(tmp)}
    sets["conversational"] = [(f"say:{v}", t, bl.say_16k(t, v, tmp)) for v in CONVO_VOICES for t in CONVO_PHRASES]
    if HUMAN.exists():
        meta = json.loads((HUMAN / "transcripts.json").read_text())
        sets["human"] = [("human:librispeech", m["text"], bl.load_wav(HUMAN / m["file"])[0]) for m in meta]
    noise = Noise(tmp)
    clean = [(t, at_level(bl.say_16k(t, "Samantha", tmp), -26)) for t in NOISE_PHRASES]
    pad = np.zeros(4800, dtype=np.float32)
    sets["noise"] = []
    for kind in ("white", "babble", "keyboard", "hum"):
        for snr in (10, 0):
            for text, sp in clean:
                x = np.concatenate([pad, sp, pad])
                sets["noise"].append((f"{kind}@{snr}dB", text, (x + at_level(noise(kind, len(x)), -26 - snr)).astype(np.float32)))
    sets["false_activation"] = []
    for kind, db in (("silence", -70), ("white", -40), ("keyboard", -35), ("hum", -35), ("tones", -35), ("babble", -35)):
        src = "white" if kind == "silence" else kind
        sets["false_activation"] += [(f"{kind}@{db}dBFS", "", at_level(noise(src, 48000), db)) for _ in range(8)]
    if limit:
        sets = {k: v[:limit] for k, v in sets.items()}
    return sets


# ---------------------------------------------------------------------------------- engines
def footprint_mb(pid: int) -> float | None:
    """Physical footprint (what Activity Monitor shows; includes Metal buffers)."""
    try:
        out = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"phys_footprint:\s*([\d.]+)\s*([KMG]B)", out) or re.search(r"Footprint:\s*([\d.]+)\s*([KMG]B)", out)
        if m:
            return round(float(m.group(1)) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)], 1)
    except Exception:
        pass
    try:
        import psutil

        return round(psutil.Process(pid).memory_info().rss / 2**20, 1)
    except Exception:
        return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NemoServer:
    """`nemo-speech serve` with one ASR model (and optionally TTS), sandboxed, local files only."""

    def __init__(self, asr: Path | None = None, extra: list[str] | None = None, device: str = "metal"):
        self.port = free_port()
        cmd = ["sandbox-exec", "-p", SANDBOX, str(NEMO_BIN), "serve", "--host", "127.0.0.1", "--port", str(self.port), "--device", device, "--quiet"]
        if asr:
            cmd += ["--asr-model", str(asr)]
        cmd += extra or []
        env = {**os.environ, "NEMO_SPEECH_MODEL_DIR": str(NEMO / "models")}
        self.log = tempfile.NamedTemporaryFile("w+", prefix="nemo-speech-", suffix=".log", delete=False)
        self.t0 = time.perf_counter()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self.log, env=env, text=True)
        self.base = f"http://127.0.0.1:{self.port}"
        import httpx

        self.http = httpx.Client(timeout=60)
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError(f"nemo-speech serve exited: {Path(self.log.name).read_text()[-800:]}")
            try:
                if self.http.get(f"{self.base}/v1/models").status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        self.start_ms = round((time.perf_counter() - self.t0) * 1000)

    def transcribe(self, audio: np.ndarray, verbose: bool = True) -> dict:
        t = time.perf_counter()
        r = self.http.post(f"{self.base}/v1/audio/transcriptions", files={"file": ("u.wav", wav_bytes(audio), "audio/wav")},
                           data={"response_format": "verbose_json" if verbose else "json"})
        ms = (time.perf_counter() - t) * 1000
        r.raise_for_status()
        body = r.json()
        words = body.get("words") or []
        conf = [float(w.get("confidence", 0)) for w in words if w.get("confidence") is not None]
        return {"text": body.get("text", "").strip(), "ms": ms, "confidence": round(float(np.exp(np.mean(np.log(np.clip(conf, 1e-6, 1))))), 3) if conf else None,
                "min_word_confidence": round(min(conf), 3) if conf else None}

    async def stream(self, audio: np.ndarray, frame_ms: int = 20) -> dict:
        """Feed PCM16 at real-time pace like the microphone; partials and the final come back."""
        import websockets

        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
        step = 16 * frame_ms
        out = {"first_partial_ms": None, "partials": 0}
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/v1/audio/transcriptions/realtime", max_size=None) as ws:
            await ws.recv()  # session.created
            done = asyncio.Event()
            final: dict = {}

            async def reader():
                async for msg in ws:
                    ev = json.loads(msg)
                    typ = ev.get("type", "")
                    if typ.endswith(".delta") and ev.get("delta", "").strip() and out["first_partial_ms"] is None:
                        out["first_partial_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
                    if typ.endswith(".delta"):
                        out["partials"] += 1
                    if typ.endswith(".completed"):
                        final.setdefault("text", "")
                        final["text"] = (final["text"] + " " + ev.get("transcript", "")).strip()
                        final["t"] = time.perf_counter()
                    if typ == "input_audio_buffer.committed":
                        final["committed"] = time.perf_counter()
                    if typ == "error":
                        final["error"] = ev
                        done.set()
                    if "t" in final and "committed" in final:
                        done.set()

            task = asyncio.create_task(reader())
            t_start = time.perf_counter()
            for i in range(0, len(pcm), step):
                await ws.send(pcm[i : i + step].tobytes())
                await asyncio.sleep(max(0.0, t_start + (i + step) / 16000 - time.perf_counter()))
            t_end = time.perf_counter()
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            try:
                await asyncio.wait_for(done.wait(), 15)
            except TimeoutError:
                final["error"] = "timeout"
            task.cancel()
        out["text"] = final.get("text", "")
        out["final_after_end_ms"] = round((final["t"] - t_end) * 1000, 1) if "t" in final else None
        out["error"] = final.get("error")
        return out

    def footprint_mb(self) -> float | None:
        import psutil

        kids = psutil.Process(self.proc.pid).children(recursive=True)
        return footprint_mb(kids[0].pid if kids else self.proc.pid)

    def close(self) -> None:
        self.http.close()
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class WhisperEngine:
    """The app's whisper.cpp worker process (app/voice/transcription.py), exactly as used live."""

    def __init__(self):
        from app.voice.transcription import WhisperTranscriber

        t0 = time.perf_counter()
        self.tr = WhisperTranscriber("base.en", "int8", MODELS, engine="whisper.cpp")
        while self.tr.state == "loading":
            time.sleep(0.02)
        assert self.tr.state == "ready", self.tr.error
        self.start_ms = round((time.perf_counter() - t0) * 1000)

    def transcribe(self, audio: np.ndarray, verbose: bool = True) -> dict:
        t = time.perf_counter()
        r = self.tr.transcribe(audio) or {}
        return {"text": (r.get("text") or "").strip(), "ms": (time.perf_counter() - t) * 1000, "confidence": r.get("confidence")}

    def footprint_mb(self) -> float | None:
        return footprint_mb(self.tr._proc.pid)

    def close(self) -> None:
        self.tr.close()


# ---------------------------------------------------------------------------------- scoring
def intent_of(text: str) -> str:
    from app.voice.intents import IntentClassifier

    return str(IntentClassifier().classify(text).name) if text.strip() else "(nothing)"


def spoken(text: str) -> bool:
    return bool(re.sub(r"[^a-z0-9]", "", text.lower()))


def score(rows: list[dict], kind: str) -> dict:
    if kind == "false_activation":
        out = {"clips": len(rows), "transcribed_something": sum(spoken(r["hyp"]) for r in rows)}
        for src in sorted({r["src"] for r in rows}):
            sub = [r for r in rows if r["src"] == src]
            out[src] = f"{sum(spoken(r['hyp']) for r in sub)}/{len(sub)}"
        out["examples"] = sorted({r["hyp"] for r in rows if spoken(r["hyp"])})[:8]
        return out
    out = {"n": len(rows), "wer_mean": round(float(np.mean([r["wer"] for r in rows])), 4),
           "wer_pooled": round(sum(r["wer"] * max(1, len(bl.norm_words(r["ref"]))) for r in rows) / max(1, sum(max(1, len(bl.norm_words(r["ref"]))) for r in rows)), 4),
           "exact": round(sum(r["wer"] == 0 for r in rows) / len(rows), 3), "ms": bl.summary([r["ms"] for r in rows])}
    cmd = [r for r in rows if r["intent_ref"] not in ("unknown", "(nothing)")]
    if cmd:
        out["intent_match"] = f"{sum(r['intent_ref'] == r['intent_hyp'] for r in cmd)}/{len(cmd)}"
    for src in sorted({r["src"] for r in rows}):
        sub = [r for r in rows if r["src"] == src]
        out[f"wer[{src}]"] = round(float(np.mean([r["wer"] for r in sub])), 4)
    return out


def run_sets(engine, sets: dict, label: str) -> dict:
    engine.transcribe(np.zeros(16000, dtype=np.float32))  # warm-up
    res: dict = {}
    for name, items in sets.items():
        rows = []
        for src, ref, audio in items:
            r = engine.transcribe(audio)
            rows.append({"src": src, "ref": ref, "hyp": r["text"], "ms": round(r["ms"], 1), "audio_s": round(len(audio) / 16000, 2),
                         "wer": round(bl.wer(ref, r["text"]), 4) if ref else None, "confidence": r.get("confidence"),
                         "intent_ref": intent_of(ref) if ref else None, "intent_hyp": intent_of(r["text"])})
        res[name] = {"summary": score(rows, name), "rows": rows}
        s = res[name]["summary"]
        print(f"  {label:34s} {name:16s} " + (f"WER {s['wer_mean']:.3f} pooled {s['wer_pooled']:.3f} exact {s['exact']:.0%} "
              f"p50 {s['ms']['p50']} ms intent {s.get('intent_match', '-')}" if name != "false_activation" else f"transcribed {s['transcribed_something']}/{s['clips']}"))
    return res


def bench_asr(args) -> dict:
    with tempfile.TemporaryDirectory() as td:
        sets = build_sets(Path(td), args.limit)
    print({k: len(v) for k, v in sets.items()})
    out: dict = {"sets": {k: len(v) for k, v in sets.items()}, "engines": {}}

    print("whisper.cpp base.en (app worker, Metal)")
    t0 = time.perf_counter()
    w = WhisperEngine()
    first = w.transcribe(sets["core"][0][2])
    out["engines"]["whisper.cpp"] = {"start_ms": w.start_ms, "first_ms": round(first["ms"], 1), **run_sets(w, sets, "whisper.cpp"),
                                     "footprint_mb": w.footprint_mb(), "wall_s": round(time.perf_counter() - t0, 1)}
    w.close()

    for name, gguf in ASR_GGUF.items():
        if not gguf.exists():  # evaluation-only files can be removed (docs/NVIDIA_EVALUATION.md, section 10)
            print(f"{name}: not downloaded, skipped ({gguf.relative_to(MODELS)}; nemo-speech pull <model>)")
            continue
        print(f"{name} (NeMo-Speech.cpp 0.3.0, Metal, q8_0)")
        t0 = time.perf_counter()
        s = NemoServer(gguf)
        first = s.transcribe(sets["core"][0][2])
        res = {"gguf": gguf.name, "start_ms": s.start_ms, "first_ms": round(first["ms"], 1), **run_sets(s, sets, name)}
        if name == "nemotron-3.5" and not args.no_stream:
            rows = []
            for src, ref, audio in sets["core"] + sets["conversational"]:
                r = asyncio.run(s.stream(audio))
                rows.append({"src": src, "ref": ref, "hyp": r["text"], "wer": round(bl.wer(ref, r["text"]), 4), "first_partial_ms": r["first_partial_ms"],
                             "final_after_end_ms": r["final_after_end_ms"], "partials": r["partials"], "audio_s": round(len(audio) / 16000, 2), "error": r["error"]})
            ok = [r for r in rows if r["final_after_end_ms"] is not None]
            res["streaming"] = {"n": len(rows), "errors": len(rows) - len(ok), "wer_mean": round(float(np.mean([r["wer"] for r in ok])), 4) if ok else None,
                                "first_partial_ms": bl.summary([r["first_partial_ms"] for r in ok if r["first_partial_ms"] is not None]) if ok else None,
                                "final_after_end_ms": bl.summary([r["final_after_end_ms"] for r in ok]) if ok else None, "rows": rows}
            st = res["streaming"]
            print(f"  {name:34s} streaming        WER {st['wer_mean']} first partial {st['first_partial_ms']} final after end {st['final_after_end_ms']} errors {st['errors']}")
        res["footprint_mb"] = s.footprint_mb()
        res["wall_s"] = round(time.perf_counter() - t0, 1)
        out["engines"][name] = res
        s.close()
    return out


# ---------------------------------------------------------------------------------- validation
# Written after the combination rule (app/voice/asr_combine.py) was designed on the sets above,
# and never used to change it: new phrases, voices, talkers, noise seeds and noise levels.
VALID_PHRASES = [
    "Lock me in for fifteen minutes.", "Hey, can I get my phone back for a second?", "Pause the timer, I'll be right back.", "Okay, unpause.",
    "How many minutes are left?", "Stop!", "Emergency stop!", "End the session, I'm done.", "Tone it down a little.", "Roast me harder.",
    "Turn off the camera.", "Turn the mic off.", "I'm studying for my chemistry midterm.", "Ugh, I keep getting distracted by notifications.",
    "Can you remind me what I said my goal was?", "Honestly I'm kind of proud of myself right now.", "So, uh, I'm thinking of switching to math.",
    "Come back to me.", "Drive back to your spot.", "Nope.",
]
VALID_VOICES = ["Reed (English (UK))", "Karen", "Grandma (English (US))"]
VALID_BABBLE = (["Eddy (English (UK))", "Flo (English (UK))", "Grandma (English (UK))"],
                ["We should probably order food before the place closes tonight.", "My sister is flying in on Thursday for the long weekend.",
                 "The printer on the third floor has been broken since Monday."])


def build_validation(tmp: Path) -> dict[str, list]:
    noise = Noise(tmp, seed=29, talkers=VALID_BABBLE)
    sets = {"clean": [(f"say:{v}", t, bl.say_16k(t, v, tmp)) for v in VALID_VOICES for t in VALID_PHRASES]}
    pad = np.zeros(4800, dtype=np.float32)
    clean = [(t, at_level(bl.say_16k(t, "Karen", tmp), -26)) for t in VALID_PHRASES]
    sets["noise"] = []
    for kind in ("white", "babble", "keyboard", "hum", "tones"):
        for snr in (15, 5):
            for text, sp in clean:
                x = np.concatenate([pad, sp, pad])
                sets["noise"].append((f"{kind}@{snr}dB", text, (x + at_level(noise(kind, len(x)), -26 - snr)).astype(np.float32)))
    sets["false_activation"] = []
    for kind, db in (("silence", -65), ("white", -45), ("keyboard", -30), ("hum", -30), ("tones", -30), ("babble", -40)):
        src = "white" if kind == "silence" else kind
        sets["false_activation"] += [(f"{kind}@{db}dBFS", "", at_level(noise(src, 40000), db)) for _ in range(6)]
    return sets


def vision_and_llm_load(stop: dict):
    """The camera pipeline's per-frame work at 15 fps (synthetic frames) and Qwen3-4B writing
    replies non-stop, as during a session. Returns a function that stops both and reports."""
    import threading

    import cv2

    sys.path.insert(0, str(BACKEND / "tests"))
    from fake_mjpeg import test_card

    from app.intelligence.local_llm import LocalLLM
    from app.intelligence.prompting import SAMPLE_CONTEXT
    from app.vision.hands import FaceObserver, HandTracker

    jpegs = [test_card(i) for i in range(30)]
    tracker, face = HandTracker(MODELS), FaceObserver(MODELS)
    frames: list[float] = []

    def camera():
        i = 0
        while not stop["stop"]:
            t0 = time.perf_counter()
            frame = cv2.imdecode(np.frombuffer(jpegs[i % len(jpegs)], np.uint8), cv2.IMREAD_COLOR)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            tracker.detect(rgb, 1000 + i * 66)
            if i % 3 == 0:
                face.observe(rgb, 1000 + i * 66)
            frames.append(time.perf_counter())
            i += 1
            time.sleep(max(0.0, 1 / 15 - (time.perf_counter() - t0)))

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    llm = LocalLLM("Qwen3-4B-Instruct-2507-4bit", MODELS)
    while llm.state == "loading":
        time.sleep(0.2)
    gens: list = []
    ctx = SAMPLE_CONTEXT.replace("{utterance}", "Why do you keep doing that?")

    async def writer():
        while not stop["stop"]:
            gens.append(await llm.generate(ctx, max_tokens=80, timeout_s=30))

    cam = threading.Thread(target=camera, daemon=True)
    cam.start()
    fut = asyncio.run_coroutine_threadsafe(writer(), loop)
    t_start = time.perf_counter()
    time.sleep(1.5)

    def finish() -> dict:
        stop["stop"] = True
        fut.result(timeout=60)
        cam.join(5)
        span = time.perf_counter() - t_start
        ok = [g for g in gens if g.ok]
        llm.close()
        loop.call_soon_threadsafe(loop.stop)
        tracker.close(), face.close()
        return {"vision_fps": round(len(frames) / span, 1), "llm_replies": len(ok), "llm_gen_tps": bl.summary([g.gen_tps or 0 for g in ok]) if ok else None}

    return finish


def bench_asr_validate(args) -> dict:
    from app.voice import nemo_asr
    from app.voice.asr_combine import combine
    from app.voice.intents import MOVES_ROBOT, IntentName
    from app.voice.transcription import CombinedTranscriber, WhisperTranscriber

    with tempfile.TemporaryDirectory() as td:
        sets = build_validation(Path(td))
    print({k: len(v) for k, v in sets.items()})
    whisper = WhisperTranscriber("base.en", "int8", MODELS, engine="whisper.cpp")
    pair = CombinedTranscriber(whisper, nemo_asr.NemoSpeechASR(MODELS))
    while pair.whisper.state == "loading" or pair.parakeet.state == "loading":
        time.sleep(0.05)
    assert pair.whisper.state == "ready" and pair.parakeet.state == "ready", (pair.whisper.error, pair.parakeet.error)
    warm = np.zeros(16000, dtype=np.float32)
    pair.transcribe(warm)
    stop = {"stop": False}
    finish = vision_and_llm_load(stop) if args.load else None
    out: dict = {"sets": {k: len(v) for k, v in sets.items()}, "load": bool(args.load), "results": {}}
    for name, items in sets.items():
        rows = []
        for src, ref, audio in items:
            r = pair.transcribe(audio)
            t = time.perf_counter()
            alone = whisper.transcribe(audio)  # the same clip through whisper.cpp alone, as today
            alone_ms = (time.perf_counter() - t) * 1000
            rd = r["readings"]
            rows.append({"src": src, "ref": ref, "whisper": rd["whisper"]["text"], "whisper_conf": rd["whisper"]["confidence"], "parakeet": rd["parakeet"]["text"],
                         "combined": r["text"], "combined_conf": r["confidence"], "decided_by": r["decided_by"], "combined_ms": r["roundtrip_ms"],
                         "whisper_alone_ms": round(alone_ms, 1), "whisper_alone_text": (alone or {}).get("text", "")})
        res = {}
        for variant in ("whisper", "parakeet", "combined"):
            hyps = [row[variant] for row in rows]
            if name == "false_activation":
                said = [spoken(h) and not row["src"].startswith("babble") for h, row in zip(hyps, rows, strict=True)]
                res[variant] = {"phantom_non_speech": f"{sum(said)}/{sum(not r['src'].startswith('babble') for r in rows)}",
                                "distant_talk_transcribed": f"{sum(spoken(h) for h, r in zip(hyps, rows, strict=True) if r['src'].startswith('babble'))}/{sum(r['src'].startswith('babble') for r in rows)}"}
                continue
            ref_int = [intent_of(row["ref"]) for row in rows]
            hyp_int = [intent_of(h) for h in hyps]
            cmd = [i for i, x in enumerate(ref_int) if x not in ("unknown", "(nothing)")]
            stops = [i for i, x in enumerate(ref_int) if x in (str(IntentName.STOP_MOTION), str(IntentName.EMERGENCY_STOP))]
            res[variant] = {"wer_mean": round(float(np.mean([bl.wer(row["ref"], h) for row, h in zip(rows, hyps, strict=True)])), 4),
                            "intent_match": f"{sum(ref_int[i] == hyp_int[i] for i in cmd)}/{len(cmd)}",
                            "stops_heard": f"{sum(hyp_int[i] in (str(IntentName.STOP_MOTION), str(IntentName.EMERGENCY_STOP)) for i in stops)}/{len(stops)}",
                            "false_motion": sum(h == str(n) and ref_int[i] != str(n) for i, h in enumerate(hyp_int) for n in MOVES_ROBOT)}
        # A motion command only acts at confidence >= 0.8 (runtime gate): how many would have moved the robot?
        motion_acting = sum(1 for row in rows if intent_of(row["combined"]) in {str(n) for n in MOVES_ROBOT} and (row["combined_conf"] or 0) >= 0.8)
        res["combined_motion_commands_passing_gate"] = motion_acting
        res["latency_ms"] = {"combined": bl.summary([row["combined_ms"] for row in rows]), "whisper_alone": bl.summary([row["whisper_alone_ms"] for row in rows])}
        res["decided_by"] = dict(Counter(row["decided_by"] for row in rows))
        out["results"][name] = {"summary": res, "rows": rows}
        print(f"  {name:16s} " + " | ".join(f"{v}: {res[v]}" for v in ("whisper", "parakeet", "combined")))
        print(f"  {'':16s} latency combined {res['latency_ms']['combined']} vs whisper alone {res['latency_ms']['whisper_alone']}")
    if finish is not None:
        out["load_report"] = finish()
        print(f"  load: {out['load_report']}")
    out["memory_mb"] = {"whisper_worker": footprint_mb(whisper._proc.pid), "parakeet_server": footprint_mb(pair.parakeet.pid())}
    pair.close()
    assert combine("", 0.0, "") == ("", 0.0, "no speech (Parakeet heard nothing)")
    return out


# ---------------------------------------------------------------------------------- TTS
TTS_LINES = bl.TTS_LINES + [
    "Okay. Thirty minutes on the stats exam. Phone's mine.",
    "Paused. Go get your snack, I'll be right here.",
    "Nope, not moving. My wheels are locked for safety.",
    "You've been at it for twenty-two minutes and touched the phone once. Not bad.",
]


def magpie_assets() -> dict[str, Path]:
    def one(pattern: str) -> Path:
        found = sorted(NEMO_MODELS.glob(pattern))
        if not found:
            raise FileNotFoundError(f"{pattern} under {NEMO_MODELS} (nemo-speech pull magpie)")
        return found[-1]

    return {"tts": one("magpie_tts_multilingual_357m/*/*.gguf"), "codec": one("nemo-nano-codec-*/*/*.gguf"),
            "tokenizer": one("magpie_tts_multilingual_357m/*/tokenizer")}


def resample(audio: np.ndarray, sr: int, to: int = 16000) -> np.ndarray:
    return np.interp(np.arange(0, len(audio), sr / to), np.arange(len(audio)), audio).astype(np.float32) if sr != to else audio.astype(np.float32)


def read_wav_bytes(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as w:
        sr, n = w.getframerate(), w.getnframes()
        return np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768, sr


def bench_tts(args) -> dict:
    """Kokoro int8 (current local voice, CPU, in-process) against MagpieTTS (NeMo-Speech.cpp,
    Metal): time to the whole line and to first audio, memory, and intelligibility as heard by
    an independent recogniser (whisper.cpp). Optionally with the language model writing
    non-stop on the same GPU (--load). Samples go to data/voice_samples/ for a person to judge;
    nothing is played."""
    from kokoro_onnx import Kokoro
    from pywhispercpp.model import Model

    from app.voice.stt_worker import INITIAL_PROMPT

    asr = Model(str(MODELS / "whispercpp" / "ggml-base.en.bin"), redirect_whispercpp_logs_to=None, n_threads=4, print_progress=False, print_realtime=False)

    def heard(audio16: np.ndarray) -> str:
        return " ".join(s.text.strip() for s in asr.transcribe(audio16, language="en", no_context=True, single_segment=True, initial_prompt=INITIAL_PROMPT)).strip()

    samples = ROOT / "data" / "voice_samples"
    samples.mkdir(parents=True, exist_ok=True)
    out: dict = {"lines": TTS_LINES, "engines": {}, "load": bool(args.load)}
    llm, stop_load, load_task, loop = None, None, None, None
    if args.load:
        import threading

        from app.intelligence.local_llm import LocalLLM
        from app.intelligence.prompting import SAMPLE_CONTEXT

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        llm = LocalLLM("Qwen3-4B-Instruct-2507-4bit", MODELS)
        while llm.state == "loading":
            time.sleep(0.2)
        stop_load = {"stop": False, "gens": []}
        ctx = SAMPLE_CONTEXT.replace("{utterance}", "Why do you keep doing that?")

        async def keep_busy():
            while not stop_load["stop"]:
                stop_load["gens"].append(await llm.generate(ctx, max_tokens=80, timeout_s=30))

        load_task = asyncio.run_coroutine_threadsafe(keep_busy(), loop)
        time.sleep(1.0)

    def measure(label: str, synth, voice_tag: str) -> dict:
        synth(TTS_LINES[0])  # warm-up
        rows = []
        for i, text in enumerate(TTS_LINES):
            t = time.perf_counter()
            audio, sr = synth(text)
            ms = (time.perf_counter() - t) * 1000
            hyp = heard(resample(audio, sr))
            rows.append({"text": text, "ms": round(ms, 1), "audio_s": round(len(audio) / sr, 2), "rtf": round(ms / 1000 / max(1e-6, len(audio) / sr), 3),
                         "heard": hyp, "wer": round(bl.wer(text, hyp), 4)})
            if not args.load and i < 4:
                with wave.open(str(samples / f"{voice_tag}-{i + 1}.wav"), "wb") as w:
                    w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
                    w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
                (samples / f"{voice_tag}-{i + 1}.txt").write_text(text)
        res = {"line_ms": bl.summary([r["ms"] for r in rows]), "rtf": bl.summary([r["rtf"] for r in rows]),
               "intelligibility_wer": round(float(np.mean([r["wer"] for r in rows])), 4), "rows": rows}
        print(f"  {label:44s} line p50 {res['line_ms']['p50']} ms (RTF {res['rtf']['p50']}), heard-back WER {res['intelligibility_wer']}")
        return res

    kok = Kokoro(str(MODELS / "kokoro" / "kokoro-v1.0.int8.onnx"), str(MODELS / "kokoro" / "voices-v1.0.bin"))
    out["engines"]["kokoro-int8 af_bella"] = measure("Kokoro int8 af_bella (CPU, in-process)", lambda t: kok.create(t, voice="af_bella", speed=1.0),
                                                     "kokoro-af_bella-m55")
    try:
        assets = magpie_assets()
    except FileNotFoundError as exc:  # evaluation-only files can be removed (docs/NVIDIA_EVALUATION.md, section 10)
        print(f"  MagpieTTS skipped: {exc}")
        assets = None
    if assets is not None:
        s = NemoServer(None, ["--tts-model", str(assets["tts"]), "--codec-model", str(assets["codec"]), "--tokenizer-dir", str(assets["tokenizer"])])
        models = s.http.get(f"{s.base}/v1/models").json()
        voices = sorted({v for m in models.get("data", []) for v in (m.get("voices") or [])})
        out["magpie"] = {"gguf": assets["tts"].name, "codec": assets["codec"].name, "start_ms": s.start_ms, "voices": voices}
        print(f"  magpie voices: {voices}")

        def magpie(text: str, voice: str):
            r = s.http.post(f"{s.base}/v1/audio/speech", json={"input": text, "voice": voice, "response_format": "wav", "language": "en"})
            r.raise_for_status()
            return read_wav_bytes(r.content)

        for voice in (args.voice or voices[:2] or ["default"]):
            out["engines"][f"magpie {voice}"] = measure(f"MagpieTTS {voice} (Metal, NeMo-Speech HTTP)", lambda t, v=voice: magpie(t, v), f"magpie-{voice.lower()}-m55")
        out["magpie"]["footprint_mb"] = s.footprint_mb()
        s.close()
        if not args.load:
            # Streaming first audio (the engine's own client-side measurement; HTTP buffers whole lines).
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
                f.write("\n".join(TTS_LINES))
            cmd = ["sandbox-exec", "-p", SANDBOX, str(NEMO_BIN), "bench", "tts", "--text-file", f.name, "--magpie-model", str(assets["tts"]),
                   "--codec-model", str(assets["codec"]), "--tokenizer-dir", str(assets["tokenizer"]), "--per-stream", "3", "--device", "metal", "--json"]
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env={**os.environ, "NEMO_SPEECH_MODEL_DIR": str(NEMO / "models")})
            try:
                rep = json.loads(run.stdout[run.stdout.index("{"):])
                r0 = rep["runs"][0]
                out["magpie"]["streaming_bench"] = {"load_ms": rep.get("load_ms"), "latency_ms": r0.get("latency_ms"), "rtfx": r0.get("rtfx"),
                                                    "first_audio_ms": (r0.get("metrics") or {}).get("first_audio_ms"), "chunk_gap_ms": (r0.get("metrics") or {}).get("chunk_gap_ms")}
            except Exception as exc:
                out["magpie"]["streaming_bench"] = {"error": f"{type(exc).__name__}: {exc}", "stderr": run.stderr[-600:]}
            print(f"  magpie streaming bench: {out['magpie']['streaming_bench']}")
    if llm is not None:
        stop_load["stop"] = True
        load_task.result(timeout=60)
        gens = [g for g in stop_load["gens"] if g.ok]
        out["llm_under_tts"] = {"gens": len(gens), "gen_tps": bl.summary([g.gen_tps or 0 for g in gens]) if gens else None}
        llm.close()
        loop.call_soon_threadsafe(loop.stop)
    return out


def save(kind: str, data: dict) -> Path:
    bl.OUT.mkdir(parents=True, exist_ok=True)
    path = bl.OUT / f"m55-{kind}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"machine": bl.machine(), "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **data}, indent=1))
    print(f"\nsaved {path.relative_to(ROOT)}")
    return path


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("asr")
    pa.add_argument("--limit", type=int, default=None, help="first N items of each set (smoke test)")
    pa.add_argument("--no-stream", action="store_true")
    pv = sub.add_parser("asr-validate")
    pv.add_argument("--load", action="store_true", help="with the vision pipeline and Qwen3-4B running throughout")
    pt = sub.add_parser("tts")
    pt.add_argument("--voice", action="append", default=None, help="Magpie voice name (default: the first two)")
    pt.add_argument("--load", action="store_true", help="with Qwen3-4B writing replies non-stop on the same GPU")
    args = p.parse_args()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    if args.cmd == "asr":
        save("asr" if not args.limit else "asr-smoke", bench_asr(args))
    elif args.cmd == "asr-validate":
        save("asr-validate-load" if args.load else "asr-validate", bench_asr_validate(args))
    elif args.cmd == "tts":
        save("tts-load" if args.load else "tts", bench_tts(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
