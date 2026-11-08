"""Pluggable speech-to-text in a worker process. Default: whisper.cpp (Metal); fallback:
faster-whisper (CPU). Same Whisper base.en weights family, same prompt (stt_worker.py).
"parakeet+whisper" adds NVIDIA Parakeet TDT 0.6B v3 (nemo_asr.py) on the same audio and
combines the two readings (asr_combine.py, docs/NVIDIA_EVALUATION.md)."""

from __future__ import annotations

import importlib.util
import logging
import multiprocessing as mp
import re
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Whisper's well-known outputs for silence/noise. Never act on these.
HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thank you for watching", "thanks for watching", "bye", "okay",
    "subtitles by the amaraorg community", "please subscribe", "so", "uh", "um", "hmm",
}


def is_hallucination(text: str) -> bool:
    t = re.sub(r"[^a-z ]", "", text.lower()).strip()
    return t in HALLUCINATIONS or len(t) < 2


class Transcriber:
    engine = "none"
    state = "unavailable"
    error: str | None = None

    def transcribe(self, audio: np.ndarray, timeout_s: float = 10.0) -> dict | None:
        return None

    def close(self) -> None:
        pass

    def describe(self) -> dict:
        return {"engine": self.engine, "state": self.state, "error": self.error}


class WhisperTranscriber(Transcriber):
    engine = "faster-whisper"

    def __init__(self, model: str = "base.en", compute_type: str = "int8", models_dir: Path | None = None, engine: str = "faster-whisper", use_gpu: bool = True):
        self.model = model
        self.compute_type = compute_type
        self.engine = engine
        self.use_gpu = use_gpu
        if engine == "whisper.cpp":
            ggml = models_dir / "whispercpp" / f"ggml-{model}.bin" if models_dir else None
            self.model_dir = str(ggml) if ggml and ggml.exists() else None
        else:
            local = models_dir / "whisper" / model if models_dir else None
            self.model_dir = str(local) if local and (local / "model.bin").exists() else None
        self.state = "loading"
        self.error = None
        self.load_ms: float | None = None
        self.last_latency_ms: float | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        ctx = mp.get_context("spawn")
        self._conn, child = ctx.Pipe()
        from .stt_worker import worker_main

        self._proc = ctx.Process(target=worker_main, args=(child, model, compute_type, self.model_dir, engine, use_gpu), daemon=True, name="stt-worker")
        self._proc.start()
        threading.Thread(target=self._await_ready, daemon=True, name="stt-ready").start()

    def _await_ready(self) -> None:
        try:
            if self._conn.poll(180):
                msg = self._conn.recv()
                if msg.get("op") == "ready":
                    self.state = "ready"
                    self.load_ms = msg.get("load_ms")
                    log.info("Whisper %s ready in %s ms", self.model, self.load_ms)
                    return
                self.state, self.error = "error", msg.get("error", "unknown error")
            else:
                self.state, self.error = "error", "timed out loading the Whisper model (is it downloaded?)"
        except Exception as exc:
            self.state, self.error = "error", str(exc)
        log.warning("STT unavailable: %s", self.error)

    def transcribe(self, audio: np.ndarray, timeout_s: float = 10.0) -> dict | None:
        if self.state != "ready":
            return None
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            t0 = time.perf_counter()
            self._conn.send({"op": "transcribe", "id": rid, "pcm": pcm, "sr": 16000})
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if not self._conn.poll(max(0.0, deadline - time.monotonic())):
                    break
                msg = self._conn.recv()
                if msg.get("id") != rid:
                    continue
                if msg.get("op") == "error":
                    log.warning("transcription failed: %s", msg.get("error"))
                    return None
                msg["roundtrip_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                self.last_latency_ms = msg["roundtrip_ms"]
                return msg
        log.warning("transcription timed out after %.1fs", timeout_s)
        return None

    def close(self) -> None:
        try:
            self._conn.send({"op": "stop"})
        except Exception:
            pass
        self._proc.join(timeout=2)
        if self._proc.is_alive():
            self._proc.terminate()

    def describe(self) -> dict:
        return {
            "engine": self.engine,
            "model": self.model,
            "state": self.state,
            "error": self.error,
            "load_ms": self.load_ms,
            "last_latency_ms": self.last_latency_ms,
            "local_model": self.model_dir is not None,
            "gpu": self.use_gpu if self.engine == "whisper.cpp" else False,
        }


class CombinedTranscriber(Transcriber):
    """whisper.cpp (its worker process) and Parakeet (nemo-speech serve) transcribe the same
    audio at the same time; asr_combine.combine() decides. If Parakeet is not ready the result
    is whisper.cpp's alone, exactly as with stt_engine "whisper.cpp".

    Neither engine can hold the other back (Milestone 5.6 audit): each has its own thread, a
    stop heard by whichever finishes first is returned at once, the second engine gets at most
    ``grace_s`` after the first, and an engine still busy with an earlier clip is skipped."""

    engine = "parakeet+whisper"

    def __init__(self, whisper: Transcriber, parakeet, grace_s: float = 1.0):
        self.whisper, self.parakeet = whisper, parakeet
        self.grace_s = grace_s
        self.error = None
        self.last_latency_ms: float | None = None
        self.last: dict | None = None
        self.decisions: Counter = Counter()
        self._pools = {name: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"stt-{name}") for name in ("whisper", "parakeet")}
        self._busy: dict[str, Future | None] = {"whisper": None, "parakeet": None}

    @property
    def state(self) -> str:
        if self.whisper.state == "ready" or self.parakeet.state == "ready":
            return "ready"
        return "loading" if "loading" in (self.whisper.state, self.parakeet.state) else "error"

    def _submit(self, name: str, engine, audio, timeout_s: float) -> Future | None:
        prev = self._busy[name]
        if engine.state != "ready" or (prev is not None and not prev.done()):
            return None
        self._busy[name] = f = self._pools[name].submit(engine.transcribe, audio, timeout_s)
        return f

    def transcribe(self, audio: np.ndarray, timeout_s: float = 10.0) -> dict | None:
        from .asr_combine import PARAKEET_ONLY_CONFIDENCE, combine, is_stop

        t0 = time.perf_counter()
        fw = self._submit("whisper", self.whisper, audio, timeout_s)
        fp = self._submit("parakeet", self.parakeet, audio, timeout_s)
        w = p = None
        pending = {f for f in (fw, fp) if f is not None}
        first_at = None
        decided = None
        while pending:
            left = timeout_s - (time.perf_counter() - t0) if first_at is None else self.grace_s - (time.perf_counter() - first_at)
            done, pending = wait(pending, timeout=max(0.0, left), return_when=FIRST_COMPLETED)
            if not done:
                break  # the slower engine missed its grace: decide with what arrived
            for f in done:
                try:
                    r = f.result()
                except Exception as exc:  # a crashed engine counts as no reading
                    log.warning("speech engine failed: %s", exc)
                    r = None
                if f is fw:
                    w = r
                else:
                    p = r
                if r is not None and is_stop(r.get("text")):
                    decided = f"stop heard by {'whisper.cpp' if f is fw else 'Parakeet'} (not waiting for the other)"
                    pending = set()
                    break
            first_at = first_at or time.perf_counter()
        late = [name for name, f in (("whisper.cpp", fw), ("Parakeet", fp)) if f is not None and not f.done()]
        if decided is not None:
            src = w if decided.startswith("stop heard by whisper") else p
            conf = (w or {}).get("confidence", 0.0) if src is w else PARAKEET_ONLY_CONFIDENCE
            out = {"text": src["text"], "confidence": conf, "audio_s": (w or {}).get("audio_s")}
        elif p is None and w is None:
            return None
        elif p is None:
            why = "too slow" if "Parakeet" in late else ("busy" if fp is None and self.parakeet.state == "ready" else self.parakeet.state)
            out, decided = dict(w), f"whisper.cpp only (Parakeet {why})"
        elif w is None:
            out = {"text": p["text"], "confidence": PARAKEET_ONLY_CONFIDENCE if p["text"] else 0.0}
            why = "too slow" if "whisper.cpp" in late else ("busy" if fw is None and self.whisper.state == "ready" else self.whisper.state)
            decided = f"Parakeet only (whisper.cpp {why})"
        else:
            text, conf, decided = combine(w.get("text"), w.get("confidence"), p["text"])
            out = {"text": text, "confidence": conf, "audio_s": w.get("audio_s")}
        out["roundtrip_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["engine"], out["decided_by"] = self.engine, decided
        out["readings"] = {"whisper": None if w is None else {"text": w.get("text"), "confidence": w.get("confidence"), "ms": w.get("roundtrip_ms")},
                           "parakeet": None if p is None else {"text": p["text"], "ms": p["roundtrip_ms"]}}
        self.last_latency_ms, self.last = out["roundtrip_ms"], {"decided_by": decided, **out["readings"]}
        self.decisions[decided] += 1
        return out

    def close(self) -> None:
        self.whisper.close()
        self.parakeet.close()
        for pool in self._pools.values():
            pool.shutdown(wait=False)

    def describe(self) -> dict:
        return {"engine": self.engine, "state": self.state, "error": self.error, "last_latency_ms": self.last_latency_ms, "last": self.last,
                "decisions": dict(self.decisions), "whisper": self.whisper.describe(), "parakeet": self.parakeet.describe()}


def make_transcriber(settings) -> Transcriber:
    if settings.stt_engine == "none":
        t = Transcriber()
        t.error = "speech-to-text disabled (LOCKEDIN_STT_ENGINE=none); typed commands still work"
        return t
    if settings.stt_engine in ("whisper.cpp", "parakeet+whisper"):
        ggml = settings.models_dir / "whispercpp" / f"ggml-{settings.whisper_model}.bin"
        if importlib.util.find_spec("pywhispercpp") is not None and ggml.exists():
            whisper = WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.models_dir, engine="whisper.cpp",
                                         use_gpu=settings.whisper_cpp_gpu)
            if settings.stt_engine == "parakeet+whisper":
                from . import nemo_asr

                if nemo_asr.available(settings.models_dir):
                    return CombinedTranscriber(whisper, nemo_asr.NemoSpeechASR(settings.models_dir))
                log.warning("Parakeet unavailable (NeMo-Speech.cpp or its GGUF not downloaded); using whisper.cpp alone")
            return whisper
        log.warning("whisper.cpp unavailable (%s); using faster-whisper", "no weights at " + str(ggml) if not ggml.exists() else "pywhispercpp not installed")
    # find_spec, not import: importing here would load PyAV into this process (see stt_worker.py).
    if importlib.util.find_spec("faster_whisper") is None:
        t = Transcriber()
        t.error = "faster-whisper not installed; install with `uv sync --extra voice`"
        return t
    return WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.models_dir)
