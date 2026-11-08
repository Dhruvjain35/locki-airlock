"""Optional NVIDIA NeMo Relay policy gate around ONE approved tool: get_recent_usb_events.

Real integration, tested on the M5 on 2026-11-07 with nemo-relay 0.11.1
(hash-pinned in requirements-relay.txt):
  * a conditional-execution guardrail registered with
    nemo_relay.guardrails.register_tool_conditional_execution;
  * the gated tool runs through nemo_relay.tools.execute, so Relay decides
    BEFORE the tool body executes. A rejection raises
    RuntimeError("guardrail rejected: <reason>") and the body never runs.

Only the in-process SDK is used. We never initialize Relay's plugin host
(it would merge ~/.config/nemo-relay/plugins.toml and /etc/nemo-relay/plugins.toml),
and we start no CLI, gateway, daemon or exporter. In the test, the process
opened no network sockets.

Policy (deterministic and stricter than the schema bound of 1-1800 s, so the
block comes from Relay, not from argument validation): data minimization for
USB history. The investigator may look back at most `max_lookback_s` seconds
before the incident (default 600) without human approval.
"""

from __future__ import annotations

import importlib.metadata
import threading
import time
from typing import Any, Awaitable, Callable

GATED_TOOL = "get_recent_usb_events"
GUARDRAIL_NAME = "locki.usb_lookback_policy"
DEFAULT_MAX_LOOKBACK_S = 600
_REJECT_PREFIX = "guardrail rejected:"


class PolicyDenied(Exception):
    """NeMo Relay rejected the tool call before execution."""


class RelayGate:
    def __init__(self, max_lookback_s: int = DEFAULT_MAX_LOOKBACK_S):
        import nemo_relay  # ImportError if the optional dependency is missing

        self._nr = nemo_relay
        self.version = importlib.metadata.version("nemo-relay")
        self.max_lookback_s = max_lookback_s
        self.decisions: list[dict] = []
        self._lock = threading.Lock()
        self._register()

    @property
    def label(self) -> str:
        return f"nemo-relay {self.version} / {GUARDRAIL_NAME}"

    def _policy(self, tool_name: str, args: Any) -> str | None:
        try:
            if tool_name != GATED_TOOL:
                decision = None
            else:
                w = args.get("window_s", 300) if isinstance(args, dict) else None
                if w is None:
                    w = 300
                if not isinstance(w, int) or isinstance(w, bool):
                    decision = f"policy {GUARDRAIL_NAME}: window_s must be an integer"
                elif w > self.max_lookback_s:
                    decision = (f"policy {GUARDRAIL_NAME}: window_s={w} exceeds the {self.max_lookback_s} s "
                                f"USB lookback allowed without human approval; a request with window_s of at most "
                                f"{self.max_lookback_s} is permitted")
                else:
                    decision = None
        except Exception as e:  # noqa: BLE001 - fail closed with a clear reason
            decision = f"policy {GUARDRAIL_NAME}: evaluation error ({type(e).__name__}); failing closed"
        if tool_name == GATED_TOOL:
            with self._lock:
                self.decisions.append({"tool": tool_name, "args": args, "allowed": decision is None,
                                       "reason": decision, "at_ms": int(time.time() * 1000)})
        return decision

    def _register(self) -> None:
        g = self._nr.guardrails
        try:
            g.register_tool_conditional_execution(GUARDRAIL_NAME, 10, self._policy)
        except RuntimeError as e:  # 0.11.x raises on duplicate names (verified)
            if "already exists" not in str(e):
                raise
            g.deregister_tool_conditional_execution(GUARDRAIL_NAME)
            g.register_tool_conditional_execution(GUARDRAIL_NAME, 10, self._policy)

    def close(self) -> None:
        self._nr.guardrails.deregister_tool_conditional_execution(GUARDRAIL_NAME)

    async def execute(self, name: str, args: dict, run: Callable[[], Awaitable[Any]]) -> Any:
        """Run `run()` through Relay's managed tool pipeline (guardrails first).

        Only a minimal marker is handed to Relay as the tool result; the evidence
        itself stays in our process (Relay events would otherwise carry it).
        """
        nr = self._nr
        holder: dict = {}

        async def body(_relay_args):
            holder["out"] = await run()
            return nr.ToolExecutionResult({"ok": True})

        try:
            await nr.tools.execute(name, args, body)
        except RuntimeError as e:
            msg = str(e)
            if msg.startswith(_REJECT_PREFIX):
                raise PolicyDenied(msg[len(_REJECT_PREFIX):].strip()) from None
            raise
        return holder["out"]


_GATE: RelayGate | None = None
_GATE_LOCK = threading.Lock()


def get_gate(max_lookback_s: int = DEFAULT_MAX_LOOKBACK_S) -> RelayGate:
    """Process-wide gate (Relay guardrail registrations are global per process)."""
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = RelayGate(max_lookback_s)
        elif _GATE.max_lookback_s != max_lookback_s:
            _GATE.max_lookback_s = max_lookback_s
        return _GATE
