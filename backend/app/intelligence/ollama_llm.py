"""A local language model served by Ollama, with the same interface as LocalLLM (local_llm.py), so
the same dialogue, tools and guards run on whichever model is chosen.

Local only: the address is fixed to this Mac, cloud-hosted model names are refused, and nothing is
ever downloaded from here (``ollama pull`` is a decision for a person; a missing model is reported).
Use an explicit tag: the bare name ``nemotron-3.5-bolt`` is a 25 GB model with no Apple Silicon support,
while ``nemotron-3.5-bolt:e4b`` is the 7.9 GB one.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .llm_worker import _json_closed
from .local_llm import Generation
from .prompting import PERSONA

log = logging.getLogger(__name__)

BASE = "http://127.0.0.1:11434"
CONTEXT_TOKENS = 8192


def _refusal(model: str) -> str | None:
    if "cloud" in model.lower():
        return f"{model} runs in the cloud: LOCKED IN only uses models on this Mac"
    if ":" not in model:
        return f"{model} has no tag: name the exact one, for example nemotron-3.5-bolt:e4b"
    return None


def lan_address_ok(base: str) -> bool:
    """The team's own machines only: loopback, a private LAN address, or a .local name. Never a public host."""
    parts = urlsplit(base)
    if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/"):
        return False
    host = parts.hostname
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def serves(base: str, model: str, timeout_s: float = 1.5) -> bool:
    """Whether Ollama at this loopback address (this Mac, or a tunnel to the team's M5) answers and already has this model. Reads the list
    of models only: nothing is pulled, loaded or generated."""
    if not base.startswith(("http://127.0.0.1:", "http://localhost:")):
        return False
    import httpx

    try:
        r = httpx.get(f"{base.rstrip('/')}/api/tags", timeout=timeout_s)
        names = {m.get("name") for m in r.json().get("models", [])} if r.status_code == 200 else set()
    except (httpx.HTTPError, ValueError, AttributeError):
        return False
    return model in names


