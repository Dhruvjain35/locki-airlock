"""Turns failing tests into ordered checkpoints. A model may propose the grouping and the wording,
but only test ids pytest reported can appear in a plan, and every failing test must end up in one:
anything else is dropped or filled in here. With no model, or a bad answer, the fixed grouping runs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_CHECKPOINTS = 6
MAX_LISTED_TESTS = 40
PLAN_MAX_TOKENS = 320


@dataclass
class Plan:
    title: str
    groups: list[tuple[str, list[str]]]
    source: str  # the model's name, or "rule"
    say: str | None = None
    problems: list[str] = field(default_factory=list)


def loads_lenient(text: str) -> dict | None:
    """A model's JSON object, tolerating text around it and missing closing braces (a real habit of Qwen3-4B)."""
    start = (text or "").find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    blob = text[start : end + 1] if end > start else text[start:]
    for tail in ("", "}", '"}', "}}", '"}}'):
        try:
            data = json.loads(blob + tail)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else None
    return None


def _test_name(node: str) -> str:
    return re.sub(r"\[.*\]$", "", node.rsplit("::", 1)[-1]).removeprefix("test_")


def humanize(node: str) -> str:
    return _test_name(node).replace("_", " ")


def short_title(objective: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", objective).strip(" .!?\"'")
    if len(text) <= limit:
        return text or "Code Quest"
    return text[: limit].rsplit(" ", 1)[0]


def rule_groups(failing: list[str]) -> list[tuple[str, list[str]]]:
    """Failing tests grouped by the first word of their name, in the order they appear."""
    groups: dict[str, list[str]] = {}
    for node in failing:
        word = _test_name(node).split("_", 1)[0] or "tests"
        groups.setdefault(word, []).append(node)
    if len(groups) > MAX_CHECKPOINTS:
        by_file: dict[str, list[str]] = {}
        for node in failing:
            by_file.setdefault(node.split("::", 1)[0].rsplit("/", 1)[-1].removesuffix(".py"), []).append(node)
        groups = by_file
        if len(groups) > MAX_CHECKPOINTS:
            return [("Make the failing tests pass", list(failing))]
    return [(f"Fix {word.removeprefix('test_').replace('_', ' ')}", tests) for word, tests in groups.items()]


def rule_plan(objective: str, failing: list[str]) -> Plan:
    return Plan(short_title(objective), rule_groups(failing), "rule")


def plan_prompt(objective: str, failing: list[str], project: str) -> str:
    listed = failing[:MAX_LISTED_TESTS]
    tests = "\n".join(f"{i + 1}. {node}" for i, node in enumerate(listed))
    return (
        f'MISSION PLANNING. The user said: "{objective[:300]}"\nPROJECT FOLDER: {project}\n'
        f"FAILING TESTS, numbered. These are facts from running the tests:\n{tests}\n\n"
        f"Group every number above into 2 to {MAX_CHECKPOINTS - 1} checkpoints, in the order the user should work on them, "
        "easiest and most basic first. Tests about the same behaviour go together. A test that depends on the others goes last. "
        "Use only these numbers. Do not invent tests, and do not say anything is done.\n"
        'Answer with one JSON object on one line and nothing else: {"action": "none", "args": {"title": "<mission title, 3 to 6 words>", '
        '"checkpoints": [{"title": "<what to get working, 2 to 5 words>", "tests": [1, 2]}]}, "say": "<one short spoken line naming the first checkpoint>"}'
    )


def parse_model_plan(text: str, objective: str, failing: list[str], source: str) -> Plan:
    """Whatever the model wrote, the result covers exactly the failing tests, or is the fixed plan."""
    listed = failing[:MAX_LISTED_TESTS]
    problems: list[str] = []
    data = loads_lenient(text)
    args = data.get("args") if isinstance(data, dict) else None
    raw = args.get("checkpoints") if isinstance(args, dict) else None
    if not isinstance(raw, list) or not raw:
        plan = rule_plan(objective, failing)
        plan.problems = ["the model's answer was not a usable plan"]
        return plan
    groups: list[tuple[str, list[str]]] = []
    used: set[int] = set()
    for item in raw[:MAX_CHECKPOINTS]:
        if not isinstance(item, dict):
            problems.append("a checkpoint was not an object")
            continue
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip(" .\"'")[:60]
        tests = []
        for n in item.get("tests") or []:
            if isinstance(n, str) and n.strip().isdigit():
                n = int(n)
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(listed):
                problems.append(f"test {str(n)[:40]!r} does not exist")
            elif n - 1 in used:
                problems.append(f"test {n} was listed twice")
            else:
                used.add(n - 1)
                tests.append(listed[n - 1])
        if tests:
            groups.append((title or "Fix these tests", tests))
    if len(raw) > MAX_CHECKPOINTS:
        problems.append(f"too many checkpoints ({len(raw)})")
    left = [node for i, node in enumerate(listed) if i not in used] + failing[MAX_LISTED_TESTS:]
    if left:
        problems.append(f"{len(left)} failing tests were left out and added back")
        if not groups:
            plan = rule_plan(objective, failing)
            plan.problems = problems
            return plan
        groups.append(("Everything still failing", left))
    title = re.sub(r"\s+", " ", str(args.get("title") or "")).strip(" .\"'")[:60] or short_title(objective)
    say = data.get("say") if isinstance(data.get("say"), str) else None
    return Plan(title, groups, source, say=(say or "").strip() or None, problems=problems)
