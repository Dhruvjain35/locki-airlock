"""The local language model, seen from the backend: a worker process (llm_worker.py) that
loads a quantised Qwen3 with MLX and streams replies back.

One request runs at a time. A request may be marked ``background`` (a line prepared ahead
of time for a likely event): a user's turn cancels it instead of waiting behind it.
``prefill`` lets the worker read the context while the user is still talking; the next
request whose context starts with that text only pays for the rest (llm_worker.py).
Nothing here decides what happens with the text; see dialogue.py.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SPEC_MAX_AGE_S = 20.0  # a prefill nobody used (the turn went to the parser) stops blocking background work
MAX_WORKER_RESTARTS = 3  # then it stays down and fixed lines carry on: a crash loop must not eat the Mac's memory
STALLS_BEFORE_RESTART = 3  # requests in a row without a single token


@dataclass
class Generation:
    text: str = ""
    finish: str = ""  # stop | length | cancelled | superseded | timeout | error | unavailable
    error: str | None = None
    first_token_ms: float | None = None
    total_ms: float | None = None
    prompt_tokens: int | None = None
    gen_tokens: int | None = None
    gen_tps: float | None = None
    prompt_tps: float | None = None
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.finish in ("stop", "length") and bool(self.text)


async def safe_generate(call: Callable[[], Awaitable[Generation]]) -> Generation:
    """A model failure of any kind is a failed generation, never an exception in the middle of someone's turn."""
    try:
        return await call()
    except Exception as exc:
        log.exception("the language model call failed")
        return Generation(finish="error", error=f"{type(exc).__name__}: {exc}"[:200])


