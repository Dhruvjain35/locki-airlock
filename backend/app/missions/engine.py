"""The mission's rules. Pure: state goes in, a test run goes in, changes come out. No model, no
files, no clock of its own, so what counts as progress can be read and tested on its own.

Only ``apply_run`` moves a mission, and only a conclusive run with unedited tests moves it. There is
no way to say a checkpoint is done: it is done when every test in it reported "passed".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

XP_TEST = 10
XP_CHECKPOINT = 50
XP_BOSS = 100
STUCK_AFTER = 3  # edits in a row that moved nothing on the current checkpoint


@dataclass
class Checkpoint:
    id: str
    title: str
    tests: list[str]
    status: str = "pending"  # pending | done | regressed
    fail_streak: int = 0
    done_at: float | None = None
    last_failures: dict[str, str] = field(default_factory=dict)


@dataclass
class Mission:
    id: str
    objective: str
    title: str
    project: str
    created_at: float
    model: str
    plan_source: str  # the model's name, or "rule" when the fixed grouping was used
    criteria: list[str] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    guard_tests: list[str] = field(default_factory=list)  # passed at the start and must keep passing
    test_hashes: dict[str, str] = field(default_factory=dict)
    status: str = "active"  # active | won | abandoned
    hp: int = 0
    hp_max: int = 0
    xp: int = 0
    ever_passed: list[str] = field(default_factory=list)
    runs: int = 0
    flagged_tests: list[str] = field(default_factory=list)  # edited test files waiting for the user
    drop_missing: bool = False  # the user accepted a test change: tests it removed stop counting
    removed_tests: list[str] = field(default_factory=list)
    finished_at: float | None = None

    def tracked(self) -> list[str]:
        return [t for c in self.checkpoints for t in c.tests] + self.guard_tests

    def active_checkpoint(self) -> Checkpoint | None:
        return next((c for c in self.checkpoints if c.status != "done"), None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Mission:
        d = dict(d)
        d["checkpoints"] = [Checkpoint(**c) for c in d.get("checkpoints", [])]
        return cls(**d)


@dataclass
class Change:
    kind: str  # test_passed | checkpoint_done | regression | boss_hit | boss_healed | won | stuck | tests_edited | inconclusive | new_tests
    data: dict = field(default_factory=dict)


def _failing_message(run, test: str) -> str:
    return run.messages.get(test) or f"{test}: {run.outcomes.get(test, 'did not run')}"


def _hp(m: Mission, outcomes: dict[str, str]) -> int:
    return sum(outcomes.get(t) != "passed" for t in m.tracked())


def new_mission(
    *, mission_id: str, objective: str, title: str, project: str, now: float, model: str, plan_source: str, groups: list[tuple[str, list[str]]], baseline, test_hashes: dict[str, str]
) -> Mission:
    """``groups``: (checkpoint title, failing test ids), already validated against ``baseline``."""
    planned = {t for _, tests in groups for t in tests}
    guards = [t for t, o in baseline.outcomes.items() if o == "passed" and t not in planned]
    m = Mission(
        id=mission_id, objective=objective, title=title, project=project, created_at=now, model=model, plan_source=plan_source,
        checkpoints=[Checkpoint(f"cp{i + 1}", name, list(tests)) for i, (name, tests) in enumerate(groups)],
        guard_tests=guards, test_hashes=test_hashes, ever_passed=list(guards),
    )
    m.hp = m.hp_max = _hp(m, baseline.outcomes)
    m.runs = 1
    m.criteria = criteria(m)
    active = m.active_checkpoint()
    if active is not None:
        active.last_failures = {t: _failing_message(baseline, t) for t in active.tests if baseline.outcomes.get(t) != "passed"}
    return m


def criteria(m: Mission) -> list[str]:
    n = sum(len(c.tests) for c in m.checkpoints)
    out = [f"All {n} failing tests pass" if n else "Write a test that fails for what you are building, then make it pass"]
    if m.guard_tests:
        out.append(f"The {len(m.guard_tests)} that pass today keep passing")
    out.append("Test files stay as they were, unless you accept a change")
    return out


def apply_run(m: Mission, run, *, tests_changed: list[str], code_changed: bool, now: float) -> list[Change]:
    """What this run proves. ``code_changed``: the run followed an edit, so a miss counts as an attempt."""
    if m.status != "active":
        return []
    m.runs += 1
    active = m.active_checkpoint()
    if not run.conclusive:
        out = [Change("inconclusive", {"status": run.status, "error": run.error, "collect_errors": run.collect_errors[:2]})]
        if run.status == "broken" and code_changed and active is not None:
            active.fail_streak += 1
            if active.fail_streak % STUCK_AFTER == 0:
                out.append(Change("stuck", {"checkpoint": active.id, "times": active.fail_streak}))
        return out
    if tests_changed:
        # Not progress and not a setback: nobody can pass a boss by editing the exam.
        m.flagged_tests = tests_changed
        return [Change("tests_edited", {"files": tests_changed})]
    m.flagged_tests = []
    changes: list[Change] = []
    if m.drop_missing:
        # A person looked at this and said yes. It stays on the record (removed_tests, the trace).
        m.drop_missing = False
        gone = [t for t in m.tracked() if t not in run.outcomes]
        m.removed_tests += gone
        for c in m.checkpoints:
            c.tests = [t for t in c.tests if t in run.outcomes]
        m.checkpoints = [c for c in m.checkpoints if c.tests]
        m.guard_tests = [t for t in m.guard_tests if t in run.outcomes]
        if gone:
            changes.append(Change("tests_removed", {"tests": gone}))
    known = set(m.tracked())
    fresh = [t for t in run.outcomes if t not in known]
    fresh_failing = [t for t in fresh if run.outcomes[t] != "passed"]
    m.guard_tests += [t for t in fresh if run.outcomes[t] == "passed"]
    if fresh_failing:
        added = next((c for c in m.checkpoints if c.id == "added"), None)
        if added is None:
            added = Checkpoint("added", "Tests added during the mission", [])
            m.checkpoints.append(added)
        added.tests += fresh_failing
        added.status = "pending"
        changes.append(Change("new_tests", {"tests": fresh_failing}))
    before = m.hp
    fixed = 0
    for t in m.tracked():
        if run.outcomes.get(t) == "passed" and t not in m.ever_passed:
            m.ever_passed.append(t)
            m.xp += XP_TEST
            fixed += 1
            changes.append(Change("test_passed", {"test": t}))
    for c in m.checkpoints:
        passing = all(run.outcomes.get(t) == "passed" for t in c.tests)
        if passing and c.status != "done":
            first = c.done_at is None
            c.status, c.fail_streak, c.last_failures = "done", 0, {}
            if first:
                c.done_at = now
                m.xp += XP_CHECKPOINT
            changes.append(Change("checkpoint_done", {"checkpoint": c.id, "title": c.title, "first": first}))
        elif not passing:
            if c.status == "done":
                c.status = "regressed"
                changes.append(Change("regression", {"checkpoint": c.id, "title": c.title}))
            c.last_failures = {t: _failing_message(run, t) for t in c.tests if run.outcomes.get(t) != "passed"}
    m.hp = _hp(m, run.outcomes)
    m.hp_max = max(m.hp_max, m.hp)
    if m.hp < before:
        changes.append(Change("boss_hit", {"damage": before - m.hp, "hp": m.hp}))
    elif m.hp > before:
        changes.append(Change("boss_healed", {"healed": m.hp - before, "hp": m.hp}))
    active = m.active_checkpoint()
    if m.checkpoints and active is None and m.hp == 0:
        m.status, m.finished_at = "won", now
        m.xp += XP_BOSS
        changes.append(Change("won", {"xp": m.xp, "tests": len(m.tracked())}))
        return changes
    if active is not None:
        if m.hp < before or fixed:
            active.fail_streak = 0
        elif code_changed:
            active.fail_streak += 1
            if active.fail_streak % STUCK_AFTER == 0:
                changes.append(Change("stuck", {"checkpoint": active.id, "title": active.title, "times": active.fail_streak}))
    return changes
