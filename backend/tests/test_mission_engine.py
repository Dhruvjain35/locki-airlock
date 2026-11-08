"""The rules of progress: only conclusive runs with unedited tests move a mission."""

from __future__ import annotations

from app.missions.engine import STUCK_AFTER, XP_BOSS, XP_CHECKPOINT, XP_TEST, apply_run, new_mission
from app.missions.evidence import TestRun


def run(outcomes: dict[str, str], status: str = "ok", messages: dict | None = None) -> TestRun:
    return TestRun("r", 0.0, "/p", "t", status, 0 if status == "ok" else None, 0.1, outcomes=outcomes, messages=messages or {})


A, B, C, G = "t.py::test_a", "t.py::test_b", "t.py::test_c", "t.py::test_guard"


def mission():
    base = run({A: "failed", B: "failed", C: "failed", G: "passed"}, messages={A: "assert 1 == 2"})
    return new_mission(
        mission_id="m", objective="fix it", title="Fix it", project="/p", now=1.0, model="rule", plan_source="rule",
        groups=[("First", [A, B]), ("Second", [C])], baseline=base, test_hashes={"t.py": "h"},
    )


def apply(m, outcomes, tests_changed=(), code_changed=True, status="ok"):
    return apply_run(m, run(outcomes, status), tests_changed=list(tests_changed), code_changed=code_changed, now=2.0)


def kinds(changes):
    return [c.kind for c in changes]


def test_the_boss_starts_with_one_health_per_failing_test():
    m = mission()
    assert (m.hp, m.hp_max, m.xp, m.status) == (3, 3, 0, "active")
    assert m.guard_tests == [G]
    assert m.active_checkpoint().id == "cp1"
    assert m.active_checkpoint().last_failures[A] == "assert 1 == 2"


def test_a_passing_test_hits_the_boss_and_a_full_checkpoint_pays_out():
    m = mission()
    ch = apply(m, {A: "passed", B: "failed", C: "failed", G: "passed"})
    assert kinds(ch) == ["test_passed", "boss_hit"] and (m.hp, m.xp) == (2, XP_TEST)
    ch = apply(m, {A: "passed", B: "passed", C: "failed", G: "passed"})
    assert "checkpoint_done" in kinds(ch) and m.checkpoints[0].status == "done"
    assert m.xp == 2 * XP_TEST + XP_CHECKPOINT and m.active_checkpoint().id == "cp2"


def test_the_boss_falls_only_when_every_test_including_the_guards_passes():
    m = mission()
    apply(m, {A: "passed", B: "passed", C: "passed", G: "failed"})
    assert m.status == "active" and m.hp == 1
    ch = apply(m, {A: "passed", B: "passed", C: "passed", G: "passed"})
    assert "won" in kinds(ch) and m.status == "won" and m.hp == 0
    assert m.xp == 3 * XP_TEST + 2 * XP_CHECKPOINT + XP_BOSS
    assert apply(m, {A: "failed", B: "passed", C: "passed", G: "passed"}) == []  # a won mission stays won


def test_breaking_a_finished_checkpoint_heals_the_boss_and_pays_nothing_twice():
    m = mission()
    apply(m, {A: "passed", B: "passed", C: "failed", G: "passed"})
    xp = m.xp
    ch = apply(m, {A: "failed", B: "passed", C: "failed", G: "passed"})
    assert "regression" in kinds(ch) and "boss_healed" in kinds(ch)
    assert m.checkpoints[0].status == "regressed" and m.hp == 2 and m.active_checkpoint().id == "cp1"
    ch = apply(m, {A: "passed", B: "passed", C: "failed", G: "passed"})
    assert m.xp == xp and m.checkpoints[0].status == "done"
    assert next(c for c in ch if c.kind == "checkpoint_done").data["first"] is False


