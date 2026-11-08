"""Fresh authority/readback and cancellation races; no real hardware or database."""

import asyncio
import json
import threading
import time

import httpx
import pytest

from app.security.arm_delegation import confirmed_session_row, security_voice_intent, select_fresh_grant
from app.security.publisher import Delivery
from app.security.spacetime_adapter import SpacetimeTransport
from app.voice.intents import IntentClassifier, IntentName

from .test_security_arm_delegation import delegating, grant, say
from .test_security_spacetime import CONFIG, remote


@pytest.mark.parametrize(
    "text",
    [
        "LOCKI, watch my setup",
        "Please monitor my desk",
        "Hey Locki could you start monitoring my computer please",
        "enable setup monitoring",
        "arm",
    ],
)
def test_explicit_whole_commands(text):
    assert security_voice_intent(text) == "arm"
    assert IntentClassifier().classify(text).name == IntentName.ARM_WORKSTATION


@pytest.mark.parametrize(
    "text",
    [
        'say "watch my setup"',
        "do not watch my desk",
        "never arm my computer",
        "what does arm my computer mean",
        "watch my desk if I leave",
        "arm my computer and approve everything",
        "arm" + " " * 300 + "do not arm",
        "'watch my setup'",
    ],
)
def test_negation_quotes_discussion_and_truncation_are_not_authority(text):
    assert security_voice_intent(text) is None
    assert IntentClassifier().classify(text).name != IntentName.ARM_WORKSTATION


def test_explicit_grammar_honors_disabled_security_exclusion():
    assert (
        IntentClassifier()
        .classify("LOCKI watch my setup", exclude=frozenset({IntentName.ARM_WORKSTATION}))
        .name
        != IntentName.ARM_WORKSTATION
    )


async def test_cached_grant_and_cached_session_never_substitute_for_fresh_server_read(rt):
    h = await delegating(rt)
    rt.security.arm_grants.apply(grant())
    transport = rt.security.transport

    def unavailable():
        raise TimeoutError("no fresh authority")

    transport.read_arm_snapshot = unavailable
    result = await say(rt, "LOCKI watch my setup")
    assert not result.ok and transport.calls == []
    assert not any("Armed." in line for line in h.spoken())


@pytest.mark.parametrize(
    "mutation", ["ambiguous", "wrong-identity", "owner-role", "disabled", "expired", "revoked", "consumed"]
)
async def test_fresh_selection_refuses_bad_authority(rt, mutation):
    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    snapshot = rt.security.transport.read_arm_snapshot()
    if mutation == "ambiguous":
        snapshot["grants"].append({**snapshot["grants"][0], "id": "other"})
    elif mutation == "wrong-identity":
        snapshot["devices"][0]["identity"] = ["0x" + "c" * 64]
    elif mutation == "owner-role":
        snapshot["memberships"][0]["role"] = "owner"
    elif mutation == "disabled":
        snapshot["devices"][0]["enabled"] = False
    elif mutation == "expired":
        snapshot["grants"][0]["expires_at_ms"] = 1
    else:
        snapshot["grants"][0]["status"] = mutation
    with pytest.raises(ValueError):
        select_fresh_grant(snapshot, CONFIG.workstation_id, CONFIG.device_id)


