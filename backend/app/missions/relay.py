"""Optional: a mission recorded by real NVIDIA NeMo Relay, through its documented Python binding, when the
``nemo-relay`` package is installed and ``LOCKEDIN_RELAY_ENABLED=true``. Output is Relay's own ATOF format in
``<mission>/relay/events.jsonl``. It is separate from ``trace.jsonl``, which is LOCKED IN's own log.

Runs without network (checked in a sandbox, see docs/legacy/CODE_QUEST.md). Relay keeps whatever is passed to it, so
only names, sizes and outcomes are: never prompts, file contents or replies. A failure in Relay is noted and
turned off for that mission; it never touches the mission or the tool being recorded.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

PLUGINS = """version = 1

[policy]
unknown_component = "error"
unknown_field = "error"
unsupported_value = "error"

[[components]]
kind = "observability"
enabled = true

[components.config]
version = 4
enable_full_payloads = false

[components.config.atof]
enabled = true

[[components.config.atof.sinks]]
type = "file"
output_directory = "{output}"
filename = "events.jsonl"
mode = "append"
"""


def installed() -> bool:
    return importlib.util.find_spec("nemo_relay") is not None


GATE_NAME = "locki-protected-tool-gate"


class RelayRecorder:
    def __init__(self, guardrail: Callable[[str, object], str | None] | None = None) -> None:
        self.guardrail = guardrail  # the gate's decision, registered with Relay so Relay rejects a denied call itself
        self._guard_registered = False
        self.trace_id: str | None = None
        self.events_path: Path | None = None
        self.error: str | None = None
        self._ctx = None
        self._scope = None
        self._handle = None
        self._relay = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def _failed(self, where: str, exc: Exception) -> None:
        self.error = f"{where}: {exc}"[:200]
        log.warning("NeMo Relay turned off for this mission (%s)", self.error)
        self._handle = None

    async def start(self, folder: Path, mission_id: str, scope_name: str = "code-quest-mission") -> None:
        if not installed():
            self.error = "the nemo-relay package is not installed (make relay-setup)"
            return
        try:
            import nemo_relay

            self._relay = nemo_relay
            folder.mkdir(parents=True, exist_ok=True)
            plugins = folder / "plugins.toml"
            plugins.write_text(PLUGINS.format(output=str(folder).replace("\\", "/")))
            self._ctx = nemo_relay.plugin.activate({}, str(plugins))
            await self._ctx.__aenter__()
            if self.guardrail is not None:
                nemo_relay.guardrails.register_tool_conditional_execution(GATE_NAME, 10, self.guardrail)
                self._guard_registered = True
            self._scope = nemo_relay.scope.scope(scope_name, nemo_relay.ScopeType.Agent, data={"mission": mission_id})
            self._handle = self._scope.__enter__()
            self.events_path = folder / "events.jsonl"
            self.trace_id = _trace_id(nemo_relay)
        except Exception as exc:
            self._failed("start", exc)

    async def record_rejection(self, name: str, args: dict, tool_call_id: str | None = None) -> bool:
        """The gate denied this call. Relay is shown the attempt so its own guardrail rejects it and its trace says so. Its callback is a stub:
        the real tool is never handed over. True if Relay rejected it."""
        if not self.active or not self._guard_registered:
            return False

        async def never(_args):
            log.error("Relay ran the stub for a denied tool: %s", name)
            return self._relay.ToolExecutionResult({"stub": True})

        try:
            await self._relay.tools.execute(name, args, never, handle=self._handle, tool_call_id=tool_call_id)
        except RuntimeError as exc:
            return "guardrail rejected" in str(exc)
        except Exception as exc:
            self._failed(f"rejection of {name}", exc)
        return False

    async def tool(self, name: str, args: dict, fn: Callable[[], Awaitable], summary: Callable[[object], dict] | None = None, tool_call_id: str | None = None):
        """Runs ``fn`` exactly once and returns its result, recorded as a Relay tool call when it can be."""
        if not self.active:
            return await fn()
        box: dict = {}

        async def call(_args):
            box["started"] = True
            try:
                box["result"] = await fn()
            except Exception as exc:
                box["raised"] = exc
                return self._relay.ToolExecutionResult({"raised": type(exc).__name__})
            return self._relay.ToolExecutionResult(summary(box["result"]) if summary else {"done": True})

        try:
            await self._relay.tools.execute(name, args, call, handle=self._handle, tool_call_id=tool_call_id)
        except Exception as exc:
            if "guardrail rejected" in str(exc) and not box.get("started"):
                raise  # a rejection is a decision, never a reason to run the tool anyway
            self._failed(f"tool {name}", exc)
            if not box.get("started"):
                return await fn()
        if "raised" in box:
            raise box["raised"]
        return box["result"]

    async def llm(self, name: str, meta: dict, fn: Callable[[], Awaitable]):
        """One model call. Sizes only: the prompt itself does not go to Relay."""
        if not self.active:
            return await fn()
        box: dict = {}

        async def call(_request):
            box["started"] = True
            box["result"] = await fn()
            g = box["result"]
            return {"finish": getattr(g, "finish", None), "total_ms": getattr(g, "total_ms", None), "first_token_ms": getattr(g, "first_token_ms", None), "gen_tokens": getattr(g, "gen_tokens", None)}

        try:
            await self._relay.llm.execute(name, self._relay.LLMRequest({}, meta), call, handle=self._handle)
        except Exception as exc:
            self._failed(f"llm {name}", exc)
            if not box.get("started"):
                return await fn()
            if "result" not in box:
                raise
        return box["result"]

    def mark(self, name: str, data: dict | None = None) -> None:
        if not self.active:
            return
        try:
            self._relay.scope.event(name, handle=self._handle, data=data or {})
        except Exception as exc:
            self._failed(f"mark {name}", exc)

    async def stop(self) -> None:
        handle, self._handle = self._handle, None
        try:
            if self._scope is not None and handle is not None:
                self._scope.__exit__(None, None, None)
            if self._guard_registered:
                self._relay.guardrails.deregister_tool_conditional_execution(GATE_NAME)
                self._guard_registered = False
            if self._relay is not None and self._ctx is not None:
                await self._relay.subscribers.flush_async()
                await self._ctx.__aexit__(None, None, None)
        except Exception as exc:
            self.error = f"stop: {exc}"[:200]
            log.warning("NeMo Relay did not close cleanly: %s", exc)
        self._scope = self._ctx = None

    def snapshot(self) -> dict:
        return {"active": self.active, "events_path": None if self.events_path is None else str(self.events_path), "error": self.error, "trace_id": self.trace_id, "guardrail": self._guard_registered, "source": "NeMo Relay's own events"}


def _trace_id(relay) -> str | None:
    """The W3C trace id of the current scope, to correlate this run with Relay's own events."""
    try:
        parent = relay.capture_traceparent()
    except Exception:
        return None
    parts = str(parent or "").split("-")
    return parts[1] if len(parts) >= 4 else None


class RelayedLLM:
    """The same model, with each generation recorded as a Relay LLM call. Everything else passes through."""

    def __init__(self, llm, relay: RelayRecorder):
        self._llm = llm
        self._relay = relay

    def __getattr__(self, name):
        return getattr(self._llm, name)

    async def generate(self, context: str, **kw):
        name = str(getattr(self._llm, "model", "model"))
        return await self._relay.llm(name, {"context_chars": len(context), "max_tokens": kw.get("max_tokens")}, lambda: self._llm.generate(context, **kw))