class LocalLLM:
    provider = "local"

    def __init__(self, model_name: str, models_dir: Path, load_timeout_s: float = 120.0):
        self.model = model_name
        self.model_dir = Path(models_dir) / "llm" / model_name
        self.state = "loading"
        self.error: str | None = None
        self.ready_info: dict = {}
        self.calls = 0
        self.failures = 0
        self.cancelled = 0
        self.last: dict | None = None
        self.latency_ms: list[float] = []
        self._next_id = 0
        self._current: tuple[int, bool] | None = None  # (id, background)
        self._waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = {}
        self._lock: asyncio.Lock | None = None
        self._send_lock = threading.Lock()
        self._stats_event = threading.Event()
        self._stats: dict = {}
        self._closed = False
        self._spec_text: str | None = None
        self._spec_id: str | None = None
        self._spec_at = 0.0
        self._spec_n = 0
        self.prefills = 0
        self.prefill_hits = 0
        self.prefill_misses = 0
        self.last_prefill: dict | None = None
        self.restarts = 0
        self._stalls = 0
        if not (self.model_dir / "config.json").exists():
            self.state, self.error = "unavailable", f"model not downloaded: {self.model_dir} (scripts/download_models.py)"
            self._proc = None
            return
        self._load_timeout_s = load_timeout_s
        self._spawn()

    def _spawn(self) -> None:
        ctx = mp.get_context("spawn")
        self._conn, child = ctx.Pipe()
        # Set while speech-to-text runs on the same GPU: the worker pauses prefill work.
        self.stt_busy = ctx.Event()
        # Set while the voice renders a sentence of the reply: the worker pauses between tokens.
        self.voice_busy = ctx.Event()
        from . import llm_worker

        self._proc = ctx.Process(target=llm_worker.worker_main, args=(child, str(self.model_dir), self.stt_busy, self.voice_busy), daemon=True, name="llm-worker")
        self._proc.start()
        threading.Thread(target=self._reader, daemon=True, name="llm-reader").start()

    def _restart(self) -> None:
        """The worker died (the Mac can kill it when memory runs out): bring it back so a live demo carries on."""
        if self._closed or self.restarts >= MAX_WORKER_RESTARTS:
            return
        self.restarts += 1
        log.warning("restarting the local LLM worker (%d of %d)", self.restarts, MAX_WORKER_RESTARTS)
        self.state, self.error, self._stalls, self._spec_text = "loading", None, 0, None
        try:
            self._proc.join(timeout=1)
            self._spawn()
        except Exception as exc:
            self.state, self.error = "error", f"could not restart the language model: {exc}"[:200]

    # ------------------------------------------------------------------ worker I/O
    def _reader(self) -> None:
        conn = self._conn
        exited = False
        try:
            if not conn.poll(self._load_timeout_s):
                self.state, self.error = "error", f"timed out loading {self.model}"
                return
            msg = conn.recv()
            if msg.get("op") != "ready":
                self.state, self.error = "error", msg.get("error", "unknown error")
                log.warning("local LLM unavailable: %s", self.error)
                return
            self.ready_info = {k: v for k, v in msg.items() if k != "op"}
            self.state = "ready"
            log.info("local LLM %s ready: %s", self.model, self.ready_info)
            while not self._closed:
                msg = conn.recv()
                op = msg.get("op")
                if op == "stats":
                    self._stats = {k: v for k, v in msg.items() if k != "op"}
                    self._stats_event.set()
                    continue
                if op == "prefilled":
                    self.last_prefill = {k: v for k, v in msg.items() if k != "op"}
                    continue
                rid = msg.get("id")
                waiter = self._waiters.get(rid)
                if waiter is not None:
                    loop, q = waiter
                    loop.call_soon_threadsafe(q.put_nowait, msg)
        except (EOFError, OSError):
            if not self._closed:
                self.state, self.error = "error", "language model process exited"
                log.warning("local LLM worker exited")
                exited = True
        except Exception as exc:  # never let the reader die silently
            self.state, self.error = "error", f"reader failed: {exc}"
        finally:
            for loop, q in list(self._waiters.values()):
                loop.call_soon_threadsafe(q.put_nowait, {"op": "error", "error": self.error or "worker gone"})
        if exited:
            self._restart()

    def _send(self, msg: dict) -> None:
        with self._send_lock:
            self._conn.send(msg)

    # ------------------------------------------------------------------ API
    async def generate(
        self,
        context: str,
        *,
        max_tokens: int = 96,
        temperature: float = 0.7,
        top_p: float = 0.9,
        timeout_s: float = 6.0,
        on_delta: Callable[[str], None] | None = None,
        background: bool = False,
        seed: int | None = None,
    ) -> Generation:
        if self.state != "ready":
            return Generation(finish="unavailable", error=self.error or self.state)
        if self._lock is None:
            self._lock = asyncio.Lock()
        if not background and self._current is not None and self._current[1]:
            self.cancel(self._current[0])  # a person is waiting: drop the prepared line
        if self._spec_text is not None and time.monotonic() - self._spec_at > SPEC_MAX_AGE_S:
            self._spec_text = None  # the turn it was for was answered without the model
        if background and (self._lock.locked() or self._spec_text is not None):
            return Generation(finish="cancelled", error="busy" if self._lock.locked() else "the user is talking")
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()
            self._waiters[rid] = (loop, q)
            self._current = (rid, background)
            self.calls += 1
            t0 = time.monotonic()
            gen = Generation()
            msg = {"op": "generate", "id": rid, "context": context, "max_tokens": max_tokens, "temperature": temperature, "top_p": top_p, "seed": seed,
                   "background": background}
            if self._spec_text is not None:
                if context.startswith(self._spec_text):
                    msg["prefix_id"] = self._spec_id
                    self.prefill_hits += 1
                else:
                    self.prefill_misses += 1
                self._spec_text = None  # the worker drops it after any request
            try:
                self._send(msg)
                deadline = t0 + timeout_s
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        self.cancel(rid)
                        gen.finish, gen.error = "timeout", f"no complete reply within {timeout_s:.1f} s"
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=left)
                    except TimeoutError:
                        continue
                    op = msg.get("op")
                    if op == "delta":
                        if gen.first_token_ms is None:
                            gen.first_token_ms = round((time.monotonic() - t0) * 1000, 1)
                        gen.text += msg["text"]
                        if on_delta is not None:
                            try:
                                on_delta(msg["text"])
                            except Exception:
                                log.exception("on_delta failed")
                        continue
                    if op == "done":
                        gen.finish = msg.get("finish", "stop")
                        gen.text = msg.get("text", gen.text)
                        gen.prompt_tokens, gen.gen_tokens = msg.get("prompt_tokens"), msg.get("gen_tokens")
                        gen.prompt_tps, gen.gen_tps = msg.get("prompt_tps"), msg.get("gen_tps")
                        gen.stats = {"worker_first_token_ms": msg.get("first_token_ms"), "worker_total_ms": msg.get("total_ms"), "peak_gb": msg.get("peak_gb"),
                                     "prefilled": msg.get("prefilled", False)}
                        break
                    if op == "error":
                        gen.finish, gen.error = "error", msg.get("error")
                        break
            except (OSError, EOFError) as exc:
                gen.finish, gen.error = "error", f"worker unavailable: {exc}"
            finally:
                gen.total_ms = round((time.monotonic() - t0) * 1000, 1)
                self._waiters.pop(rid, None)
                self._current = None
            if gen.finish in ("cancelled", "superseded"):
                self.cancelled += 1
            elif not gen.ok:
                self.failures += 1
            else:
                self.latency_ms = (self.latency_ms + [gen.total_ms])[-50:]
            self.last = {"finish": gen.finish, "total_ms": gen.total_ms, "first_token_ms": gen.first_token_ms, "gen_tokens": gen.gen_tokens,
                         "gen_tps": gen.gen_tps, "prompt_tokens": gen.prompt_tokens, "background": background, "error": gen.error}
            self._note_stall(gen, background)
            return gen

    def _note_stall(self, gen: Generation, background: bool) -> None:
        """Several requests in a row without a single token: the worker is stuck, not slow. Its reader restarts it."""
        if gen.ok:
            self._stalls = 0
        elif gen.finish == "timeout" and gen.first_token_ms is None and not background:
            self._stalls += 1
            if self._stalls >= STALLS_BEFORE_RESTART and self.restarts < MAX_WORKER_RESTARTS:
                log.warning("no token from the local LLM in %d requests in a row: stopping the worker", self._stalls)
                self._stalls = 0
                self.state, self.error = "loading", "restarting: the language model stopped responding"
                self._proc.kill()

    def prefill(self, prefix: str) -> None:
        """The user started talking: let the worker read ``prefix`` (the context without the
        line being answered) now. Cheap to call; a stale prefix is simply not used."""
        if self.state != "ready" or not prefix or prefix == self._spec_text:
            return
        if self._current is not None:
            if not self._current[1]:
                return  # a reply is being written right now; it gets the GPU
            self.cancel(self._current[0])  # a prepared line can wait
        self._spec_n += 1
        self._spec_id, self._spec_text, self._spec_at = f"p{self._spec_n}", prefix, time.monotonic()
        self.prefills += 1
        try:
            self._send({"op": "prefill", "id": self._spec_id, "prefix": prefix})
        except (OSError, EOFError):
            self._spec_text = None

    def speech_recognition(self, busy: bool) -> None:
        """Whisper started / finished on the shared GPU (see llm_worker.yield_to_stt)."""
        ev = getattr(self, "stt_busy", None)
        if ev is not None:
            ev.set() if busy else ev.clear()

    def voice_rendering(self, busy: bool) -> None:
        """The voice started / finished rendering a sentence (see llm_worker voice_busy)."""
        ev = getattr(self, "voice_busy", None)
        if ev is not None:
            ev.set() if busy else ev.clear()

    def cancel(self, rid: int | None = None) -> None:
        cur = self._current
        if cur is None or (rid is not None and cur[0] != rid):
            return
        try:
            self._send({"op": "cancel", "id": cur[0]})
        except (OSError, EOFError):
            pass

    def memory(self, timeout_s: float = 1.0) -> dict:
        """Worker RSS and MLX memory (blocks up to ``timeout_s``; call from a thread)."""
        if self.state != "ready":
            return {}
        self._stats_event.clear()
        try:
            self._send({"op": "stats"})
        except (OSError, EOFError):
            return {}
        self._stats_event.wait(timeout_s)
        return dict(self._stats)

    def close(self) -> None:
        self._closed = True
        if self._proc is None:
            return
        try:
            self._send({"op": "stop"})
        except Exception:
            pass
        self._proc.join(timeout=3)
        if self._proc.is_alive():
            self._proc.terminate()

    def describe(self) -> dict:
        lat = sorted(self.latency_ms)
        return {
            "provider": self.provider,
            "model": self.model,
            "state": self.state,
            "error": self.error,
            "ready": self.ready_info,
            "calls": self.calls,
            "failures": self.failures,
            "restarts": self.restarts,
            "cancelled": self.cancelled,
            "last": self.last,
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "busy": self._current is not None,
            "prefill": {"sent": self.prefills, "hits": self.prefill_hits, "misses": self.prefill_misses, "last": self.last_prefill},
        }
