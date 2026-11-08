"""Runs missions inside the runtime: watches the approved project, runs the tests, applies the
results (engine.py), and tells the rest of LOCKED IN through the event bus and the voice.

Models help here in three ways and all three are checked: wording a plan (planner.py), explaining
a failure, and proposing an edit a person must approve. None of them can change a mission's state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections import deque
from pathlib import Path

from ..intelligence.local_llm import safe_generate
from ..telemetry.events import EventSource, EventType
from .engine import Change, Checkpoint, Mission, apply_run, new_mission
from .evidence import TestRun, run_pytest
from .planner import (
    PLAN_MAX_TOKENS,
    Plan,
    humanize,
    loads_lenient,
    parse_model_plan,
    plan_prompt,
    rule_plan,
    short_title,
)
from .relay import RelayedLLM, RelayRecorder
from .relay import installed as relay_installed
from .trace import MissionTrace
from .workspace import (
    FileMention,
    PolicyError,
    ProjectRegistry,
    Workspace,
    changed_tests,
    is_test_file,
    redact,
)

log = logging.getLogger(__name__)

SCAN_EVERY_S = 1.0
DEBOUNCE_S = 1.0
MIN_AUTO_RUN_GAP_S = 2.0
REACTION_MOOD_S = 8.0
PLAN_TIMEOUT_S = 25.0
HELP_TIMEOUT_S = 12.0
EDIT_TIMEOUT_S = 30.0
EDIT_MAX_TOKENS = 320
MAX_TOOL_OUTPUT = 1800
# Words in a question that say nothing about which test it means.
_NOT_ABOUT_A_TEST = frozenset(
    "test tests testing fail fails failing failed failure broken wrong bug bugs issue problem error why what where when how which this that these those the "
    "code function file files still keep keeps does doing done with about not and for can you could would should help stuck hint fix fixed fixing look show tell "
    "think off get got worked work working works there something some mine your have has had was were are its going happening right now just really".split()
)
DEMO_NAME = "shopping_cart"
DEMO_TEMPLATE = Path(__file__).resolve().parents[3] / "examples" / "code_quest_demo"
NOT_EVIDENCE = ("skipped", "xfailed")  # no verdict on the code: not part of the fight, but never a pass
TRACED = {
    EventType.VOICE_TRANSCRIBED: "user_said",
    EventType.DIALOGUE_REPLY: "model_reply",
    EventType.ACTION_REQUESTED: "tool_requested",
    EventType.ACTION_RESOLVED: "tool_resolved",
    EventType.VOICE_TTS_FINISHED: "locki_spoke",
    EventType.ROBOT_EXPRESSION: "locki_expression",
    EventType.VISION_PHONE_REACH_CONFIRMED: "camera_reach",
    EventType.SYSTEM_ERROR: "error",
}


def failure_summary(message: str, limit: int = 180) -> str:
    """The assertion line of a failure, for speech and for the model."""
    lines = [ln[1:].strip() for ln in (message or "").splitlines() if ln.startswith("E ")]
    text = next((ln for ln in lines if ln and not ln.startswith("+")), None) or next((ln.strip() for ln in reversed((message or "").splitlines()) if ln.strip()), "")
    return redact(text)[:limit]


def _run_summary(run: TestRun) -> dict:
    return {"status": run.status, "counts": run.counts(), "duration_s": run.duration_s, "exit_code": run.exit_code}


def _short_checkpoint(title: str) -> str:
    return title.removeprefix("Fix ").removeprefix("fix ")


class MissionService:
    def __init__(self, rt):
        self.rt = rt
        self.dir: Path = rt.settings.data_dir / "missions"
        self.quests_dir: Path = rt.settings.data_dir / "quests"
        self.registry = ProjectRegistry(self.dir / "approved_projects.json")
        self.mission: Mission | None = None
        self.workspace: Workspace | None = None
        self.trace: MissionTrace | None = None
        self.last_run: TestRun | None = None
        self.runs: deque[dict] = deque(maxlen=12)
        self.edits: dict[str, dict] = {}
        self.help_note: dict | None = None
        self.busy: str | None = None  # planning | testing | helping | proposing
        self.error: str | None = None
        self.xp_total = self._load_xp()
        self._lock = asyncio.Lock()
        self._signature: str | None = None
        self._last_scan = 0.0
        self._dirty_at: float | None = None
        self._last_auto_run = 0.0
        self._scan_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._help_task: asyncio.Task | None = None
        self._last_change: tuple[str, float] | None = None
        self._own_session = False
        self._stuck_since: float | None = None
        self.relay: RelayRecorder | None = None
        self._plan_stats: dict | None = None
        self._restored = False
        rt.bus.add_listener(self._on_event)
        rt.tool_gate.register("run_pytest_in_sandbox", "sandbox")
        rt.tool_gate.register("read_file", "read", self._read_file_check)
        rt.tool_gate.register("read_test_output", "read")

    # ------------------------------------------------------------------ small facts
    @property
    def active(self) -> bool:
        return self.mission is not None and self.mission.status == "active"

    def _model_name(self) -> str:
        llm = self.rt.companion.llm
        return str(getattr(llm, "model", None) or "none") if self.rt.companion.available else "none"

    def _load_xp(self) -> int:
        try:
            return int(json.loads((self.dir / "profile.json").read_text()).get("xp_total", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0

    def _save(self) -> None:
        m = self.mission
        if m is None:
            return
        folder = self.dir / m.id
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "mission.json.tmp"
        tmp.write_text(json.dumps(m.to_dict(), indent=1))
        os.replace(tmp, folder / "mission.json")
        (self.dir / "profile.json").write_text(json.dumps({"xp_total": self.xp_total}))

    def _publish(self, event: EventType, payload: dict) -> None:
        self.rt.bus.publish(event, EventSource.MISSION, payload)

    def _record(self, record_kind: str, /, **data) -> None:
        if self.trace is not None:
            self.trace.record(record_kind, mission=None if self.mission is None else self.mission.id, **data)
        if self.relay is not None:
            self.relay.mark(record_kind, {k: v for k, v in data.items() if isinstance(v, (int, float, bool)) or k in ("status", "source", "category", "origin", "model", "plan_source")})

    async def _relay_start(self, mission_id: str) -> None:
        if not self.rt.settings.relay_enabled:
            return
        self.relay = RelayRecorder(guardrail=self.rt.tool_gate.guardrail)
        await self.relay.start(self.dir / mission_id / "relay", mission_id)
        comp = self.rt.companion
        if self.relay.active and comp.dialogue is not None:
            comp.dialogue.llm = RelayedLLM(comp.llm, self.relay)

    async def _relay_stop(self) -> None:
        comp = self.rt.companion
        if comp.dialogue is not None and isinstance(comp.dialogue.llm, RelayedLLM):
            comp.dialogue.llm = comp.llm
        if self.relay is not None:
            await self.relay.stop()

    async def relay_tool(self, name: str, args: dict, fn, summary=None):
        """Every tool of this managed agent goes through the protected tool gate, and Relay records it when it is on."""
        outcome = await self.rt.tool_gate.execute(name, args, fn, relay=self.relay, summary=summary, context={"mission_id": None if self.mission is None else self.mission.id})
        if not outcome.allowed:
            raise PolicyError(outcome.decision.reason or "that tool is not allowed")
        if outcome.error is not None:
            raise outcome.error
        return outcome.value

    def _read_file_check(self, args: dict) -> str | None:
        """Refused before the tool runs, not after: outside the project, secret-looking and closed folders."""
        if self.workspace is None:
            return "no mission is running"
        try:
            self.workspace.resolve(str(args.get("path") or ""))
        except PolicyError as exc:
            return str(exc)
        return None

    def _slots(self) -> dict:
        m = self.mission
        if m is None:
            return {}
        cp = m.active_checkpoint()
        return {
            "mission": m.title, "hp": m.hp, "hp_max": m.hp_max, "xp": m.xp, "tests": len(m.tracked()),
            "checkpoint": _short_checkpoint(cp.title) if cp else None,
            "failing": sum(1 for t in cp.tests if (self.last_run is None or self.last_run.outcomes.get(t) != "passed")) if cp else None,
        }

    def numbers(self) -> set[int]:
        """Numbers a generated line may state about the mission."""
        m = self.mission
        if m is None:
            return set()
        out = {m.hp, m.hp_max, m.xp, m.runs, len(m.tracked()), len(m.checkpoints)}
        if self.last_run is not None:
            out |= set(self.last_run.counts().values())
        cp = m.active_checkpoint()
        if cp is not None:  # what the failing test itself reported ("assert 850 == 1300")
            for message in cp.last_failures.values():
                out |= {int(n) for n in re.findall(r"\b\d{1,6}\b", failure_summary(message))}
        return out

    def status_line(self, origin: str = "voice") -> str:
        """Where things stand, from the record. Instant, and the same whoever asks."""
        rt = self.rt
        slots = {k: v for k, v in self._slots().items() if v is not None}
        m = self.mission
        if m is None:
            return rt._say("mission_none")
        if m.status == "won":
            return rt._say("boss_beaten", **slots)
        return rt._say("mission_status" if m.active_checkpoint() is not None else "mission_no_boss", **slots)

    def progress_claim_ok(self) -> bool:
        """Whether a line may say the tests pass or the boss fell: only once they have."""
        return self.mission is None or self.mission.status != "active"

    def next_objective(self) -> str:
        m = self.mission
        if m is None:
            return "Tell me what you are building"
        if m.status == "won":
            return "Boss beaten"
        if m.status == "abandoned":
            return "Mission dropped"
        if m.flagged_tests:
            return "A test file changed: accept it or put it back"
        cp = m.active_checkpoint()
        if cp is None:
            return "Write a test that fails for what you are building"
        failing = next((t for t in cp.tests if self.last_run is None or self.last_run.outcomes.get(t) != "passed"), cp.tests[0])
        return f"Make '{humanize(failing)}' pass"

    # ------------------------------------------------------------------ projects
    def prepare_demo(self, reset: bool = False) -> str:
        """A working copy of the intentionally failing project. ``reset`` puts the bugs back."""
        target = self.quests_dir / DEMO_NAME
        if reset and target.exists():
            shutil.rmtree(target)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(DEMO_TEMPLATE, target, ignore=shutil.ignore_patterns("__pycache__"))
        return str(target.resolve())

    def _default_project(self) -> str:
        approved = self.registry.all()
        return approved[-1] if approved else self.prepare_demo()

    def _project_allowed(self, folder: str) -> bool:
        resolved = str(Path(folder).expanduser().resolve())
        return self.registry.approved(resolved) or resolved == str((self.quests_dir / DEMO_NAME).resolve())

    def approve_project(self, folder: str, origin: str = "operator") -> str:
        path = self.registry.approve(folder)
        self.rt.bus.publish(EventType.MISSION_PROJECT_APPROVED, EventSource.OPERATOR, {"path": redact(path), "origin": origin})
        return path

    # ------------------------------------------------------------------ start and end
    async def start(self, objective: str, project: str | None = None, origin: str = "operator", speak: bool = True):
        rt = self.rt
        objective = re.sub(r"\s+", " ", redact(objective or "")).strip()[:300]
        if not objective:
            return rt._result(False, "a mission needs an objective")
        if self.active:
            return rt._result(False, f"a mission is already running: {self.mission.title}", rt._say("mission_already", **self._slots()) if speak else None)
        if self.busy == "planning":
            return rt._result(False, "a mission is being planned")
        folder = project or self._default_project()
        if not self._project_allowed(folder):
            return rt._result(False, "that folder has not been approved: approve it on the dashboard first")
        self.busy, self.error = "planning", None
        try:
            if speak:
                rt._say("mission_planning")
            return await self._start(objective, Path(folder).expanduser().resolve(), origin, speak)
        finally:
            self.busy = None

    async def _start(self, objective: str, folder: Path, origin: str, speak: bool):
        rt = self.rt
        t0 = time.monotonic()
        ws = Workspace(folder)
        hashes = await asyncio.to_thread(ws.test_hashes)
        mission_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        await self._relay_stop()
        await self._relay_start(mission_id)
        baseline = await self.relay_tool("run_pytest_in_sandbox", {"trigger": "baseline"}, lambda: run_pytest(folder, trigger="baseline"), _run_summary)
        if baseline.status not in ("ok", "no_tests"):
            await self._relay_stop()
            why = baseline.error or (failure_summary(baseline.collect_errors[0]["message"]) if baseline.collect_errors else baseline.status)
            self.error = f"could not run the tests: {why}"
            self._publish(EventType.MISSION_REFUSED, {"reason": self.error, "project": redact(str(folder))})
            reply = rt._say("mission_tests_wont_run") if speak else None
            return rt._result(False, self.error, reply)
        failing = [t for t, o in baseline.outcomes.items() if o != "passed" and o not in NOT_EVIDENCE]
        plan = await self._plan(objective, failing, folder)
        self.mission = new_mission(
            mission_id=mission_id, objective=objective, title=plan.title, project=str(folder), now=time.time(), model=self._model_name(),
            plan_source=plan.source, groups=plan.groups, baseline=baseline, test_hashes=hashes,
        )
        self.workspace, self.last_run = ws, baseline
        self.trace = MissionTrace(self.dir / mission_id / "trace.jsonl")
        self.runs.clear()
        self.edits.clear()
        self.help_note = None
        self._stuck_since = None
        self._signature = await asyncio.to_thread(ws.signature)
        self._dirty_at = None
        self._own_session = False
        if not rt.sessions.active:
            r = await rt.start_session(task=plan.title, origin="mission", speak=False)
            self._own_session = r.ok
        else:
            rt.sessions.set_task(plan.title)
        rt.companion.context.set_goal(short_title(objective, 80), "user_said")
        m = self.mission
        self._note_run(baseline, [])
        self._save()
        self._record("mission_created", objective=objective, origin=origin, title=m.title, model=m.model, plan_source=m.plan_source, problems=plan.problems,
                     project=str(folder), criteria=m.criteria, checkpoints=[{"title": c.title, "tests": c.tests} for c in m.checkpoints], guard_tests=m.guard_tests,
                     hp=m.hp, start_ms=round((time.monotonic() - t0) * 1000, 1), plan_generation=self._plan_stats)
        self._publish(EventType.MISSION_STARTED, {"id": m.id, "title": m.title, "objective": m.objective, "hp": m.hp, "plan_source": m.plan_source, "problems": plan.problems,
                                                  "checkpoints": [c.title for c in m.checkpoints], "origin": origin})
        reply = None
        if speak:
            reply = self._spoken_plan(plan) or rt._say("mission_start" if m.checkpoints else "mission_no_boss", **self._slots())
        self._last_change = ("mission_start", time.monotonic())
        return rt._result(True, f"mission started: {m.title}, {m.hp} failing tests", reply, mission=self.snapshot())

    async def _plan(self, objective: str, failing: list[str], folder: Path) -> Plan:
        if not failing:
            return Plan(short_title(objective), [], "rule")
        comp = self.rt.companion
        if not comp.available:
            return rule_plan(objective, failing)
        name = self._model_name()
        prompt = plan_prompt(objective, failing, folder.name)
        ask = lambda: comp.llm.generate(prompt, max_tokens=PLAN_MAX_TOKENS, temperature=0.3, timeout_s=PLAN_TIMEOUT_S)  # noqa: E731
        gen = await safe_generate(lambda: self.relay.llm(name, {"context_chars": len(prompt), "purpose": "plan"}, ask) if self.relay is not None else ask())
        if not gen.ok:
            plan = rule_plan(objective, failing)
            plan.problems = [f"model: {gen.finish}" + (f" ({gen.error})" if gen.error else "")]
        else:
            plan = parse_model_plan(gen.text, objective, failing, name)
        self._plan_stats = {"model": name, "total_ms": gen.total_ms, "first_token_ms": gen.first_token_ms, "gen_tokens": gen.gen_tokens, "finish": gen.finish}
        return plan

    def _spoken_plan(self, plan: Plan) -> str | None:
        """The model's own line about the plan, if the same guards as any other reply let it through."""
        if not plan.say or plan.source == "rule":
            return None
        from ..intelligence.dialogue import check_reply

        rt = self.rt
        verdict = check_reply(plan.say, facts=rt.facts(), style=rt.style(), extra_numbers=self.numbers(), progress_claim_ok=False)
        if not verdict.ok:
            self._record("model_line_refused", text=plan.say, reason=verdict.reason)
            return None
        self._speak_generated(verdict.text)
        return verdict.text

    def _speak_generated(self, text: str) -> None:
        """A line a model wrote and the guards passed: spoken and recorded like any other reply."""
        rt = self.rt
        audible = rt.speaker.say(text, priority=2, category="generated")
        rt.last_reply = {"text": text, "category": "generated", "at": time.time(), "audible": audible, "priority": "P2"}
        rt.bus.publish(EventType.VOICE_RESPONSE_GENERATED, EventSource.VOICE, {"category": "generated", "text": text, "source": "local language model", "audible": audible})

    async def abandon(self, origin: str = "operator", speak: bool = True):
        rt = self.rt
        m = self.mission
        if not self.active:
            return rt._result(False, "no mission is running", rt._say("mission_none") if speak else None)
        m.status, m.finished_at = "abandoned", time.time()
        self._save()
        self._record("mission_abandoned", origin=origin, hp=m.hp, xp=m.xp)
        self._publish(EventType.MISSION_ABANDONED, {"id": m.id, "title": m.title, "hp": m.hp, "origin": origin})
        reply = rt._say("mission_ended") if speak else None
        await self._relay_stop()
        if self._own_session and rt.sessions.active:
            await rt.end_session(reason="user", origin="mission", speak=False)
        return rt._result(True, f"mission dropped: {m.title}", reply)

    def restore(self) -> None:
        """Carry on with a mission that was running when the backend stopped."""
        if not self.dir.exists():
            return
        latest = None
        for path in sorted(self.dir.glob("*/mission.json"), reverse=True):
            try:
                data = json.loads(path.read_text())
                if data.get("status") == "active":
                    latest = Mission.from_dict(data)
                break  # only the newest counts: older ones were finished or replaced
            except (OSError, ValueError, TypeError):
                continue
        if latest is None or not self._project_allowed(latest.project) or not Path(latest.project).is_dir():
            return
        self.mission = latest
        self.workspace = Workspace(latest.project)
        self.trace = MissionTrace(self.dir / latest.id / "trace.jsonl")
        self._record("mission_restored", model=self._model_name())
        self._dirty_at = time.monotonic()
        self._restored = True

    # ------------------------------------------------------------------ evidence
    async def run_tests(self, trigger: str = "manual", code_changed: bool = False, speak_always: bool = False) -> TestRun | None:
        m = self.mission
        if not self.active or self.workspace is None:
            return None
        async with self._lock:
            self.busy = "testing"
            self._publish(EventType.MISSION_TESTS_STARTED, {"id": m.id, "trigger": trigger})
            try:
                run = await self.relay_tool("run_pytest_in_sandbox", {"trigger": trigger}, lambda: run_pytest(self.workspace.root, trigger=trigger), _run_summary)
                now_hashes = await asyncio.to_thread(self.workspace.test_hashes)
            except PolicyError as exc:  # the protected tool gate refused: during an incident, for example. Nothing about the mission changes.
                self._publish(EventType.MISSION_REFUSED, {"reason": str(exc)})
                if speak_always:
                    self.rt._say("mission_tool_refused", reason=str(exc).rstrip("."))
                return None
            finally:
                self.busy = None
            xp_before = m.xp
            changes = apply_run(m, run, tests_changed=changed_tests(m.test_hashes, now_hashes), code_changed=code_changed, now=time.time())
            self.xp_total += m.xp - xp_before
            self.last_run = run
            self._note_run(run, changes)
            self._save()
            self._record("verification", run=run.to_payload(), changes=[{"kind": c.kind, **c.data} for c in changes], hp=m.hp, xp=m.xp, status=m.status)
            self._publish(EventType.MISSION_TEST_RUN, {"id": m.id, "trigger": trigger, "status": run.status, "counts": run.counts(), "exit_code": run.exit_code,
                                                       "duration_s": run.duration_s, "changes": [c.kind for c in changes], "hp": m.hp, "hp_max": m.hp_max, "xp": m.xp})
            await self._react(changes, run, speak_always)
            return run

    def _note_run(self, run: TestRun, changes: list[Change]) -> None:
        self.runs.appendleft({"id": run.run_id, "at": run.at, "trigger": run.trigger, "status": run.status, "counts": run.counts(), "duration_s": run.duration_s,
                              "changes": [c.kind for c in changes], "error": run.error})

    def accept_test_changes(self, origin: str = "operator"):
        rt = self.rt
        m = self.mission
        if not self.active or self.workspace is None:
            return rt._result(False, "no mission is running")
        if not m.flagged_tests:
            return rt._result(False, "no test file is waiting to be accepted")
        accepted = list(m.flagged_tests)
        m.test_hashes = self.workspace.test_hashes()
        m.flagged_tests = []
        m.drop_missing = True
        self._save()
        self._record("tests_accepted", origin=origin, files=accepted)
        self._publish(EventType.MISSION_TESTS_ACCEPTED, {"id": m.id, "files": accepted, "origin": origin})
        self._dirty_at = time.monotonic() - DEBOUNCE_S
        return rt._result(True, f"accepted changes to {', '.join(accepted)}: the next run counts")

    async def _react(self, changes: list[Change], run: TestRun, speak_always: bool = False) -> None:
        rt = self.rt
        m = self.mission
        kinds = {c.kind: c for c in changes}
        cat = None
        slots = self._slots()
        quiet = rt.companion.context.quiet() or rt.sessions.paused
        if "won" in kinds:
            cat = "boss_beaten"
        elif "regression" in kinds:
            cat, slots["done"] = "mission_regression", _short_checkpoint(kinds["regression"].data["title"])
        elif "checkpoint_done" in kinds:
            cat, slots["done"] = "checkpoint_done", _short_checkpoint(kinds["checkpoint_done"].data["title"])
        elif "boss_hit" in kinds:
            cat, slots["damage"] = "boss_hit", kinds["boss_hit"].data["damage"]
        elif "tests_edited" in kinds:
            cat = "tests_edited"
        elif "inconclusive" in kinds:
            cat = {"broken": "code_broken", "timeout": "tests_timeout"}.get(run.status, "mission_tests_wont_run")
        elif "boss_healed" in kinds:
            cat = "boss_healed"
        elif speak_always and m.status == "active":
            cat = "mission_status" if m.active_checkpoint() is not None else "mission_no_boss"
        if cat is not None:
            self._last_change = (cat, time.monotonic())
        if "won" in kinds or "checkpoint_done" in kinds:
            self.help_note = None
            self._stuck_since = None
        if "won" in kinds:
            rt._say("boss_beaten", force=True, **slots)  # earned: said even in quiet mode
            self._publish(EventType.MISSION_WON, {"id": m.id, "title": m.title, "xp": m.xp, "runs": m.runs})
            self._record("mission_won", xp=m.xp, runs=m.runs, seconds=round(time.time() - m.created_at, 1))
            await self._relay_stop()
            if self._own_session and rt.sessions.active:
                await rt.end_session(reason="completed", origin="mission", speak=False)
            return
        if cat is not None and (not quiet or speak_always):
            rt._say(cat, **{k: v for k, v in slots.items() if v is not None})
        if "stuck" in kinds and not quiet:
            self._stuck_since = time.monotonic()
            if self._help_task is None or self._help_task.done():
                self._help_task = asyncio.create_task(self.help(origin="stuck", times=kinds["stuck"].data.get("times")), name="mission-help")

    # ------------------------------------------------------------------ the watcher
    def tick(self) -> None:
        if not self.active or self.workspace is None:
            return
        if self._restored:
            self._restored = False
            if self.rt.settings.relay_enabled:
                asyncio.create_task(self._relay_start(self.mission.id), name="mission-relay")
        now = time.monotonic()
        if (self._scan_task is None or self._scan_task.done()) and now - self._last_scan >= SCAN_EVERY_S:
            self._last_scan = now
            self._scan_task = asyncio.create_task(self._scan(), name="mission-scan")
        if self._dirty_at is None or now - self._dirty_at < DEBOUNCE_S or self.rt.sessions.paused:
            return
        if self._lock.locked() or now - self._last_auto_run < MIN_AUTO_RUN_GAP_S:
            return
        self._dirty_at = None
        self._last_auto_run = now
        self._run_task = asyncio.create_task(self.run_tests("you saved a file", code_changed=True), name="mission-tests")

    async def _scan(self) -> None:
        ws = self.workspace
        if ws is None:
            return
        try:
            signature = await asyncio.to_thread(ws.signature)
        except OSError:
            return
        if signature != self._signature:
            first = self._signature is None
            self._signature = signature
            if not first:
                self._dirty_at = time.monotonic()

    # ------------------------------------------------------------------ tools the model may ask for
    def tool_read_file(self, path: str) -> str:
        if self.workspace is None:
            raise PolicyError("no mission is running")
        text = self.workspace.read_text(path)
        self._record("file_read", path=path, chars=len(text))
        return text[:MAX_TOOL_OUTPUT] + ("\n[cut: the file goes on]" if len(text) > MAX_TOOL_OUTPUT else "")

    def tool_test_output(self, about: str | None = None) -> str:
        m = self.mission
        if m is None or self.last_run is None:
            raise PolicyError("no test run yet")
        named = self._failing_about(about)
        cp = named[0] if named else m.active_checkpoint()
        if cp is None:
            return "No failing checkpoint. " + (redact(self.last_run.output_tail[-400:]))
        failures = [f"{t}:\n{redact(self.last_run.messages.get(t, cp.last_failures.get(t, 'no output')))}" for t in cp.tests if self.last_run.outcomes.get(t) != "passed"]
        return ("\n\n".join(failures) or "These tests passed in the last run.")[:MAX_TOOL_OUTPUT]

    def _failing_about(self, text: str | None) -> tuple[Checkpoint, str] | None:
        """The failing test these words are about, if they point at one: "why is shipping failing" while subtotal is the
        current checkpoint. None means they name no test, so the current checkpoint is what they mean."""
        m = self.mission
        if m is None or not text or self.last_run is None:
            return None
        words = {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _NOT_ABOUT_A_TEST}
        best, best_score = None, 0
        for cp in m.checkpoints:
            if cp.status == "done":
                continue
            for test in cp.tests:
                if self.last_run.outcomes.get(test) == "passed":
                    continue
                name = set(re.findall(r"[a-z]{3,}", humanize(test).lower()))
                score = sum(1 for w in words if any(n.startswith(w) or w.startswith(n) for n in name if min(len(n), len(w)) >= 4))
                if score > best_score:
                    best, best_score = (cp, test), score
        return best

    def tool_files(self) -> list[str]:
        return [] if self.workspace is None else self.workspace.files()[:80]

    def file_named(self, text: str) -> FileMention | None:
        """The file a person's words name while a mission runs. For "the test file": the one the current checkpoint is failing in."""
        if not self.active or self.workspace is None:
            return None
        cp = self.mission.active_checkpoint()
        return self.workspace.mentioned_file(text, test_file=cp.tests[0].split("::", 1)[0] if cp is not None and cp.tests else None)

    def _relevant_source(self, test: str, message: str) -> str:
        """The functions the failure names, from files that are not tests."""
        ws = self.workspace
        if ws is None:
            return ""
        names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", message + " " + test.rsplit("::", 1)[-1].replace("test_", " ").replace("_", " ")))
        chunks = []
        for rel in ws.files():
            if not rel.endswith(".py") or is_test_file(rel):
                continue
            try:
                lines = ws.read_text(rel).splitlines()
            except PolicyError:
                continue
            for i, line in enumerate(lines):
                m = re.match(r"\s*def ([A-Za-z_][A-Za-z0-9_]*)", line)
                if m and m.group(1) in names:
                    indent = len(line) - len(line.lstrip())
                    end = i + 1
                    while end < len(lines) and end - i < 25 and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
                        end += 1
                    chunks.append(f"{rel}:\n" + "\n".join(lines[i:end]).rstrip())
        return "\n\n".join(chunks)[:1400]

    def _failure_detail(self, about: tuple[Checkpoint, str] | None = None) -> tuple[str, str, str] | None:
        m = self.mission
        cp = about[0] if about else (None if m is None else m.active_checkpoint())
        if cp is None:
            return None
        outcomes = self.last_run.outcomes if self.last_run is not None else {}
        failing = about[1] if about else next((t for t in cp.tests if outcomes.get(t) != "passed"), cp.tests[0])
        message = (self.last_run.messages.get(failing) if self.last_run is not None else None) or cp.last_failures.get(failing, "")
        return failing, redact(message), self._relevant_source(failing, message)

    async def help(self, origin: str = "voice", user_text: str | None = None, times: int | None = None):
        """One concrete hint about the current failure: from the model if it can give a checked one,
        else what the test itself says. Never a claim that anything passes."""
        rt = self.rt
        about = self._failing_about(user_text)
        detail = self._failure_detail(about)
        if detail is None:
            return rt._result(False, "nothing is failing right now", rt._say("mission_status" if self.active else "mission_none", **self._slots()))
        test, message, source = detail
        m = self.mission
        cp = about[0] if about else m.active_checkpoint()
        summary = failure_summary(message)
        self.busy = self.busy or "helping"
        text, kind = None, "rule"
        t0 = time.monotonic()
        comp = rt.companion
        try:
            if comp.available:
                from ..intelligence.dialogue import numbers_in

                block = (
                    f"\n\nHELP WITH A FAILURE. Checkpoint: {cp.title}. Failing test: {humanize(test)}.\nWHAT THE TEST REPORTED:\n{message[-700:]}\n"
                    + (f"THE CODE IT RUNS:\n{source}\n" if source else "")
                    + "Point at the actual bug in one or two short sentences, in your own words. Name the function. Do not write out the full fix. "
                    "Do not say that anything passes or is fixed. Action none."
                )
                context = comp.render(allowed=[], user_text=user_text, event=None if user_text else "the user keeps failing this checkpoint") + block
                res = await comp.dialogue.respond(
                    context, allowed=[], facts=rt.facts(), style=rt.style(), user_text=user_text or "", proactive=not user_text, reach_recorded=comp.context.count("reach_confirmed") > 0,
                    recent_lines=comp.context.recent_robot_lines(), extra_numbers=self.numbers() | numbers_in(message), timeout_s=HELP_TIMEOUT_S, camera_on=comp._watching(), progress_claim_ok=False,
                )
                comp._log_reply(user_text or "", res, kind="generated" if res.say else "fallback", spoken=res.say, event="mission_help")
                if res.say:
                    text, kind = res.say, self._model_name()
                    self._speak_generated(text)
        finally:
            if self.busy == "helping":
                self.busy = None
        if text is None:
            text = rt._say("mission_stuck" if origin == "stuck" else "mission_hint", checkpoint=_short_checkpoint(cp.title), times=times or cp.fail_streak, summary=summary)
        self.help_note = {"text": text, "test": test, "summary": summary, "source": kind, "checkpoint": cp.id, "at": time.time(), "origin": origin}
        self._record("help", origin=origin, test=test, text=text, source=kind, ms=round((time.monotonic() - t0) * 1000, 1))
        self._publish(EventType.MISSION_HELP, {"text": text, "test": test, "source": kind, "origin": origin})
        self._last_change = ("help", time.monotonic())
        return rt._result(True, f"hint from {kind}", text)

    # ------------------------------------------------------------------ edits: proposed by a model, applied only by a person
    async def propose_edit(self, origin: str = "voice"):
        rt = self.rt
        detail = self._failure_detail()
        if detail is None or self.workspace is None:
            return rt._result(False, "nothing is failing, so there is nothing to change")
        comp = rt.companion
        if not comp.available:
            return rt._result(False, "no language model is running, so there is no suggestion", rt._say("mission_edit_failed"))
        test, message, source = detail
        if not source:
            return rt._result(False, "could not find the code this test runs")
        self.busy = "proposing"
        try:
            prompt = (
                f"CODE CHANGE REQUEST. Failing test: {test}\nWHAT THE TEST REPORTED:\n{message[-700:]}\nTHE CODE:\n{source}\n\n"
                "Propose the smallest change to this code that fixes the failure. Copy the lines to replace exactly, character for character. Never change a test.\n"
                'Answer with one JSON object on one line and nothing else: {"action": "none", "args": {"path": "<file>", "old": "<exact lines to replace>", "new": "<replacement lines>"}, "say": "<one short line saying what it changes>"}'
            )
            ask = lambda: comp.llm.generate(prompt, max_tokens=EDIT_MAX_TOKENS, temperature=0.2, timeout_s=EDIT_TIMEOUT_S)  # noqa: E731
            gen = await safe_generate(lambda: self.relay.llm(self._model_name(), {"context_chars": len(prompt), "purpose": "edit"}, ask) if self.relay is not None else ask())
        finally:
            self.busy = None
        try:
            edit = self._validate_edit(gen.text if gen.ok else "", test)
        except PolicyError as exc:
            self._record("edit_refused", test=test, reason=str(exc), raw=gen.text[:800], finish=gen.finish)
            self._publish(EventType.MISSION_EDIT, {"status": "refused", "reason": str(exc), "test": test})
            return rt._result(False, f"no usable suggestion: {exc}", rt._say("mission_edit_failed"))
        self.edits[edit["id"]] = edit
        self._record("edit_proposed", **{k: edit[k] for k in ("id", "path", "old", "new", "test")}, model=self._model_name(), origin=origin)
        self._publish(EventType.MISSION_EDIT, {"status": "proposed", "id": edit["id"], "path": edit["path"], "test": test})
        return rt._result(True, f"edit proposed for {edit['path']}: waiting for you", rt._say("mission_edit_ready"), edit=edit)

    def _validate_edit(self, text: str, test: str) -> dict:
        data = loads_lenient(text)
        args = data.get("args") if isinstance(data, dict) else None
        if not isinstance(args, dict) or not all(isinstance(args.get(k), str) for k in ("path", "old", "new")):
            raise PolicyError("the model did not write a usable change")
        path, old, new = args["path"].strip(), args["old"], args["new"]
        if "\\n" in old and "\n" not in old:  # newlines escaped twice, another real habit
            old, new = old.replace("\\n", "\n"), new.replace("\\n", "\n")
        if is_test_file(path):
            raise PolicyError("a suggestion may not change a test")
        if not old.strip() or old == new or len(new) > 3000:
            raise PolicyError("the change is empty or too large")
        target = self.workspace.resolve(path)
        current = self.workspace.read_text(path)
        if current.count(old) != 1:
            raise PolicyError("the lines it wants to replace are not in the file exactly once")
        why = data.get("say") or args.get("say") or ""
        return {"id": uuid.uuid4().hex[:8], "path": target.relative_to(self.workspace.root).as_posix(), "old": old, "new": new, "test": test, "why": str(why)[:160],
                "base_sha": hashlib.sha256(target.read_bytes()).hexdigest(), "at": time.time()}

    def decide_edit(self, edit_id: str, approve: bool, origin: str = "operator"):
        rt = self.rt
        edit = self.edits.pop(edit_id, None)
        if edit is None or self.workspace is None:
            return rt._result(False, "no such suggestion")
        if not approve:
            self._record("edit_rejected", id=edit_id, origin=origin)
            self._publish(EventType.MISSION_EDIT, {"status": "rejected", "id": edit_id, "path": edit["path"], "origin": origin})
            return rt._result(True, "suggestion dropped: nothing changed")
        target = self.workspace.resolve(edit["path"])
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != edit["base_sha"]:
            self._record("edit_refused", id=edit_id, reason="the file changed since the suggestion")
            return rt._result(False, "the file changed since the suggestion, so it was not applied")
        text = raw.decode("utf-8")
        tmp = target.with_name(target.name + ".locked-in-tmp")
        tmp.write_text(text.replace(edit["old"], edit["new"], 1), encoding="utf-8")
        os.replace(tmp, target)
        self._record("edit_applied", id=edit_id, path=edit["path"], origin=origin)
        self._publish(EventType.MISSION_EDIT, {"status": "applied", "id": edit_id, "path": edit["path"], "origin": origin})
        self._dirty_at = time.monotonic()  # only the tests can say whether it worked
        return rt._result(True, f"applied to {edit['path']}: the tests decide if it worked")

    # ------------------------------------------------------------------ what the model is told
    def context_block(self) -> str | None:
        """Mission facts for a model turn. None without a mission: other prompts stay exactly as they were."""
        m = self.mission
        if m is None or m.status == "abandoned":
            return None
        if m.status == "won":
            return f"\nMISSION FINISHED, proven by the tests: {m.title}. Boss beaten. {m.xp} XP. Ask what is next."
        cp = m.active_checkpoint()
        outcomes = self.last_run.outcomes if self.last_run is not None else {}
        following = next((c for c in m.checkpoints if c.status != "done" and c is not cp), None)
        steps = [f"{i + 1}. {c.title}: {'DONE' if c.status == 'done' else 'CURRENT' if c is cp else 'NEXT AFTER THE CURRENT ONE' if c is following else 'BROKEN AGAIN' if c.status == 'regressed' else 'later'}" for i, c in enumerate(m.checkpoints)]
        lines = [
            "\nMISSION. These are facts from real test runs and you cannot change them. "
            "Only a test run can finish a checkpoint. Never say a test passes, a checkpoint is done or the boss is beaten unless it says so here.",
            f"OBJECTIVE: {m.objective}",
            f"BOSS: {m.hp} of {m.hp_max} health left, one per failing test. XP: {m.xp}. Test runs: {m.runs}.",
            "CHECKPOINTS, in order:",
            *steps,
        ]
        if cp is not None:
            failing = [t for t in cp.tests if outcomes.get(t) != "passed"]
            lines.append(f"NEXT: {self.next_objective()}.")
            lines += [f"FAILING NOW: {humanize(t)}: {failure_summary(cp.last_failures.get(t, ''), 120)}" for t in failing[:3]]
        else:
            lines.append("NEXT: the user needs a test that fails for what they are building.")
        if m.flagged_tests:
            lines.append(f"TEST FILES EDITED, NOT COUNTED until the user accepts on the dashboard: {', '.join(m.flagged_tests)}")
        lines.append("PROJECT FILES: " + ", ".join(self.tool_files()[:25]))
        lines.append(
            "MISSION TOOLS, used as the action when they are what the user needs:\n"
            "- run_tests: run the project's tests now. no args.\n"
            "- read_file: read a project file before talking about its code. args: path.\n"
            "- read_test_output: read the full failure before explaining it. no args.\n"
            "- propose_edit: the user asks you to write the fix. It goes on the dashboard and changes nothing until they approve it. no args.\n"
            "- abandon_mission: the user wants to drop the whole mission. no args.\n"
            "A question you can answer from this block: action none. Asked to do something you have no tool for (git, installing, email, "
            "deleting or skipping a test): action none, and say you can't do that. Told something is fixed, or asked for credit: action none, "
            "and say only a test run can prove it. release_phone only when the user mentions the phone."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ records and dashboard
    def _on_event(self, ev) -> None:
        kind = TRACED.get(ev.event_type)
        if kind is None or self.trace is None or self.mission is None or self.mission.status != "active":
            return
        p = ev.payload or {}
        keep = {k: p.get(k) for k in ("text", "source", "category", "outcome", "name", "args", "status", "reason", "origin", "say", "problems", "requested", "spoken", "ms", "llm", "user", "event", "latency_ms", "code", "message") if p.get(k) is not None}
        if p.get("kind") is not None:
            keep["reply_kind"] = p["kind"]
        self._record(kind, simulated=ev.simulated, **keep)

    def _mood(self) -> str:
        m = self.mission
        if m is None or m.status == "abandoned":
            return "idle"
        if m.status == "won":
            return "celebrating"
        if self.busy in ("testing", "planning"):
            return "watching"
        if self.busy in ("helping", "proposing"):
            return "thinking"
        if m.flagged_tests:
            return "suspicious"
        if self.last_run is not None and not self.last_run.conclusive:
            return "alarmed"
        if self._last_change is not None and time.monotonic() - self._last_change[1] < REACTION_MOOD_S:
            kind = self._last_change[0]
            if kind in ("boss_hit", "checkpoint_done"):
                return "hyped"
            if kind in ("mission_regression", "boss_healed"):
                return "worried"
            if kind == "help":
                return "helping"
        if self._stuck_since is not None:
            return "concerned"
        return "focused" if m.checkpoints else "waiting"

    def _character(self) -> dict:
        rt = self.rt
        last = rt.last_reply or {}
        return {"name": "LOCKI", "mood": self._mood(), "line": last.get("text"), "line_at": last.get("at")}

    def snapshot(self) -> dict:
        m = self.mission
        data = None
        if m is not None:
            outcomes = self.last_run.outcomes if self.last_run is not None else {}
            active = m.active_checkpoint()
            data = {
                "id": m.id, "objective": m.objective, "title": m.title, "project": redact(m.project), "status": m.status, "hp": m.hp, "hp_max": m.hp_max, "xp": m.xp, "runs": m.runs,
                "model": m.model, "plan_source": m.plan_source, "criteria": m.criteria, "next_objective": self.next_objective(), "flagged_tests": m.flagged_tests, "created_at": m.created_at,
                "removed_tests": len(m.removed_tests), "guard_tests": len(m.guard_tests), "guards_failing": sum(outcomes.get(t) != "passed" for t in m.guard_tests) if outcomes else 0,
                "checkpoints": [
                    {"id": c.id, "title": c.title, "status": c.status, "current": c is active, "fail_streak": c.fail_streak,
                     "tests": [{"id": t, "name": humanize(t), "outcome": outcomes.get(t, "unknown"), "summary": failure_summary(c.last_failures.get(t, ""), 140) if outcomes.get(t) != "passed" else None} for t in c.tests]}
                    for c in m.checkpoints
                ],
            }
        run = self.last_run
        return {
            "mission": data,
            "busy": self.busy,
            "error": self.error,
            "character": self._character(),
            "xp_total": self.xp_total,
            "projects": [redact(p) for p in self.registry.all()],
            "demo_project": redact(str(self.quests_dir / DEMO_NAME)),
            "demo_ready": (self.quests_dir / DEMO_NAME).exists(),
            "last_run": None if run is None else {"status": run.status, "at": run.at, "trigger": run.trigger, "counts": run.counts(), "duration_s": run.duration_s, "error": run.error, "command": run.command[:3], "tail": redact(run.output_tail[-500:]) if not run.conclusive else None},
            "runs": list(self.runs),
            "help": self.help_note,
            "edits": list(self.edits.values()),
            "watching": self.active and self.workspace is not None and not self.rt.sessions.paused,
            "trace": None if self.trace is None else {"path": redact(str(self.trace.path)), "events": self.trace.count},
            "relay": {
                "enabled": self.rt.settings.relay_enabled, "installed": relay_installed(), "active": self.relay is not None and self.relay.active,
                "events_path": None if self.relay is None or self.relay.events_path is None else redact(str(self.relay.events_path)), "error": None if self.relay is None else self.relay.error,
            },
        }

    async def close(self) -> None:
        for task in (self._scan_task, self._run_task, self._help_task):
            if task is not None and not task.done():
                task.cancel()
        await self._relay_stop()
