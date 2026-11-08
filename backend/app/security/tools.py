"""The investigator's tools. A fixed list, with arguments validated here, and every call goes through the protected tool gate. A model can only
ask. None of them runs a shell command, reads an arbitrary file, reaches the network, locks the screen or touches a USB device: they read the
security record, and two of them (a lock proposal and a review request) only write down that someone should look.

Text inside the record (a device's name, an app's name) comes from the outside world. It is data, and is labelled as such to the model.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import Id, SecurityEventV1, SecurityKind
from .tool_gate import Permission, ProtectedToolGate

if TYPE_CHECKING:
    from .service import SecurityService

MIN_EVIDENCE_TOOLS = 3
MAX_RESULT_EVENTS = 20
# An accusation or a verdict, which this agent never makes. Allowed only when the sentence denies it ("that does not prove it is malicious").
_VERDICT = re.compile(r"\b(criminal|criminals|hacker|hackers|attacker|attackers|thief|thieves|burglar|intruder|intruders|culprit|perpetrator|guilty|police|law enforcement|malware|malicious|ransomware|virus|trojan|stolen|compromised)\b", re.I)
_DENIES = re.compile(r"\b(not|no|never|doesn'?t|does not|isn'?t|can'?t|cannot|without|unproven|unconfirmed|nothing)\b", re.I)
_LOCK_CLAIM = re.compile(r"\b(screen|mac|workstation|computer)\b.{0,20}\b(is|was|has been|got)\b.{0,10}\blocked\b|\block\b.{0,20}\b(succeeded|worked|confirmed|completed)\b", re.I)
_NOTIFY_CLAIM = re.compile(r"\b(owner|reviewer|teammate|someone)\b.{0,25}\b(notified|alerted|told|informed|contacted)\b", re.I)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class EventArgs(_Args):
    event_id: Id


class RecentArgs(_Args):
    limit: int = Field(10, ge=1, le=MAX_RESULT_EVENTS)


class LockProposalArgs(_Args):
    reason: str = Field(min_length=1, max_length=200)


class ReviewArgs(_Args):
    question: str = Field(min_length=1, max_length=240)
    urgency: Literal["normal", "urgent"] = "normal"


class Statement(_Args):
    statement: str = Field(min_length=1, max_length=240)
    evidence_ids: list[Id] = Field(min_length=1, max_length=10)


class IncidentReport(_Args):
    """What the investigation found. Every known fact cites the evidence behind it, and what is not known is said."""

    summary: str = Field(min_length=1, max_length=480)
    known: list[Statement] = Field(min_length=1, max_length=8)
    unknown: list[str] = Field(min_length=1, max_length=8)
    assessment: Literal["likely_routine", "needs_owner_review", "unusual_needs_review", "insufficient_evidence"]
    recommended_action: Literal["none", "owner_review", "lock_screen", "owner_checks_device"]
    spoken_summary: str = Field(min_length=1, max_length=240)


@dataclass
class ToolResult:
    tool: str
    status: Literal["ok", "denied", "error"]
    data: Any = None
    error: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    callback_ran: bool = False
    trace_source: str = "locki_log"


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[_Args]
    permission: Permission
    handler: Callable[[Any, RunContext], Awaitable[Any]]
    evidence: bool = False  # counts as looking at the evidence


@dataclass
class RunContext:
    run_id: str
    seen_evidence: set[str] = field(default_factory=set)
    evidence_tools_used: set[str] = field(default_factory=set)
    report: IncidentReport | None = None
    proposals: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    step: int = 0


def report_problems(report: IncidentReport, ctx: RunContext, *, lock_status: str | None) -> list[str]:
    """Deterministic checks on what the model wrote. It has to pass these before it counts as a report."""
    problems: list[str] = []
    if len(ctx.evidence_tools_used) < MIN_EVIDENCE_TOOLS:
        problems.append(f"look at the evidence first: only {len(ctx.evidence_tools_used)} of {MIN_EVIDENCE_TOOLS} evidence tools used")
    for item in report.known:
        missing = [e for e in item.evidence_ids if e not in ctx.seen_evidence]
        if missing:
            problems.append(f"cites evidence no tool returned: {', '.join(missing)[:120]}")
    texts = [report.summary, report.spoken_summary, *(k.statement for k in report.known), *report.unknown]
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            word = _VERDICT.search(sentence)
            if word and not _DENIES.search(sentence):
                problems.append(f"makes a verdict or an accusation: {word.group(0)}")
            if _LOCK_CLAIM.search(sentence) and not _DENIES.search(sentence) and lock_status != "LOCK_CONFIRMED":
                problems.append(f"claims a lock the record does not confirm (it says {lock_status or 'no lock'})")
            if _NOTIFY_CLAIM.search(sentence) and not _DENIES.search(sentence) and not ctx.reviews:
                problems.append("claims someone was notified, and nobody was")
    return problems


class AirlockTools:
    def __init__(self, security: SecurityService, gate: ProtectedToolGate):
        self.security = security
        self.gate = gate
        self.specs: dict[str, ToolSpec] = {}
        for spec in (
            ToolSpec("inspect_usb_event", "What the OS reported about one USB event. args: event_id", EventArgs, "read", self._inspect_usb, True),
            ToolSpec("inspect_app_metadata", "What the OS reported about one app launch, including its code signature. args: event_id", EventArgs, "read", self._inspect_app, True),
            ToolSpec("inspect_camera_observation", "What the desk camera reported in one event: a person at the desk, a face state, a hand in a zone, or the camera's own state, with its zone, time and dwell. args: event_id", EventArgs, "read", self._inspect_camera, True),
            ToolSpec("read_recent_security_events", "The recent events on this session, newest last: USB, apps, camera, lock, protected tools. args: limit", RecentArgs, "read", self._recent, True),
            ToolSpec("get_session_policy", "The owner's armed session: baseline size, approved devices and apps, whether automatic locking is on. no args", NoArgs, "read", self._policy, True),
            ToolSpec("get_device_health", "Which monitors are working, the desk camera, the last lock status. no args", NoArgs, "read", self._health, True),
            ToolSpec("run_sandbox_tests", "Run pytest in the managed sandbox project, to see whether its code still behaves. no args", NoArgs, "sandbox", self._sandbox_tests),
            ToolSpec("propose_lock_response", "Ask whether a screen lock would be allowed, and record the proposal. It does not lock anything. args: reason", LockProposalArgs, "propose", self._propose_lock),
            ToolSpec("request_human_review", "Put a question on the owner's review list on the dashboard. Nothing is sent anywhere else. args: question, urgency (normal or urgent)", ReviewArgs, "propose", self._review),
            ToolSpec("prepare_incident_report", "Finish the investigation. args: summary, known [{statement, evidence_ids}], unknown [text], assessment (likely_routine, needs_owner_review, unusual_needs_review, insufficient_evidence), recommended_action (none, owner_review, lock_screen, owner_checks_device), spoken_summary", IncidentReport, "propose", self._report),
        ):
            self.specs[spec.name] = spec
            gate.register(spec.name, spec.permission, self._args_check(spec))

    def _args_check(self, spec: ToolSpec):
        def check(args: dict) -> str | None:
            try:
                spec.args.model_validate(args)
            except ValidationError as exc:
                first = exc.errors()[0]
                return f"bad arguments: {'.'.join(str(x) for x in first['loc']) or 'args'}: {first['msg']}"[:200]
            return None

        return check

    def describe(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self.specs.values())

    async def call(self, name: str, args: Any, ctx: RunContext, *, relay=None, context: dict | None = None) -> ToolResult:
        """Every request comes through here, whatever the model wrote."""
        spec = self.specs.get(str(name))

        async def run():
            return await spec.handler(spec.args.model_validate(args), ctx)

        outcome = await self.gate.execute(str(name), args if isinstance(args, dict) else {"args": str(args)[:80]}, run, relay=relay, summary=_summary, context=context)
        if not outcome.allowed:
            return ToolResult(str(name)[:80], "denied", error=outcome.decision.reason, duration_ms=outcome.duration_ms, callback_ran=outcome.callback_ran, trace_source=outcome.trace_source)
        if outcome.error is not None:
            return ToolResult(spec.name, "error", error=str(outcome.error)[:240], duration_ms=outcome.duration_ms, callback_ran=outcome.callback_ran, trace_source=outcome.trace_source)
        data, evidence = outcome.value
        ctx.seen_evidence.update(evidence)
        if spec.evidence:
            ctx.evidence_tools_used.add(spec.name)
        return ToolResult(spec.name, "ok", data=data, evidence_ids=list(evidence), duration_ms=outcome.duration_ms, callback_ran=outcome.callback_ran, trace_source=outcome.trace_source)

    # ------------------------------------------------------------------ the record
    def _event(self, event_id: str) -> SecurityEventV1 | None:
        return next((e for e in self.security.evidence if e.event_id == event_id), None)

    async def _inspect_usb(self, args: EventArgs, ctx: RunContext):
        event = self._event(args.event_id)
        if event is None:
            raise LookupError(f"no event with id {args.event_id} in this session")
        if event.kind not in (SecurityKind.USB_DEVICE_ADDED, SecurityKind.USB_DEVICE_REMOVED):
            raise LookupError(f"{args.event_id} is a {event.kind.value} event, not a USB event")
        return {"event_id": event.event_id, "kind": event.kind.value, "age_s": _age(event), "metadata": event.metadata, "limits": "Identifiers the OS reports. Nothing on the device was read. A device can misreport them."}, [event.event_id]

    async def _inspect_app(self, args: EventArgs, ctx: RunContext):
        event = self._event(args.event_id)
        if event is None:
            raise LookupError(f"no event with id {args.event_id} in this session")
        if event.kind != SecurityKind.APP_LAUNCHED:
            raise LookupError(f"{args.event_id} is a {event.kind.value} event, not an app launch")
        return {"event_id": event.event_id, "age_s": _age(event), "metadata": event.metadata, "limits": "Visible app launches only. A signature says who signed an app, not that it is safe. Opening does not mean anyone has taken control."}, [event.event_id]

    async def _inspect_camera(self, args: EventArgs, ctx: RunContext):
        from .contracts import VISION_KINDS, VISION_SOURCES

        event = self._event(args.event_id)
        if event is None:
            raise LookupError(f"no event with id {args.event_id} in this session")
        if event.source not in VISION_SOURCES or event.kind not in VISION_KINDS:
            raise LookupError(f"{args.event_id} is a {event.kind.value} event, not a camera observation")
        return {"event_id": event.event_id, "kind": event.kind.value, "device_id": event.device_id, "age_s": _age(event), "confidence": event.confidence, "metadata": event.metadata,
                "limits": "What one camera saw from one angle. A face match is not identity verification and an unrecognized face is not an accusation. "
                          "A hand at the USB ports is not a device being connected: only the Mac's USB events say that. Out of view means not seen, not absent."}, [event.event_id]

    async def _recent(self, args: RecentArgs, ctx: RunContext):
        events = list(self.security.evidence)[-args.limit :]
        rows = [{"event_id": e.event_id, "kind": e.kind.value, "source": e.source.value, "age_s": _age(e), "seq": e.seq, "confidence": e.confidence, "flagged": self.security._flagged(e)} for e in events]
        return {"events": rows, "count_in_session": len(self.security.evidence)}, [e.event_id for e in events]

    async def _policy(self, args: NoArgs, ctx: RunContext):
        session = self.security.session
        if session is None:
            return {"armed": False}, []
        p = session.policy
        return {"armed": True, "session_id": session.session_id, "source": session.source, "armed_seconds_ago": round(time.time() - session.armed_at_ms / 1000), "baseline_devices": len(session.baseline), "warn_on_unknown_usb": p.warn_on_unknown_usb, "warn_on_watchlist_app": p.warn_on_watchlist_app,
                "auto_lock": p.auto_lock, "approved_devices": len(p.approved_devices), "approved_apps": p.approved_apps, "lock_mode": self.security.lock.mode}, []

    async def _health(self, args: NoArgs, ctx: RunContext):
        security = self.security
        monitor = security.monitor.snapshot() if security.monitor is not None else None
        publisher = security.publisher.snapshot() if security.publisher is not None else None
        from .contracts import VISION_SOURCES

        camera = [e for e in security.evidence if e.source in VISION_SOURCES or e.source.value.startswith("jetson")]
        lock = security.locks[-1] if security.locks else None
        return {
            "usb_monitor": None if monitor is None else {k: monitor["usb"][k] for k in ("method", "connected", "error")},
            "app_monitor": None if monitor is None else {k: monitor["apps"][k] for k in ("method", "coverage", "error")},
            "desk_camera": ({"status": "no camera events in this session", "last_event_age_s": None} if not camera else {"status": camera[-1].kind.value, "last_event_age_s": _age(camera[-1])})
            | {"running": None if security.camera is None else security.camera.running},
            "events_backend": None if publisher is None else {"transport": publisher["transport"], "pending": publisher["pending"], "last_delivery": publisher["last_delivery"]},
            "last_lock": None if lock is None else {"status": lock["status"], "dry_run": lock["dry_run"], "detail": lock["detail"]},
        }, []

    # ------------------------------------------------------------------ actions that stay inside the managed agent
    async def _sandbox_tests(self, args: NoArgs, ctx: RunContext):
        from ..missions.evidence import run_pytest

        missions = self.security.rt.missions
        folder = missions.workspace.root if missions.workspace is not None else Path(missions.prepare_demo())
        run = await run_pytest(folder, trigger="airlock_investigation")
        return {"status": run.status, "counts": run.counts(), "project": Path(folder).name, "ran_in": "the sandbox runner: no network, no keys, writes only inside the project"}, []

    async def _propose_lock(self, args: LockProposalArgs, ctx: RunContext):
        session = self.security.session
        allowed = session is not None and session.policy.auto_lock
        proposal = {"reason": args.reason, "at": time.time(), "would_be_allowed": allowed, "why_not": None if allowed else ("not armed" if session is None else "the owner has not opted in to automatic locking")}
        ctx.proposals.append(proposal)
        return {**proposal, "note": "This records a proposal. It did not lock anything."}, []

    async def _review(self, args: ReviewArgs, ctx: RunContext):
        item = {"question": args.question, "urgency": args.urgency, "run_id": ctx.run_id, "at": time.time()}
        ctx.reviews.append(item)
        self.security.review_requests.append(item)
        return {"queued": True, "where": "the owner's review list on the dashboard", "note": "Nothing was sent outside this Mac."}, []

    async def _report(self, report: IncidentReport, ctx: RunContext):
        lock = self.security.locks[-1]["status"] if self.security.locks else None
        problems = report_problems(report, ctx, lock_status=lock)
        if problems:
            raise ValueError("report refused: " + "; ".join(problems)[:220])
        ctx.report = report
        return {"accepted": True}, []


def _age(event: SecurityEventV1) -> int:
    return max(0, round(time.time() - event.observed_at / 1000))


def _summary(value: object) -> dict:
    """What Relay is told about a tool's result: its size, not its contents."""
    data = value[0] if isinstance(value, tuple) and value else value
    return {"ok": True, "keys": sorted(data)[:12] if isinstance(data, dict) else None, "evidence_count": len(value[1]) if isinstance(value, tuple) and len(value) > 1 else 0}
