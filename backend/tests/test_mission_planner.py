"""A model may word and order a plan, but it cannot add a test or leave one out."""

from __future__ import annotations

import json

from app.missions.planner import (
    MAX_CHECKPOINTS,
    humanize,
    parse_model_plan,
    plan_prompt,
    rule_groups,
    rule_plan,
)

F = ["t.py::test_subtotal_counts", "t.py::test_discount_percent", "t.py::test_discount_range[-1]", "t.py::test_shipping_free"]


def reply(checkpoints, title="Fix the cart", say="Start with the subtotal."):
    return json.dumps({"action": "none", "args": {"title": title, "checkpoints": checkpoints}, "say": say})


def covered(plan):
    return sorted(t for _, tests in plan.groups for t in tests)


def test_the_rule_plan_groups_by_what_the_tests_are_about():
    assert rule_groups(F) == [("Fix subtotal", [F[0]]), ("Fix discount", [F[1], F[2]]), ("Fix shipping", [F[3]])]
    assert rule_plan("fix the cart", F).source == "rule"
    assert humanize(F[2]) == "discount range"


def test_too_many_groups_fall_back_to_files_then_to_one():
    many = [f"a.py::test_w{i}_x" for i in range(MAX_CHECKPOINTS + 1)]
    assert rule_groups(many) == [("Fix a", many)]
    files = [f"f{i}.py::test_w{i}_x" for i in range(MAX_CHECKPOINTS + 1)]
    assert rule_groups(files) == [("Make the failing tests pass", files)]


def test_a_good_model_plan_is_used():
    p = parse_model_plan(reply([{"title": "Subtotal", "tests": [1]}, {"title": "Discount and shipping", "tests": [2, 3, 4]}]), "x", F, "qwen")
    assert p.source == "qwen" and p.title == "Fix the cart" and p.say == "Start with the subtotal." and p.problems == []
    assert p.groups == [("Subtotal", [F[0]]), ("Discount and shipping", F[1:])]


def test_invented_tests_are_refused_and_every_failing_test_is_kept():
    p = parse_model_plan(reply([{"title": "A", "tests": [1, 99, "t.py::test_made_up"]}, {"title": "B", "tests": [2, 1]}]), "x", F, "qwen")
    assert covered(p) == sorted(F)
    assert any("99" in x for x in p.problems) and any("made_up" in x for x in p.problems) and any("twice" in x for x in p.problems)
    assert p.groups[-1] == ("Everything still failing", [F[2], F[3]])


def test_a_plan_that_covers_nothing_uses_the_fixed_grouping():
    for bad in ("not json", "{}", reply([]), reply([{"title": "A", "tests": [99]}]), json.dumps({"args": {"checkpoints": "x"}})):
        p = parse_model_plan(bad, "fix the cart", F, "qwen")
        assert p.source == "rule" and covered(p) == sorted(F) and p.problems, bad


def test_numbers_written_as_text_are_accepted_and_booleans_are_not():
    p = parse_model_plan(reply([{"title": "A", "tests": ["1", "2", True]}, {"title": "B", "tests": [3, 4]}]), "x", F, "qwen")
    assert covered(p) == sorted(F) and any("True" in x for x in p.problems)


def test_the_prompt_lists_only_real_tests_and_asks_for_numbers():
    text = plan_prompt("fix the cart", F, "/p")
    assert all(node in text for node in F) and '"tests": [1, 2]' in text and "Do not invent" in text
