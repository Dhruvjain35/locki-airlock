"""NVIDIA Parakeet TDT 0.6B v3 through NeMo-Speech.cpp (NVIDIA's ggml runtime, Metal), as a
local HTTP service in its own process (docs/NVIDIA_EVALUATION.md).

``nemo-speech serve`` binds to 127.0.0.1 on a free port, loads the pinned local GGUF (a local
path never touches the network; the runtime's model index is not used) and runs inside the
macOS sandbox with every outbound connection except localhost blocked. One request at a time.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

PARAKEET = ("nvidia/parakeet-tdt-0.6b-v3", "541d1f99c6b0c3cd0b11a95167540bb8edefd82b", "parakeet-tdt-0.6b-v3.q8_0.gguf")
SANDBOX = (
    '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
    "(allow network-outbound (remote unix-socket))"
    '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
)


def nemo_binary(models_dir: Path) -> Path | None:
    found = sorted((Path(models_dir) / "nemo-speech").glob("runtime-*/bin/nemo-speech"))
    return found[-1] if found else None


def parakeet_gguf(models_dir: Path) -> Path:
    repo, rev, name = PARAKEET
    return Path(models_dir) / "nemo-speech" / "models" / repo / rev / name


def available(models_dir: Path) -> bool:
    return nemo_binary(models_dir) is not None and parakeet_gguf(models_dir).exists()


def _wav(audio: np.ndarray, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class NemoSpeechASR:
    engine = "parakeet-tdt-0.6b-v3 (NeMo-Speech.cpp)"

    def __init__(self, models_dir: Path, device: str = "metal", load_timeout_s: float = 60.0, sandbox: bool = True):
        """``sandbox=False`` only when this process is already inside the macOS sandbox (sandboxes
        cannot be nested); the server then inherits it."""
        self.models_dir = Path(models_dir)
        self.binary, self.gguf = nemo_binary(self.models_dir), parakeet_gguf(self.models_dir)
        self.state, self.error = "loading", None
        self.load_ms: float | None = None
        self.last_latency_ms: float | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._http = None
        if self.binary is None or not self.gguf.exists():
            self.state = "unavailable"
            self.error = "NeMo-Speech.cpp or the Parakeet GGUF is not downloaded (make models; scripts/download_models.py --nvidia)"
            return
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        cmd = [str(self.binary), "serve", "--host", "127.0.0.1", "--port", str(self.port), "--device", device, "--quiet", "--asr-model", str(self.gguf)]
        if sandbox and shutil.which("sandbox-exec"):
            cmd = ["sandbox-exec", "-p", SANDBOX, *cmd]
        self._log = tempfile.NamedTemporaryFile("w+", prefix="nemo-speech-", suffix=".log", delete=False)
        env = {**os.environ, "NEMO_SPEECH_MODEL_DIR": str(self.models_dir / "nemo-speech" / "models")}
        self._t0 = time.monotonic()
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self._log, env=env)
        threading.Thread(target=self._await_ready, args=(load_timeout_s,), daemon=True, name="nemo-asr-ready").start()

    def _await_ready(self, timeout_s: float) -> None:
        import httpx

        self._http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10.0)
        while time.monotonic() - self._t0 < timeout_s:
            if self._proc.poll() is not None:
                self.state, self.error = "error", f"nemo-speech exited: {Path(self._log.name).read_text()[-300:]}"
                log.warning("Parakeet unavailable: %s", self.error)
                return
            try:
                if self._http.get("/v1/models").status_code == 200:
                    self.load_ms = round((time.monotonic() - self._t0) * 1000)
                    self.state = "ready"
                    log.info("Parakeet (NeMo-Speech.cpp) ready in %s ms", self.load_ms)
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.state, self.error = "error", "timed out starting nemo-speech"

    def transcribe(self, audio: np.ndarray, timeout_s: float = 10.0) -> dict | None:
        if self.state != "ready":
            return None
        if self._proc is not None and self._proc.poll() is not None:
            self.state, self.error = "error", f"nemo-speech exited (code {self._proc.returncode})"
            log.warning("Parakeet unavailable: %s", self.error)
            return None
        with self._lock:
            t0 = time.perf_counter()
            try:
                r = self._http.post("/v1/audio/transcriptions", files={"file": ("u.wav", _wav(audio), "audio/wav")}, timeout=timeout_s)
                r.raise_for_status()
                text = (r.json().get("text") or "").strip()
            except Exception as exc:
                log.warning("Parakeet transcription failed: %s", exc)
                return None
            self.last_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            return {"text": text, "roundtrip_ms": self.last_latency_ms}

    def pid(self) -> int | None:
        if self._proc is None:
            return None
        try:
            import psutil

            kids = psutil.Process(self._proc.pid).children(recursive=True)
            return kids[0].pid if kids else self._proc.pid
        except Exception:
            return self._proc.pid

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def describe(self) -> dict:
        return {"engine": self.engine, "state": self.state, "error": self.error, "load_ms": self.load_ms,
                "last_latency_ms": self.last_latency_ms, "model": f"{PARAKEET[0]}@{PARAKEET[1][:8]} q8_0"}
