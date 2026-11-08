"""NeMo Relay gate tests against the REAL installed nemo-relay package (skipped if absent)."""

from dataclasses import replace

import pytest

nemo_relay = pytest.importorskip("nemo_relay")

from conftest import A_ID, A_PATH, calls, final, scripted_client, text  # noqa: E402
from nemotron_investigator.agent import Requester, investigate  # noqa: E402
from nemotron_investigator.evidence.fixture import FixtureEvidenceSource  # noqa: E402
from nemotron_investigator.relay_gate import get_gate  # noqa: E402
from nemotron_investigator.tools import ToolContext, execute_tool_call  # noqa: E402


async def _ctx():
    src = FixtureEvidenceSource(A_PATH)
    inc = await FixtureEvidenceSource(A_PATH).get_incident(A_ID)
    return ToolContext("inv_relay", inc, src, relay_gate=get_gate(600)), src


async def test_relay_blocks_before_tool_body_runs():
    ctx, src = await _ctx()
    rec, _ = await execute_tool_call(ctx, "get_recent_usb_events", {"window_s": 1800}, step=1, call_index=0,
                                     timeout_s=5)
    assert rec.status == "policy_denied" and src.reads == 0
    assert "nemo-relay" in rec.policy_gate and "600" in rec.error


async def test_relay_allows_within_policy_and_ignores_other_tools():
    ctx, src = await _ctx()
    rec, _ = await execute_tool_call(ctx, "get_recent_usb_events", {"window_s": 600}, step=1, call_index=0,
                                     timeout_s=5)
    assert rec.status == "ok" and src.reads == 1 and rec.policy_gate
    rec2, _ = await execute_tool_call(ctx, "get_recent_vision_events", {"window_s": 1800}, step=1, call_index=1,
                                      timeout_s=5)
    assert rec2.status == "ok" and rec2.policy_gate is None


async def test_agent_with_gate_reports_policy_denial(settings):
    client, _ = scripted_client([calls(("get_incident", {"incident_id": A_ID})),
                                 calls(("get_recent_usb_events", {"window_s": 1800})), text(), final()])
    r = await investigate(A_ID, Requester("t", ("*",)), FixtureEvidenceSource(A_PATH), client,
                          replace(settings, relay_gate=True))
    assert r["policy_gate"]["product"] == "NVIDIA NeMo Relay"
    assert r["metrics"]["tool_calls_policy_denied"] == 1
    assert r["investigation_status"] == "completed_with_warnings"
