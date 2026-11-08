"""The one gateway our own managed agents' tools run through. Code Quest's tools and the investigator's tools both pass here.

The decision is deterministic and made here, by a fixed list and fixed argument checks. A model's request is only ever a request. When
NeMo Relay is recording, the same decision is registered with it as a conditional-execution guardrail, so Relay rejects the call too and
its own trace shows the rejection. If Relay is missing or fails, the decision does not change. A denied tool's function is never handed to
Relay or anything else, so it cannot run.

This covers tools called through this gateway and nothing else. It does not control shell commands, USB drivers, or any process outside
our managed agents.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

Permission = Literal["read", "sandbox", "propose"]
ArgsCheck = Callable[[dict], str | None]  # the reason to refuse, or None


@dataclass(frozen=True)
class ToolRule:
    name: str
    permission: Permission
    check: ArgsCheck | None = None


@dataclass(frozen=True)
class GateDecision:
    tool: str
    allowed: bool
    reason: str | None
    rule: str


@dataclass
class GateOutcome:
    decision: GateDecision
    value: Any = None
    error: str | None = None
    callback_ran: bool = False
    duration_ms: float = 0.0
    trace_source: Literal["nemo_relay", "locki_log"] = "locki_log"
    relay_saw_rejection: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision.allowed


@dataclass
class GateStats:
    allowed: int = 0
    denied: int = 0
    denied_callbacks_run: int = 0  # must stay 0: the proof that a denial stopped the callback
    decisions: list[dict] = field(default_factory=list)


class ProtectedToolGate:
    def __init__(self, on_decision: Callable[[GateDecision, GateOutcome, dict], None] | None = None):
        self._rules: dict[str, ToolRule] = {}
        self.on_decision = on_decision
        self.frozen: str | None = None
        self.stats = GateStats()
        self._in_flight: dict[str, GateDecision] = {}

    def register(self, name: str, permission: Permission, check: ArgsCheck | None = None) -> None:
        self._rules[name] = ToolRule(name, permission, check)

    def tools(self) -> list[str]:
        return sorted(self._rules)

    def freeze(self, reason: str) -> None:
        """During an incident, nothing but reading runs."""
        self.frozen = reason

    def unfreeze(self) -> None:
        self.frozen = None

    def decide(self, name: str, args: Any) -> GateDecision:
        rule = self._rules.get(name)
        if rule is None:
            return GateDecision(name, False, f"{name[:60]} is not a tool this agent has", "not_on_the_list")
        if self.frozen and rule.permission != "read":
            return GateDecision(name, False, f"protected actions are frozen: {self.frozen}"[:200], "frozen")
        if not isinstance(args, dict):
            return GateDecision(name, False, "arguments must be an object", "bad_arguments")
        if rule.check is not None:
            try:
                why = rule.check(args)
            except Exception as exc:
                return GateDecision(name, False, f"the check failed: {type(exc).__name__}"[:200], "check_failed")  # fails closed
            if why:
                return GateDecision(name, False, why[:200], "argument_check")
        return GateDecision(name, True, None, "allowed")

    def guardrail(self, name: str, args: Any) -> str | None:
        """The decision in the form NeMo Relay's conditional-execution guardrail expects. Relay is shown shortened arguments, so for a call
        that came through execute() it enforces the decision already made on the real ones. Any other call is decided here on what it carries."""
        decision = self._in_flight.get(_key(name, args)) or self.decide(name, args)
        return None if decision.allowed else decision.reason or "denied"

    async def execute(self, name: str, args: dict, fn: Callable[[], Awaitable], *, relay=None, summary: Callable[[object], dict] | None = None, context: dict | None = None) -> GateOutcome:
        started = time.monotonic()
        decision = self.decide(name, args)
        outcome = GateOutcome(decision)
        shown_args = safe_args(args)
        recording = relay is not None and relay.active
        key = _key(name, shown_args)
        self._in_flight[key] = decision
        try:
            if not decision.allowed:
                self.stats.denied += 1
                if recording:
                    outcome.relay_saw_rejection = await relay.record_rejection(name, shown_args, tool_call_id=(context or {}).get("tool_call_id"))
                    outcome.trace_source = "nemo_relay" if outcome.relay_saw_rejection else "locki_log"
                outcome.duration_ms = round((time.monotonic() - started) * 1000, 2)
                self._note(decision, outcome, context or {})
                return outcome
            self.stats.allowed += 1
            ran = {"yes": False}

            async def counted():
                ran["yes"] = True
                return await fn()

            try:
                if recording:
                    outcome.value = await relay.tool(name, shown_args, counted, summary, tool_call_id=(context or {}).get("tool_call_id"))
                    outcome.trace_source = "nemo_relay" if relay.active else "locki_log"
                else:
                    outcome.value = await counted()
            except Exception as exc:
                outcome.error = exc
        finally:
            self._in_flight.pop(key, None)
        outcome.callback_ran = ran["yes"]
        outcome.duration_ms = round((time.monotonic() - started) * 1000, 2)
        self._note(decision, outcome, context or {})
        return outcome

    def _note(self, decision: GateDecision, outcome: GateOutcome, context: dict) -> None:
        self.stats.decisions = ([*self.stats.decisions, {"tool": decision.tool, "allowed": decision.allowed, "reason": decision.reason, "callback_ran": outcome.callback_ran, "trace_source": outcome.trace_source, "at": time.time()}])[-50:]
        if not decision.allowed and outcome.callback_ran:
            self.stats.denied_callbacks_run += 1
            log.error("a denied tool's callback ran: %s", decision.tool)
        if self.on_decision is not None:
            try:
                self.on_decision(decision, outcome, context)
            except Exception:
                log.exception("recording a gate decision failed")

    def snapshot(self) -> dict:
        s = self.stats
        return {"tools": self.tools(), "frozen": self.frozen, "allowed": s.allowed, "denied": s.denied, "denied_callbacks_run": s.denied_callbacks_run, "recent": s.decisions[-10:],
                "scope": "Only tools called through this gateway. Not shell commands, USB drivers or other processes."}


def _key(name: str, args: Any) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"


def safe_args(args: Any) -> dict:
    """What a trace may keep: argument names and short plain values. Long text stays out of it."""
    if not isinstance(args, dict):
        return {}
    out: dict = {}
    for key, value in list(args.items())[:12]:
        if isinstance(value, (int, float, bool)) or value is None:
            out[str(key)[:40]] = value
        elif isinstance(value, str) and len(value) <= 120:
            out[str(key)[:40]] = value
        else:
            out[str(key)[:40]] = f"<{type(value).__name__}, {len(json.dumps(value, default=str))} chars>"
    return out
