"""Merged conversational features must not create new security authority."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.runtime import LockedInRuntime
from app.security.arm_delegation import security_voice_intent
from app.voice.intents import IntentClassifier, IntentName

from .test_conversation_turns import part, voice
from .test_security_arm_delegation import delegating, grant, say


@pytest.mark.parametrize("wake", ["locki", "lockie", "locky", "loki", "lokey", "lokie", "locked in", "lock in", "lockedin", "lockin", "lock it in", "locked and", "lockton"])
def test_existing_wake_aliases_preserve_whole_security_grammar(wake):
    text = f"Okay {wake}, watch my setup"
    assert security_voice_intent(text) == "arm"
    assert IntentClassifier().classify(text).name == IntentName.ARM_WORKSTATION
    assert security_voice_intent(f'{text} if I leave') is None
    assert security_voice_intent(f'say "{text}"') is None
    assert security_voice_intent(f"{wake} do not watch my setup") is None
    assert security_voice_intent(f"{text}" + " " * 300 + "do not arm") is None


@pytest.mark.parametrize("case", ["current", "absent-source", "absent-epoch", "boolean-epoch", "stopped", "restart", "replaced",
                                  "absent-generation", "boolean-generation", "hold-release"])
@pytest.mark.parametrize("command", ["LOCKI watch my setup", "Lockie stop watching my setup"])
async def test_security_callback_requires_same_live_capture_at_dispatch(case, command):
    capture = SimpleNamespace(running=True, capture_epoch=3, input_generation=0, input_held=False)
    runtime = SimpleNamespace(_loop=asyncio.get_running_loop(), voice=capture, bus=SimpleNamespace(publish=Mock()), handle_utterance=AsyncMock())
    payload = {"text": command, "confidence": 1.0, "capture_epoch": 3, "input_generation": 0}
    if case == "absent-epoch":
        payload.pop("capture_epoch")
    elif case == "boolean-epoch":
        payload["capture_epoch"] = True
    elif case == "absent-generation":
        payload.pop("input_generation")
    elif case == "boolean-generation":
        payload["input_generation"] = False
    LockedInRuntime._voice_emit(runtime, "utterance", payload, capture=None if case == "absent-source" else capture)
    # All mutations occur after the thread callback, before scheduled dispatch.
    if case == "stopped":
        capture.running = False
    elif case == "restart":
        capture.capture_epoch += 2
    elif case == "replaced":
        runtime.voice = SimpleNamespace(running=True, capture_epoch=3, input_generation=0, input_held=False)
    elif case == "hold-release":
        capture.input_generation += 2
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime.handle_utterance.await_count == int(case == "current")
    assert runtime.bus.publish.call_count == int(case != "current")


async def test_unscoped_conversation_callback_remains_available():
    runtime = SimpleNamespace(_loop=asyncio.get_running_loop(), voice=None, bus=SimpleNamespace(publish=Mock()), handle_utterance=AsyncMock())
    LockedInRuntime._voice_emit(runtime, "utterance", {"text": "What happened while I was away?"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    runtime.handle_utterance.assert_awaited_once()


async def test_echo_removal_cannot_upgrade_original_discussion_into_arm(rt, monkeypatch):
    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    monkeypatch.setattr(rt.speaker, "echo_split", lambda *args, **kwargs: (0.9, "watch my setup"))
    await rt.handle_utterance('You said "watch my setup"', source="voice", stt_confidence=1)
    assert rt.security.transport.calls == []


async def test_deferred_fragment_retains_playback_ineligibility(rt):
    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    capture, emit = voice()
    capture._deliver(part("watch my", during=True))
    capture._deliver(part("setup", during=False))
    joined = emit.utterances()[-1]
    assert joined["text"] == "watch my setup" and joined["during_playback"]
    await rt.handle_utterance(joined["text"], source="voice", stt_confidence=1, during_playback=joined["during_playback"])
    assert rt.security.transport.calls == []


async def test_quiet_mode_preserves_verified_direct_arm_reply(rt):
    h = await delegating(rt)
    rt.security.arm_grants.apply(grant())
    rt.context.set_quiet(None, "test")
    result = await say(rt, "Loki watch my setup")
    assert result.ok and result.reply and await h.said("Armed.")