def test_a_missing_test_is_not_a_passing_test():
    m = mission()
    apply(m, {A: "passed", C: "failed", G: "passed"})  # B deleted
    assert m.hp == 2 and m.checkpoints[0].status == "pending"
    for outcome in ("skipped", "xfailed", "xpassed", "error"):
        m2 = mission()
        apply(m2, {A: outcome, B: "failed", C: "failed", G: "passed"})
        assert m2.hp == 3 and A not in m2.ever_passed, outcome


def test_edited_tests_count_for_nothing_until_accepted():
    m = mission()
    ch = apply(m, {A: "passed", B: "passed", C: "passed", G: "passed"}, tests_changed=["t.py"])
    assert kinds(ch) == ["tests_edited"] and m.status == "active" and m.hp == 3 and m.xp == 0
    assert m.flagged_tests == ["t.py"]
    apply(m, {A: "passed", B: "passed", C: "passed", G: "passed"})  # accepted: the caller stops reporting the change
    assert m.status == "won" and m.flagged_tests == []


def test_a_run_that_proves_nothing_changes_nothing():
    for status in ("broken", "timeout", "error", "no_tests"):
        m = mission()
        ch = apply(m, {A: "passed", B: "passed", C: "passed", G: "passed"}, status=status)
        assert kinds(ch) == ["inconclusive"] and (m.hp, m.xp, m.status) == (3, 0, "active"), status


def test_repeated_edits_that_move_nothing_ask_for_help_once_each_few_tries():
    m = mission()
    same = {A: "failed", B: "failed", C: "failed", G: "passed"}
    seen = [kinds(apply(m, same)) for _ in range(STUCK_AFTER * 2)]
    assert [("stuck" in k) for k in seen] == [False, False, True, False, False, True]
    assert m.active_checkpoint().fail_streak == STUCK_AFTER * 2
    apply(m, {A: "passed", B: "failed", C: "failed", G: "passed"})
    assert m.active_checkpoint().fail_streak == 0


def test_a_run_nobody_edited_for_is_not_an_attempt():
    m = mission()
    for _ in range(STUCK_AFTER + 1):
        assert "stuck" not in kinds(apply(m, {A: "failed", B: "failed", C: "failed", G: "passed"}, code_changed=False))
    assert m.active_checkpoint().fail_streak == 0


def test_new_tests_join_the_fight_or_the_guards():
    m = mission()
    new = "t2.py::test_new"
    ch = apply(m, {A: "failed", B: "failed", C: "failed", G: "passed", new: "failed"})
    assert "new_tests" in kinds(ch) and "boss_healed" in kinds(ch) and m.hp == 4 and m.hp_max == 4
    assert m.checkpoints[-1].id == "added" and m.checkpoints[-1].tests == [new]
    good = "t2.py::test_fine"
    apply(m, {A: "failed", B: "failed", C: "failed", G: "passed", new: "failed", good: "passed"})
    assert good in m.guard_tests and m.hp == 4


def test_a_mission_with_nothing_failing_has_no_boss_until_a_test_fails():
    base = run({G: "passed"})
    m = new_mission(mission_id="m", objective="x", title="x", project="/p", now=1.0, model="rule", plan_source="rule", groups=[], baseline=base, test_hashes={})
    assert m.hp == 0 and m.status == "active" and m.active_checkpoint() is None
    assert "Write a test" in m.criteria[0]
    assert apply_run(m, run({G: "passed"}), tests_changed=[], code_changed=True, now=2.0) == [] and m.status == "active"
    apply_run(m, run({G: "passed", A: "failed"}), tests_changed=[], code_changed=True, now=3.0)
    assert m.hp == 1 and m.active_checkpoint().tests == [A]
    apply_run(m, run({G: "passed", A: "passed"}), tests_changed=[], code_changed=True, now=4.0)
    assert m.status == "won"


def test_a_mission_survives_a_restart():
    m = mission()
    apply(m, {A: "passed", B: "failed", C: "failed", G: "passed"})
    from app.missions.engine import Mission

    back = Mission.from_dict(m.to_dict())
    assert back.to_dict() == m.to_dict() and back.active_checkpoint().title == "First"
