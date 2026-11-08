"""Minimal async client for the local Ollama /api/chat endpoint.

Deliberately sends no `keep_alive` and no `num_ctx`, so requests never change
how the shared, already-loaded Nemotron instance is configured (Dhruv's M4
uses the same server through its SSH tunnel).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx


class OllamaUnavailable(Exception):
    pass


class ModelTimeout(Exception):
    pass


class ModelError(Exception):
    pass


class DegenerateOutput(ModelError):
    """Generation collapsed into a whitespace loop (seen with constrained JSON decoding)."""

    def __init__(self, message: str, partial: str, wall_ms: float):
        super().__init__(message)
        self.partial = partial
        self.wall_ms = wall_ms


@dataclass
class ChatResult:
    message: dict
    wall_ms: float
    total_ms: float | None
    load_ms: float | None
    prompt_tokens: int | None
    prompt_eval_ms: float | None
    eval_tokens: int | None
    eval_ms: float | None
    done_reason: str | None
    request_bytes: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def tool_calls(self) -> list[dict]:
        return list(self.message.get("tool_calls") or [])

    @property
    def content(self) -> str:
        return self.message.get("content") or ""


def _ns_to_ms(v) -> float | None:
    return round(v / 1e6, 2) if isinstance(v, (int, float)) else None


class OllamaClient:
    def __init__(self, base_url: str, model: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, transport=self._transport,
                                 timeout=httpx.Timeout(timeout_s, connect=min(3.0, timeout_s)))

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None, fmt: dict | None = None,
                   think: bool = False, temperature: float = 0.1, timeout_s: float = 45.0,
                   num_predict: int | None = None, guard_degenerate: bool = False) -> ChatResult:
        payload: dict = {"model": self.model, "messages": messages, "stream": False, "think": think,
                         "options": {"temperature": temperature}}
        if num_predict:
            payload["options"]["num_predict"] = num_predict
        if tools:
            payload["tools"] = tools
        if fmt is not None:
            payload["format"] = fmt
        if guard_degenerate:
            return await self._chat_streamed(payload, timeout_s)
        t0 = time.perf_counter()
        try:
            async with self._client(timeout_s) as client:
                resp = await asyncio.wait_for(client.post("/api/chat", json=payload), timeout=timeout_s)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise OllamaUnavailable(f"cannot reach Ollama at {self.base_url}: {type(e).__name__}") from e
        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            raise ModelTimeout(f"model call exceeded {timeout_s:.2f}s") from e
        except httpx.HTTPError as e:
            raise OllamaUnavailable(f"HTTP error talking to Ollama: {type(e).__name__}") from e
        wall_ms = round((time.perf_counter() - t0) * 1000, 2)
        if resp.status_code != 200:
            raise ModelError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            d = resp.json()
        except ValueError as e:
            raise ModelError("Ollama returned non-JSON body") from e
        if "error" in d:
            raise ModelError(f"Ollama error: {str(d['error'])[:200]}")
        return ChatResult(
            message=d.get("message") or {},
            wall_ms=wall_ms,
            total_ms=_ns_to_ms(d.get("total_duration")),
            load_ms=_ns_to_ms(d.get("load_duration")),
            prompt_tokens=d.get("prompt_eval_count"),
            prompt_eval_ms=_ns_to_ms(d.get("prompt_eval_duration")),
            eval_tokens=d.get("eval_count"),
            eval_ms=_ns_to_ms(d.get("eval_duration")),
            done_reason=d.get("done_reason"),
            request_bytes=len(resp.request.content or b""),
        )

    async def _chat_streamed(self, payload: dict, timeout_s: float) -> ChatResult:
        """Streamed variant that aborts as soon as output degenerates into whitespace.

        Closing the stream makes Ollama stop generating, so a degenerate decode
        costs ~1-2 s instead of running to the token cap or the deadline.
        """
        payload = {**payload, "stream": True}
        t0 = time.perf_counter()
        parts: list[str] = []
        tool_calls: list[dict] = []
        final: dict = {}

        async def consume():
            nonlocal final
            tail = ""
            async with self._client(timeout_s) as client:
                async with client.stream("POST", "/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode(errors="replace")
                        raise ModelError(f"Ollama returned HTTP {resp.status_code}: {body[:200]}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        d = json.loads(line)
                        if "error" in d:
                            raise ModelError(f"Ollama error: {str(d['error'])[:200]}")
                        m = d.get("message") or {}
                        piece = m.get("content") or ""
                        if piece:
                            parts.append(piece)
                            tail = (tail + piece)[-160:]
                            if len(tail) >= 96 and not tail[-96:].strip():
                                raise DegenerateOutput(
                                    "generation degenerated into whitespace", "".join(parts),
                                    round((time.perf_counter() - t0) * 1000, 2))
                        if m.get("tool_calls"):
                            tool_calls.extend(m["tool_calls"])
                        if d.get("done"):
                            final = d
                            return

        try:
            await asyncio.wait_for(consume(), timeout=timeout_s)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise OllamaUnavailable(f"cannot reach Ollama at {self.base_url}: {type(e).__name__}") from e
        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            raise ModelTimeout(f"model call exceeded {timeout_s:.2f}s") from e
        except (DegenerateOutput, ModelError):
            raise
        except httpx.HTTPError as e:
            raise OllamaUnavailable(f"HTTP error talking to Ollama: {type(e).__name__}") from e
        except ValueError as e:
            raise ModelError("Ollama stream contained invalid JSON") from e
        msg = {"role": "assistant", "content": "".join(parts)}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return ChatResult(
            message=msg, wall_ms=round((time.perf_counter() - t0) * 1000, 2),
            total_ms=_ns_to_ms(final.get("total_duration")), load_ms=_ns_to_ms(final.get("load_duration")),
            prompt_tokens=final.get("prompt_eval_count"), prompt_eval_ms=_ns_to_ms(final.get("prompt_eval_duration")),
            eval_tokens=final.get("eval_count"), eval_ms=_ns_to_ms(final.get("eval_duration")),
            done_reason=final.get("done_reason"), extra={"streamed": True})

    async def health(self, timeout_s: float = 3.0) -> dict:
        out: dict = {"url": self.base_url, "model": self.model, "reachable": False,
                     "model_installed": False, "model_loaded": False}
        try:
            async with self._client(timeout_s) as client:
                v = await client.get("/api/version")
                tags = await client.get("/api/tags")
                ps = await client.get("/api/ps")
        except httpx.HTTPError as e:
            out["error"] = f"{type(e).__name__}"
            return out
        out["reachable"] = v.status_code == 200
        out["ollama_version"] = (v.json() or {}).get("version") if v.status_code == 200 else None
        installed = [m for m in (tags.json() or {}).get("models", []) if m.get("name") == self.model] \
            if tags.status_code == 200 else []
        out["model_installed"] = bool(installed)
        if installed:
            out["model_digest"] = installed[0].get("digest")
            out["model_capabilities"] = installed[0].get("capabilities")
            out["runner"] = (installed[0].get("details") or {}).get("runner")
        loaded = [m for m in (ps.json() or {}).get("models", []) if m.get("name") == self.model] \
            if ps.status_code == 200 else []
        out["model_loaded"] = bool(loaded)
        if loaded:
            out["loaded_size_vram_bytes"] = loaded[0].get("size_vram")
            out["loaded_context_length"] = loaded[0].get("context_length")
            out["loaded_expires_at"] = loaded[0].get("expires_at")
        return out