class OllamaLLM:
    provider = "ollama"

    def __init__(self, model: str, *, base: str = BASE, keep_alive: str = "30m", load_timeout_s: float = 180.0, warm: bool = True, allow_lan: bool = False):
        if allow_lan:
            if not lan_address_ok(base):
                raise ValueError("Ollama must be on this Mac or a machine on the team's private network")
        elif not base.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("Ollama must be on this Mac")
        self.model = model
        self.base = base
        self.keep_alive = keep_alive
        self.state = "loading"
        self.error: str | None = None
        self.ready_info: dict = {}
        self.calls = self.failures = self.cancelled = 0
        self.last: dict | None = None
        self.latency_ms: list[float] = []
        self._lock: asyncio.Lock | None = None
        self._current: tuple[int, bool] | None = None
        self._cancel: set[int] = set()
        self._next_id = 0
        self._closed = False
        refusal = _refusal(model)
        if refusal:
            self.state, self.error = "unavailable", refusal
            return
        threading.Thread(target=self._load, args=(load_timeout_s, warm), daemon=True, name="ollama-load").start()

    def _load(self, timeout_s: float, warm: bool) -> None:
        import httpx

        t0 = time.monotonic()
        try:
            r = httpx.get(f"{self.base}/api/tags", timeout=3.0)
            r.raise_for_status()
            names = {m.get("name") for m in r.json().get("models", [])}
        except Exception as exc:
            self.state, self.error = "unavailable", f"Ollama is not running on this Mac ({exc})"[:200]
            return
        if self.model not in names:
            self.state, self.error = "unavailable", f"model not downloaded: {self.model}. Nothing was fetched; run: ollama pull {self.model}"
            return
        if warm:
            try:
                r = httpx.post(f"{self.base}/api/chat", json=self._body("warm up", 1, 0.1, 0.9, None, stream=False), timeout=timeout_s)
                r.raise_for_status()
            except Exception as exc:
                self.state, self.error = "error", f"could not load {self.model}: {exc}"[:200]
                return
        self.ready_info = {"load_ms": round((time.monotonic() - t0) * 1000)}
        self.state = "ready"
        log.info("Ollama model %s ready in %d ms", self.model, self.ready_info["load_ms"])

    def _body(self, context: str, max_tokens: int, temperature: float, top_p: float, seed: int | None, stream: bool = True, system: str | None = None, response_format: dict | None = None) -> dict:
        options = {"temperature": temperature, "top_p": top_p, "num_predict": max_tokens, "num_ctx": CONTEXT_TOKENS}
        if seed is not None:
            options["seed"] = seed
        body = {"model": self.model, "stream": stream, "think": False, "keep_alive": self.keep_alive, "options": options,
                "messages": [{"role": "system", "content": PERSONA if system is None else system}, {"role": "user", "content": context}]}
        if response_format is not None:
            body["format"] = response_format
        return body

    # ------------------------------------------------------------------ API (as LocalLLM)
    def speech_recognition(self, busy: bool) -> None:
        return

    def voice_rendering(self, busy: bool) -> None:
        return

    def cancel(self, rid: int | None = None) -> None:
        cur = self._current
        if cur is not None and (rid is None or cur[0] == rid):
            self._cancel.add(cur[0])

    async def generate(self, context: str, *, max_tokens: int = 96, temperature: float = 0.7, top_p: float = 0.9, timeout_s: float = 6.0,
                       on_delta: Callable[[str], None] | None = None, background: bool = False, seed: int | None = None, system: str | None = None, response_format: dict | None = None) -> Generation:
        import httpx

        if self.state != "ready":
            return Generation(finish="unavailable", error=self.error or self.state)
        if self._lock is None:
            self._lock = asyncio.Lock()
        if not background and self._current is not None and self._current[1]:
            self.cancel(self._current[0])  # a person is waiting: drop the prepared line
        if background and self._lock.locked():
            return Generation(finish="cancelled", error="busy")
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._current = (rid, background)
            self.calls += 1
            gen = Generation()
            t0 = time.monotonic()
            deadline = t0 + timeout_s
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=2.0)) as client:
                    async with client.stream("POST", f"{self.base}/api/chat", json=self._body(context, max_tokens, temperature, top_p, seed, system=system, response_format=response_format)) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if rid in self._cancel:
                                gen.finish = "cancelled" if background else "superseded"
                                break
                            if time.monotonic() > deadline:
                                gen.finish, gen.error = "timeout", f"no complete reply within {timeout_s:.1f} s"
                                break
                            if not line.strip():
                                continue
                            ev = json.loads(line)
                            if ev.get("error"):
                                gen.finish, gen.error = "error", str(ev["error"])[:300]
                                break
                            piece = (ev.get("message") or {}).get("content", "")
                            if piece:
                                if gen.first_token_ms is None:
                                    gen.first_token_ms = round((time.monotonic() - t0) * 1000, 1)
                                gen.text += piece
                                if on_delta is not None:
                                    try:
                                        on_delta(piece)
                                    except Exception:
                                        log.exception("on_delta failed")
                            if _json_closed(gen.text):
                                gen.finish = "stop"
                                break
                            if ev.get("done"):
                                gen.finish = "length" if ev.get("done_reason") == "length" else "stop"
                                gen.prompt_tokens, gen.gen_tokens = ev.get("prompt_eval_count"), ev.get("eval_count")
                                if ev.get("eval_duration"):
                                    gen.gen_tps = round((ev.get("eval_count") or 0) / (ev["eval_duration"] / 1e9), 1)
                                if ev.get("prompt_eval_duration"):
                                    gen.prompt_tps = round((ev.get("prompt_eval_count") or 0) / (ev["prompt_eval_duration"] / 1e9), 1)
                                break
                        else:
                            gen.finish = gen.finish or "stop"
            except httpx.TimeoutException:
                gen.finish, gen.error = "timeout", f"no complete reply within {timeout_s:.1f} s"
            except Exception as exc:
                gen.finish, gen.error = "error", f"Ollama: {exc}"[:300]
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
            self.last = {"finish": gen.finish, "total_ms": gen.total_ms, "first_token_ms": gen.first_token_ms, "gen_tokens": gen.gen_tokens, "gen_tps": gen.gen_tps,
                         "prompt_tokens": gen.prompt_tokens, "background": background, "error": gen.error}
            return gen

    def memory(self, timeout_s: float = 1.0) -> dict:
        """What Ollama reports the loaded model is using."""
        import httpx

        try:
            r = httpx.get(f"{self.base}/api/ps", timeout=timeout_s)
            r.raise_for_status()
            for m in r.json().get("models", []):
                if m.get("name") == self.model:
                    return {"footprint_mb": round((m.get("size") or 0) / 2**20, 1), "gpu_mb": round((m.get("size_vram") or 0) / 2**20, 1)}
        except Exception as exc:
            return {"error": str(exc)[:120]}
        return {}

    def close(self) -> None:
        self._closed = True

    def describe(self) -> dict:
        lat = sorted(self.latency_ms)
        return {
            "provider": self.provider, "model": self.model, "state": self.state, "error": self.error, "ready": self.ready_info,
            "calls": self.calls, "failures": self.failures, "cancelled": self.cancelled, "last": self.last,
            "p50_ms": lat[len(lat) // 2] if lat else None, "busy": self._current is not None,
            "prefill": {"sent": 0, "hits": 0, "misses": 0, "last": None},
        }
