"""The protected tool gate, alone and on real NeMo Relay: a denied tool's callback never runs, whichever layer is asked, and a Relay failure changes nothing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.missions.relay import RelayRecorder, installed
from app.security.tool_gate import ProtectedToolGate

needs_relay = pytest.mark.skipif(not installed(), reason="nemo-relay is not installed")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "airlock_relay_demo.py"


def gate():
    g = ProtectedToolGate()
    g.register("run_pytest_in_sandbox", "sandbox")
    g.register("read_file", "read", lambda a: "looks like a secret" if "secret" in str(a.get("path")) else None)
    g.register("save_report", "propose", lambda a: None if isinstance(a.get("items"), list) else "items must be a list")
    return g


class Callback:
    def __init__(self, value="done"):
        self.calls, self.value = 0, value

    async def __call__(self):
        self.calls += 1
        return self.value


async def test_a_tool_not_on_the_list_is_denied_and_its_callback_never_runs():
    g, cb = gate(), Callback()
    outcome = await g.execute("deploy_dummy_artifact", {"target": "production"}, cb)
    assert not outcome.allowed and cb.calls == 0 and not outcome.callback_ran and outcome.decision.rule == "not_on_the_list"
    assert g.stats.denied == 1 and g.stats.denied_callbacks_run == 0


async def test_an_allowed_tool_runs_once_and_returns_its_value():
    g, cb = gate(), Callback(41)
    outcome = await g.execute("run_pytest_in_sandbox", {"trigger": "x"}, cb)
    assert outcome.allowed and outcome.value == 41 and cb.calls == 1 and outcome.callback_ran and outcome.trace_source == "locki_log"


async def test_an_argument_check_denies_before_the_callback():
    g, cb = gate(), Callback()
    assert not (await g.execute("read_file", {"path": ".secrets/x"}, cb)).allowed and cb.calls == 0
    assert (await g.execute("read_file", {"path": "cart.py"}, cb)).allowed and cb.calls == 1


async def test_a_check_that_crashes_denies():
    g, cb = gate(), Callback()
    g.register("broken", "read", lambda a: 1 / 0)
    outcome = await g.execute("broken", {}, cb)
    assert not outcome.allowed and outcome.decision.rule == "check_failed" and cb.calls == 0


async def test_an_exception_in_an_allowed_tool_is_reported_not_swallowed():
    g = gate()

    async def boom():
        raise RuntimeError("the tests would not start")

    outcome = await g.execute("run_pytest_in_sandbox", {}, boom)
    assert outcome.allowed and outcome.callback_ran and "would not start" in str(outcome.error)


async def test_while_frozen_only_reading_runs():
    g, cb = gate(), Callback()
    g.freeze("an incident is open")
    assert not (await g.execute("run_pytest_in_sandbox", {}, cb)).allowed
    assert (await g.execute("read_file", {"path": "cart.py"}, cb)).allowed and cb.calls == 1
    g.unfreeze()
    assert (await g.execute("run_pytest_in_sandbox", {}, cb)).allowed


async def test_decisions_are_reported():
    seen = []
    g = ProtectedToolGate(on_decision=lambda d, o, c: seen.append((d.tool, d.allowed, o.callback_ran, c.get("run_id"))))
    g.register("ok", "read")
    await g.execute("ok", {}, Callback(), context={"run_id": "r1"})
    await g.execute("nope", {}, Callback(), context={"run_id": "r1"})
    assert seen == [("ok", True, True, "r1"), ("nope", False, False, "r1")]


# ------------------------------------------------------------------ real NeMo Relay
def relay_events(folder: Path) -> list[dict]:
    path = folder / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


@needs_relay
async def test_real_relay_rejects_a_denied_call_and_its_own_trace_says_so(tmp_path):
    g, cb, ok = gate(), Callback(), Callback()
    relay = RelayRecorder(guardrail=g.guardrail)
    await relay.start(tmp_path, "test", scope_name="gate-test")
    assert relay.active and relay.trace_id
    denied = await g.execute("deploy_dummy_artifact", {"target": "production"}, cb, relay=relay, context={"tool_call_id": "t:1"})
    allowed = await g.execute("run_pytest_in_sandbox", {"trigger": "x"}, ok, relay=relay, summary=lambda v: {"v": v}, context={"tool_call_id": "t:2"})
    await relay.stop()
    assert cb.calls == 0 and not denied.allowed and denied.relay_saw_rejection and denied.trace_source == "nemo_relay"
    assert ok.calls == 1 and allowed.value == "done" and allowed.trace_source == "nemo_relay"
    events = relay_events(tmp_path)
    assert any(e.get("kind") == "mark" and e.get("name") == "deploy_dummy_artifact" and (e.get("data") or {}).get("rejected") for e in events)
    tool_starts = [e for e in events if e.get("category") == "tool" and e.get("scope_category") == "start"]
    assert [e["name"] for e in tool_starts] == ["run_pytest_in_sandbox"], "the denied tool never started"


@needs_relay
async def test_real_relay_alone_stops_a_real_callback(tmp_path):
    g = gate()
    relay = RelayRecorder(guardrail=g.guardrail)
    await relay.start(tmp_path, "test")
    ran = []

    async def real(_args):
        ran.append(1)
        return relay._relay.ToolExecutionResult({"ran": True})

    with pytest.raises(RuntimeError, match="guardrail rejected"):
        await relay._relay.tools.execute("deploy_dummy_artifact", {"target": "production"}, real, handle=relay._handle)
    await relay.stop()
    assert ran == []


@needs_relay
async def test_arguments_shortened_for_the_trace_do_not_change_the_decision(tmp_path):
    """From a real run: a valid report was rejected because Relay checked the shortened copy of its arguments."""
    g, cb = gate(), Callback()
    relay = RelayRecorder(guardrail=g.guardrail)
    await relay.start(tmp_path, "test")
    big = {"items": [{"statement": "x" * 200, "evidence_ids": ["e1"]}] * 5}
    outcome = await g.execute("save_report", big, cb, relay=relay)
    await relay.stop()
    assert outcome.allowed and outcome.error is None and cb.calls == 1
    assert "x" * 200 not in (tmp_path / "events.jsonl").read_text(), "long text stays out of the trace"


@needs_relay
async def test_a_rejection_inside_relay_is_never_treated_as_a_relay_failure(tmp_path):
    g, cb = gate(), Callback()
    relay = RelayRecorder(guardrail=g.guardrail)
    await relay.start(tmp_path, "test")
    with pytest.raises(RuntimeError, match="guardrail rejected"):
        await relay.tool("deploy_dummy_artifact", {"target": "x"}, cb)
    assert cb.calls == 0 and relay.active, "Relay stays on and the tool did not run"
    await relay.stop()


@needs_relay
async def test_the_gate_still_decides_when_relay_cannot_start(tmp_path):
    g, cb, ok = gate(), Callback(), Callback()
    first = RelayRecorder(guardrail=g.guardrail)
    await first.start(tmp_path / "a", "first")
    second = RelayRecorder(guardrail=g.guardrail)
    await second.start(tmp_path / "b", "second")  # Relay allows one active configuration
    assert first.active and not second.active and second.error
    assert not (await g.execute("deploy_dummy_artifact", {}, cb, relay=second)).allowed and cb.calls == 0
    assert (await g.execute("run_pytest_in_sandbox", {}, ok, relay=second)).allowed and ok.calls == 1
    await first.stop()


@needs_relay
def test_the_malicious_readme_scenario_end_to_end():
    out = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=120, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout and "NOT stopped" not in out.stdout and "stopped by Relay" in out.stdout


@needs_relay
async def test_relay_events_become_agent_steps_that_say_they_came_from_relay(tmp_path):
    from app.security.relay_steps import read_events, steps_from_relay

    g, ok = gate(), Callback()
    relay = RelayRecorder(guardrail=g.guardrail)
    await relay.start(tmp_path, "test")
    await g.execute("run_pytest_in_sandbox", {"trigger": "x"}, ok, relay=relay, context={"tool_call_id": "r:1"})
    await g.execute("deploy_dummy_artifact", {}, ok, relay=relay, context={"tool_call_id": "r:2"})
    await relay.stop()
    steps = steps_from_relay(read_events(tmp_path / "events.jsonl"), run_id="r", model_id="m")
    assert [(s.tool_name, s.status.value) for s in steps] == [("run_pytest_in_sandbox", "succeeded"), ("deploy_dummy_artifact", "denied")]
    assert all(s.trace_source == "nemo_relay" and s.trace_id and s.model_id == "m" for s in steps)
    assert steps[0].duration_ms is not None and "not a tool" in steps[1].detail


def test_nothing_is_a_relay_step_unless_relay_recorded_it():
    from app.security.relay_steps import read_events, steps_from_relay

    assert steps_from_relay([], run_id="r") == [] and read_events(Path("/nonexistent/events.jsonl")) == []
    assert steps_from_relay([{"kind": "scope", "category": "agent", "scope_category": "end", "name": "x"}, {"not": "an atof event"}], run_id="r") == []
