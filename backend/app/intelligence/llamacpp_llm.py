"""An optional local language model served by llama.cpp (``llama-server``, Metal) instead of
MLX, with the same interface as LocalLLM (local_llm.py). It exists to run GGUF models such
as NVIDIA Nemotron-3-Nano-4B against the same dialogue code (docs/NVIDIA_EVALUATION.md).

The server listens on 127.0.0.1 only, loads a local GGUF file and runs inside the macOS
sandbox with every outbound connection except localhost blocked: nothing is downloaded and
nothing leaves the Mac. Prompts use the model's own chat template (``/apply-template``) with
the same two messages as the MLX worker (persona, context). ``prefill`` sends the chat-
formatted text up to the end of the context prefix with ``n_predict`` 0 and ``cache_prompt``,
so the turn's request only reads what is new, as with the MLX worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .local_llm import SPEC_MAX_AGE_S, Generation
from .prompting import PERSONA

log = logging.getLogger(__name__)

SANDBOX = (
    '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
    "(allow network-outbound (remote unix-socket))"
    '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
)
SENTINEL = "⁣LOCKEDIN-PREFIX-END⁣"
MODELS_DIR = Path(__file__).resolve().parents[2] / "models"


def default_server(models_dir: Path = MODELS_DIR) -> Path | None:
    found = sorted((Path(models_dir) / "llama.cpp").glob("*/llama-server"))
    return found[-1] if found else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServerLLM:
    provider = "llamacpp"

    def __init__(self, gguf: Path, server: Path | None = None, *, ctx_size: int = 8192, chat_kwargs: dict | None = None,
                 extra_args: tuple[str, ...] = (), load_timeout_s: float = 120.0):
        self.gguf = Path(gguf)
        self.model = self.gguf.stem
        self.server = Path(server) if server else default_server()
        self.chat_kwargs = {"enable_thinking": False} if chat_kwargs is None else chat_kwargs
        self.state = "loading"
        self.error: str | None = None
        self.ready_info: dict = {}
        self.calls = self.failures = self.cancelled = 0
        self.last: dict | None = None
        self.latency_ms: list[float] = []
        self.prefills = self.prefill_hits = self.prefill_misses = 0
        self.last_prefill: dict | None = None
        self._lock: asyncio.Lock | None = None
        self._current: tuple[int, bool] | None = None
        self._cancel: set[int] = set()
        self._next_id = 0
        self._spec_text: str | None = None
        self._spec_at = 0.0
        self._prefill_task: asyncio.Task | None = None
        self._proc: subprocess.Popen | None = None
        self._closed = False
        if not self.gguf.exists():
            self.state, self.error = "unavailable", f"model not downloaded: {self.gguf}"
            return
        if self.server is None or not self.server.exists():
            self.state, self.error = "unavailable", "llama-server not found (backend/models/llama.cpp/)"
            return
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        cmd = [str(self.server), "-m", str(self.gguf), "--host", "127.0.0.1", "--port", str(self.port), "-ngl", "999", "-c", str(ctx_size),
               "--parallel", "1", "--jinja", "--no-webui", *extra_args]
        if shutil.which("sandbox-exec"):
            cmd = ["sandbox-exec", "-p", SANDBOX, *cmd]
        self._log = tempfile.NamedTemporaryFile("w+", prefix="llama-server-", suffix=".log", delete=False)
        self._t0 = time.monotonic()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HF_", "LLAMA_ARG_"))}
        self._proc = subprocess.Popen(cmd, stdout=self._log, stderr=subprocess.STDOUT, env=env)
        threading.Thread(target=self._wait_ready, args=(load_timeout_s,), daemon=True, name="llama-server-ready").start()

    # ------------------------------------------------------------------ lifecycle
    def _wait_ready(self, timeout_s: float) -> None:
        import httpx

        while time.monotonic() - self._t0 < timeout_s and not self._closed:
            if self._proc.poll() is not None:
                self.state, self.error = "error", f"llama-server exited: {Path(self._log.name).read_text()[-500:]}"
                return
            try:
                r = httpx.get(f"{self.base}/health", timeout=1.0)
                if r.status_code == 200:
                    props = httpx.get(f"{self.base}/props", timeout=5.0).json()
                    self.ready_info = {"load_ms": round((time.monotonic() - self._t0) * 1000), "runtime": "llama.cpp",
                                       "build": props.get("build_info"), "ctx": (props.get("default_generation_settings") or {}).get("n_ctx"),
                                       "model": self.gguf.name}
                    self.state = "ready"
                    log.info("llama-server %s ready: %s", self.model, self.ready_info)
                    return
            except Exception:
                pass
            time.sleep(0.1)
        if self.state == "loading":
            self.state, self.error = "error", f"timed out loading {self.model}"

    def close(self) -> None:
        self._closed = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    # ------------------------------------------------------------------ prompts
    async def _template(self, client, user: str) -> str:
        r = await client.post(f"{self.base}/apply-template", json={
            "messages": [{"role": "system", "content": PERSONA}, {"role": "user", "content": user}], "chat_template_kwargs": self.chat_kwargs})
        r.raise_for_status()
        return r.json()["prompt"]

    async def _head(self, client, prefix: str) -> str:
        full = await self._template(client, prefix + SENTINEL)
        return full[: full.index(SENTINEL)]

    # ------------------------------------------------------------------ API (as LocalLLM)
    def prefill(self, prefix: str) -> None:
        if self.state != "ready" or not prefix or prefix == self._spec_text:
            return
        if self._current is not None:
            if not self._current[1]:
                return
            self.cancel(self._current[0])
        self._spec_text, self._spec_at = prefix, time.monotonic()
        self.prefills += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prefill_task = loop.create_task(self._do_prefill(prefix))

    async def _do_prefill(self, prefix: str) -> None:
        import httpx

        t = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                head = await self._head(client, prefix)
                r = await client.post(f"{self.base}/completion", json={"prompt": head, "n_predict": 0, "cache_prompt": True, "id_slot": 0})
                r.raise_for_status()
                tm = r.json().get("timings") or {}
            self.last_prefill = {"read": tm.get("prompt_n"), "ms": round((time.monotonic() - t) * 1000, 1)}
        except Exception as exc:
            self.last_prefill = {"error": str(exc)[:200]}

    def speech_recognition(self, busy: bool) -> None:  # llama-server cannot pause between prefill chunks
        return

    def voice_rendering(self, busy: bool) -> None:  # nor between tokens of a streamed reply
        return

    def cancel(self, rid: int | None = None) -> None:
        cur = self._current
        if cur is not None and (rid is None or cur[0] == rid):
            self._cancel.add(cur[0])

    async def generate(self, context: str, *, max_tokens: int = 96, temperature: float = 0.7, top_p: float = 0.9, timeout_s: float = 6.0,
                       on_delta: Callable[[str], None] | None = None, background: bool = False, seed: int | None = None) -> Generation:
        import httpx

        if self.state != "ready":
            return Generation(finish="unavailable", error=self.error or self.state)
        if self._lock is None:
            self._lock = asyncio.Lock()
        if not background and self._current is not None and self._current[1]:
            self.cancel(self._current[0])
        if self._spec_text is not None and time.monotonic() - self._spec_at > SPEC_MAX_AGE_S:
            self._spec_text = None
        if background and (self._lock.locked() or self._spec_text is not None):
            return Generation(finish="cancelled", error="busy" if self._lock.locked() else "the user is talking")
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._current = (rid, background)
            self.calls += 1
            gen = Generation()
            t0 = time.monotonic()
            prefilled = False
            if self._spec_text is not None:
                prefilled = context.startswith(self._spec_text)
                self.prefill_hits += prefilled
                self.prefill_misses += not prefilled
                self._spec_text = None
            if self._prefill_task is not None and not self._prefill_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self._prefill_task), timeout=max(0.1, timeout_s / 2))
                except Exception:
                    pass
            body = {"n_predict": max_tokens, "temperature": temperature, "top_p": top_p, "stream": True, "cache_prompt": True, "id_slot": 0}
            if seed is not None:
                body["seed"] = seed
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=2.0)) as client:
                    body["prompt"] = await self._template(client, context)
                    deadline = t0 + timeout_s
                    async with client.stream("POST", f"{self.base}/completion", json=body) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if rid in self._cancel:
                                gen.finish = "superseded" if not background else "cancelled"
                                break
                            if time.monotonic() > deadline:
                                gen.finish, gen.error = "timeout", f"no complete reply within {timeout_s:.1f} s"
                                break
                            if not line.startswith("data:"):
                                continue
                            ev = json.loads(line[5:].strip())
                            piece = ev.get("content", "")
                            if piece:
                                if gen.first_token_ms is None:
                                    gen.first_token_ms = round((time.monotonic() - t0) * 1000, 1)
                                gen.text += piece
                                if on_delta is not None:
                                    try:
                                        on_delta(piece)
                                    except Exception:
                                        log.exception("on_delta failed")
                            if ev.get("stop"):
                                tm = ev.get("timings") or {}
                                gen.finish = "length" if ev.get("stop_type") == "limit" else "stop"
                                gen.prompt_tokens, gen.gen_tokens = tm.get("prompt_n"), tm.get("predicted_n")
                                gen.prompt_tps, gen.gen_tps = tm.get("prompt_per_second"), tm.get("predicted_per_second")
                                gen.stats = {"prompt_read": tm.get("prompt_n"), "cached": ev.get("tokens_cached"), "prefilled": prefilled,
                                             "server_prompt_ms": tm.get("prompt_ms"), "server_gen_ms": tm.get("predicted_ms")}
                                break
            except httpx.TimeoutException:
                gen.finish, gen.error = "timeout", f"no complete reply within {timeout_s:.1f} s"
            except Exception as exc:
                gen.finish, gen.error = "error", f"llama-server: {exc}"[:300]
            finally:
                gen.total_ms = round((time.monotonic() - t0) * 1000, 1)
                self._cancel.discard(rid)
                self._current = None
            if gen.finish in ("cancelled", "superseded"):
                self.cancelled += 1
            elif not gen.ok:
                self.failures += 1
            else:
                self.latency_ms = (self.latency_ms + [gen.total_ms])[-50:]
            self.last = {"finish": gen.finish, "total_ms": gen.total_ms, "first_token_ms": gen.first_token_ms, "gen_tokens": gen.gen_tokens,
                         "gen_tps": gen.gen_tps, "prompt_tokens": gen.prompt_tokens, "background": background, "error": gen.error}
            return gen

    def memory(self, timeout_s: float = 1.0) -> dict:
        """Server process memory: resident set and physical footprint (includes Metal buffers)."""
        if self._proc is None or self._proc.poll() is not None:
            return {}
        import re

        import psutil

        try:
            procs = [psutil.Process(self._proc.pid), *psutil.Process(self._proc.pid).children(recursive=True)]
            server = procs[-1]
            out = {"rss_mb": round(server.memory_info().rss / 2**20, 1)}
            fp = subprocess.run(["footprint", "-p", str(server.pid)], capture_output=True, text=True, timeout=max(1.0, timeout_s * 5)).stdout
            m = re.search(r"phys_footprint:\s*([\d.]+)\s*([KMG]B)", fp) or re.search(r"Footprint:\s*([\d.]+)\s*([KMG]B)", fp)
            if m:
                out["footprint_mb"] = round(float(m.group(1)) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)], 1)
            return out
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def describe(self) -> dict:
        lat = sorted(self.latency_ms)
        return {
            "provider": self.provider, "model": self.model, "state": self.state, "error": self.error, "ready": self.ready_info,
            "calls": self.calls, "failures": self.failures, "cancelled": self.cancelled, "last": self.last,
            "p50_ms": lat[len(lat) // 2] if lat else None, "busy": self._current is not None,
            "prefill": {"sent": self.prefills, "hits": self.prefill_hits, "misses": self.prefill_misses, "last": self.last_prefill},
        }