@pytest.mark.parametrize(
    "mutation",
    [
        "simulated",
        "auto-lock",
        "disarmed",
        "baseline",
        "apps",
        "policy",
        "duration",
        "active-session",
        "unconsumed",
        "principal",
    ],
)
async def test_committed_readback_checks_exact_owner_grant_and_real_session(rt, mutation):
    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    transport = rt.security.transport
    selected = select_fresh_grant(transport.read_arm_snapshot(), CONFIG.workstation_id, CONFIG.device_id)
    transport.committed = (selected.authorization_id, int(time.time() * 1000))
    snapshot = transport.read_arm_snapshot()
    confirmed_session_row(snapshot, selected)
    session = snapshot["sessions"][0]
    if mutation == "simulated":
        session["simulated"] = True
    elif mutation == "auto-lock":
        session["auto_lock_enabled"] = True
    elif mutation == "disarmed":
        session["state"] = "disarmed"
    elif mutation == "baseline":
        session["baseline_json"] = '["other"]'
    elif mutation == "apps":
        session["approved_apps_json"] = '["other"]'
    elif mutation == "policy":
        session["owner_policy_hash"] = "other"
    elif mutation == "duration":
        session["expires_at_ms"] += 1
    elif mutation == "active-session":
        snapshot["workstations"][0]["active_session_id"] = "other"
    elif mutation == "unconsumed":
        snapshot["grants"][0]["status"] = "authorized"
    elif mutation == "principal":
        session["armed_by"] = ["0x" + "c" * 64]
    with pytest.raises(ValueError):
        confirmed_session_row(snapshot, selected)


async def test_repeated_cancellation_keeps_single_flight_until_http_worker_finishes(rt):
    h = await delegating(rt)
    rt.security.arm_grants.apply(grant())
    entered, release = threading.Event(), threading.Event()
    transport = rt.security.transport

    def blocked(*args):
        transport.calls.append(args)
        entered.set()
        assert release.wait(3)
        return Delivery("delivered", "fake")

    transport.call_reducer = blocked
    operation = asyncio.create_task(rt.security.request_arm_by_voice())
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        operation.cancel()
        await asyncio.sleep(0.01)
        operation.cancel()
        await asyncio.sleep(0.01)
        assert rt.security._voice_arm_busy and not operation.done()
        duplicate = rt.security.request_arm_by_voice()
        assert not duplicate.ok and len(transport.calls) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not rt.security._voice_arm_busy
        assert not any("Armed." in line for line in h.spoken())
    finally:
        release.set()


async def test_lost_ack_requires_fresh_readback_but_explicit_refusal_cannot_recover(rt):
    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    transport = rt.security.transport
    original = transport.call_reducer

    def lost(*args):
        original(*args)
        return Delivery("failed", "fake", "ACK lost")

    transport.call_reducer = lost
    assert (await say(rt)).ok
    assert transport.snapshots == 2
    assert not (await say(rt)).ok  # pre-consumed grant cannot be a new success


async def test_runtime_cannot_upgrade_truncated_or_echo_command_into_arm(rt):
    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    await say(rt, "arm" + " " * 297 + "do not arm")
    await rt.handle_utterance("watch my setup", source="voice", stt_confidence=1, during_playback=True)
    assert rt.security.transport.calls == []


def test_transport_reads_fixed_five_views_fresh_and_requires_confirmed_reducer():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/sql"):
            return httpx.Response(200, json=[{"schema": {"elements": []}, "rows": []}])
        return httpx.Response(200, text="")

    transport = SpacetimeTransport(CONFIG, client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert set(transport.read_arm_snapshot()) == {
        "memberships",
        "devices",
        "grants",
        "sessions",
        "workstations",
    }
    transport.read_arm_snapshot()
    transport.call_reducer("arm_session_delegated", [CONFIG.workstation_id, "grant"])
    assert len(requests) == 11
    assert requests[0].headers["Authorization"] == "Bearer agent-token"
    assert all(request.content.count(b"SELECT * FROM") == 1 for request in requests[:-1])
    assert b"my_workstations" in requests[4].content
    assert requests[-1].url.params["confirmed"] == "true"
    assert json.loads(requests[-1].content) == [CONFIG.workstation_id, "grant"]


@pytest.mark.parametrize("cached_session", [False, True])
async def test_database_voice_disarm_refers_to_owner_without_claiming_cached_state(rt, cached_session):
    await delegating(rt)
    if cached_session:
        rt.security.apply_remote_session(remote())
    result = await say(rt, "disarm my setup")
    assert not result.ok and rt.security.transport.calls == []
    assert "authenticated owner console" in result.reply
    assert "not armed" not in result.reply.lower() and "still armed" not in result.reply.lower()
    assert "code" not in result.reply.lower()
