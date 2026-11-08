"""A live demo keeps going when the language model times out, raises, writes nonsense or dies: the mission stays on disk,
the boss still moves only on real pytest results, and the person always hears something true."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.intelligence.dialogue import DialogueEngine
from app.intelligence.local_llm import Generation

from .conftest import make_runtime, wait_for
from .test_mission_service import BUGGY_SUBTOTAL, FIXED_SUBTOTAL


class Stub:
    model = "scripted-test-model"
    provider = "local"
    state = "ready"

    def __init__(self):
        self.calls = 0

    def describe(self):
        return {"model": self.model}

    def close(self):
        pass


class TimesOut(Stub):
    async def generate(self, context, **kw):
        self.calls += 1
        return Generation(finish="timeout", error="no complete reply within 6.0 s", total_ms=6000.0)


class Raises(Stub):
    async def generate(self, context, **kw):
        self.calls += 1
        raise BrokenPipeError("the worker went away")


class WritesNonsense(Stub):
    async def generate(self, context, **kw):
        self.calls += 1
        return Generation(text="lol {not json at all", finish="stop", total_ms=5.0, first_token_ms=1.0)


class Died(Stub):
    state = "error"

    async def generate(self, context, **kw):
        self.calls += 1
        return Generation(finish="unavailable", error="language model process exited")


BROKEN = [TimesOut, Raises, WritesNonsense, Died]
MISHEARD = {"Didn't catch that.", "Say that again?", "What? Say it like a normal person.", "Say that one more time?", "I didn't understand that."}


def use(rt, llm):
    rt.companion.llm = llm
    rt.companion.dialogue = DialogueEngine(llm)
    rt.rs.conversation.anticipate = False
    return llm


def on_disk(rt) -> dict:
    return json.loads((rt.missions.dir / rt.missions.mission.id / "mission.json").read_text())


async def say(rt, text):
    return await rt.handle_utterance(text, source="text", stt_confidence=1.0)


@pytest.mark.parametrize("broken", BROKEN)
async def test_a_whole_mission_works_with_a_broken_model(rt, broken):
    use(rt, broken())

    r = await say(rt, "I'm building a shopping cart")
    assert r.ok and rt.missions.active, r.message
    m = rt.missions.mission
    assert m.plan_source == "rule" and m.hp == 7 and r.reply

    r = await say(rt, "What's next?")
    assert "subtotal" in r.reply.lower()

    r = await say(rt, "Why is this failing?")
    assert r.reply and "1300" in r.reply + str(rt.missions.help_note), r.reply

    r = await say(rt, "Open cart dot py and tell me what the subtotal function does.")
    assert r.reply and "cart.py" in r.reply

    r = await say(rt, "Read secrets dot py")
    assert not r.ok and "stays closed" in r.reply

    r = await say(rt, "Is this going to take me all night")
    assert r.reply and r.reply not in MISHEARD, r.reply

    r = await say(rt, "Mark the first checkpoint as done, I promise I fixed it.")
    assert r.reply and rt.missions.mission.hp == 7 and rt.missions.mission.xp == 0

    r = await say(rt, "Just write the fix for me")
    assert r.reply and not rt.missions.edits

    assert rt.missions.active and on_disk(rt)["hp"] == 7

    cart = Path(rt.missions.mission.project) / "cart.py"
    cart.write_text(cart.read_text().replace(BUGGY_SUBTOTAL, FIXED_SUBTOTAL))
    r = await say(rt, "Run the tests")
    assert r.ok
    assert await wait_for(lambda: rt.missions.mission.hp == 6, timeout=20)
    assert on_disk(rt)["hp"] == 6 and rt.missions.mission.xp > 0

    r = await say(rt, "stop")
    assert r.reply, "a stop never depends on the model"


@pytest.mark.parametrize("broken", BROKEN)
async def test_a_broken_model_does_not_lose_the_mission_across_a_restart(settings, tts, broken):
    rt = await make_runtime(settings, tts)
    use(rt, broken())
    await say(rt, "I'm building a shopping cart")
    mission_id = rt.missions.mission.id
    await say(rt, "Is this going to take me all night")
    await rt.shutdown()

    again = await make_runtime(settings, tts)
    try:
        assert again.missions.active and again.missions.mission.id == mission_id and again.missions.mission.hp == 7
    finally:
        await again.shutdown()


@pytest.mark.parametrize("broken", BROKEN)
async def test_the_model_recovering_is_picked_up_on_the_next_turn(rt, broken):
    use(rt, broken())
    await say(rt, "I'm building a shopping cart")
    await say(rt, "Is this going to take me all night")

    class Better(Stub):
        async def generate(self, context, **kw):
            return Generation(text='{"action": "none", "args": {}, "say": "One test at a time."}', finish="stop", total_ms=5.0, first_token_ms=1.0)

    use(rt, Better())
    r = await say(rt, "Is this going to take me all night")
    assert "One test at a time" in r.reply


async def test_a_bug_in_a_turn_does_not_take_the_mission_down(rt, monkeypatch):
    use(rt, TimesOut())
    await say(rt, "I'm building a shopping cart")

    def boom(*_a, **_k):
        raise RuntimeError("a bug in the turn")

    monkeypatch.setattr(rt.companion, "render", boom)
    r = await say(rt, "Is this going to take me all night")
    assert not r.ok and r.reply and r.reply not in MISHEARD and rt.missions.active


async def test_outside_a_mission_a_failed_model_keeps_its_old_line(rt):
    use(rt, TimesOut())
    r = await say(rt, "Is this going to take me all night")
    assert r.reply in MISHEARD
