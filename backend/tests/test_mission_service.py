"""A whole mission on the real pytest runner, with a stand-in for the language model where one is needed.
No microphone, camera or robot: those are exercised on the hardware (docs/legacy/CODE_QUEST.md)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.intelligence.actions import ActionName
from app.intelligence.dialogue import DialogueEngine, check_reply
from app.intelligence.local_llm import Generation
from app.main import create_app
from app.missions.engine import STUCK_AFTER
from app.runtime import VisionEvent
from app.telemetry.events import EventType
from app.voice.conversation import SessionFacts
from app.voice.intents import IntentClassifier, IntentName
from app.voice.personality import Style

from .conftest import RecordingTTS, make_runtime, wait_for

DEMO = Path(__file__).resolve().parents[2] / "examples" / "code_quest_demo"
FIXED_SUBTOTAL = "return sum(item.unit_price_cents * item.quantity for item in self.items)"
BUGGY_SUBTOTAL = "return sum(item.unit_price_cents for item in self.items)"


class ScriptedLLM:
    """Stands in for the model process: the rest of the path (checks, tools, voice) is the real one."""

    model = "scripted-test-model"
    state = "ready"
    provider = "local"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def generate(self, context, **kw):
        self.prompts.append(context)
        text = self.replies.pop(0) if self.replies else '{"action": "none", "args": {}, "say": "Keep going."}'
        on_delta = kw.get("on_delta")
        if on_delta is not None:  # as the real model streams: a broken handler fails the test here
            for i in range(0, len(text), 12):
                on_delta(text[i : i + 12])
        return Generation(text=text, finish="stop", total_ms=5.0, first_token_ms=1.0)

    def describe(self):
        return {"model": self.model}

    def close(self):
        pass


def with_model(rt, replies=()):
    llm = ScriptedLLM(replies)
    rt.companion.llm = llm
    rt.companion.dialogue = DialogueEngine(llm)
    rt.rs.conversation.anticipate = False  # no lines prepared in the background: replies go in the order scripted
    return llm


@pytest.fixture
async def quest(rt, tmp_path):
    return rt


def cart(rt) -> Path:
    return Path(rt.missions.mission.project) / "cart.py"


def fix(rt, old, new) -> None:
    p = cart(rt)
    p.write_text(p.read_text().replace(old, new))


async def fix_everything(rt):
    fix(rt, BUGGY_SUBTOTAL, FIXED_SUBTOTAL)
    fix(rt, "return subtotal_cents - percent", "if not 0 <= percent <= 100:\n        raise ValueError(percent)\n    return subtotal_cents * (100 - percent) // 100")
    fix(rt, "subtotal_cents > FREE_SHIPPING_CENTS", "subtotal_cents >= FREE_SHIPPING_CENTS")


async def test_a_whole_mission_from_objective_to_boss(rt):
    r = await rt.missions.start("I'm finishing the cart checkout", origin="test")
    assert r.ok
    m = rt.missions.mission
    assert m.hp == m.hp_max == 7 and m.xp == 0 and m.status == "active" and m.plan_source == "rule"
    assert [c.title for c in m.checkpoints] == ["Fix subtotal", "Fix discount", "Fix shipping", "Fix total"]
    assert len(m.guard_tests) == 2 and rt.sessions.active
    assert rt.missions.next_objective() == "Make 'subtotal counts quantity' pass"

    fix(rt, BUGGY_SUBTOTAL, FIXED_SUBTOTAL)
    await rt.missions.run_tests("test", code_changed=True)
    assert m.hp == 6 and m.checkpoints[0].status == "done" and m.xp == 60
    assert rt.missions.next_objective() == "Make 'discount takes a percentage' pass"

    await fix_everything(rt)
    await rt.missions.run_tests("test", code_changed=True)
    assert m.status == "won" and m.hp == 0 and m.xp == 7 * 10 + 4 * 50 + 100
    assert rt.missions.xp_total == m.xp
    assert not rt.sessions.active, "the session the mission started ends with it"
    assert any(e.event_type == EventType.MISSION_WON for e in rt.bus.history())
    assert await wait_for(lambda: any("Boss" in s for s in rt.speaker.engine.spoken))

    trace = (rt.settings.data_dir / "missions" / m.id / "trace.jsonl").read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in trace]
    assert kinds[0] == "mission_created" and kinds.count("verification") == 2 and "mission_won" in kinds


async def test_nothing_a_model_says_can_move_the_mission(rt):
    await rt.missions.start("fix the cart", origin="test")
    m = rt.missions.mission
    llm = with_model(rt, ['{"action": "complete_checkpoint", "args": {"id": "cp1"}, "say": "Checkpoint done, boss beaten."}'])
    r = await rt.handle_utterance("I think I fixed the subtotal, is the checkpoint complete now then?", source="text", stt_confidence=1.0)
    assert llm.prompts, "the turn reached the model"
    assert m.hp == 7 and m.xp == 0 and m.checkpoints[0].status == "pending"
    assert any("unknown action 'complete_checkpoint'" in p for p in rt.companion.replies[-1]["problems"])
    assert "boss beaten" not in (r.reply or "").lower()


def test_a_line_claiming_progress_is_refused_while_the_tests_say_otherwise():
    facts = SessionFacts()
    style = Style()
    for line in ("Boss beaten!", "All the tests pass now.", "Checkpoint one is done.", "Mission complete.", "You fixed it.", "That fixed the bug."):
        assert not check_reply(line, facts=facts, style=style, progress_claim_ok=False).ok, line
        assert check_reply(line, facts=facts, style=style).ok, f"{line}: fine with no mission running"
    for line in ("The boss isn't beaten yet.", "Run the tests once you think it works.", "The subtotal ignores quantity."):
        assert check_reply(line, facts=facts, style=style, progress_claim_ok=False).ok, line


async def test_the_watcher_runs_the_tests_when_you_save(settings, tts):
    rt = await make_runtime(settings, tts)
    try:
        await rt.missions.start("fix the cart", origin="test")
        m = rt.missions.mission
        fix(rt, BUGGY_SUBTOTAL, FIXED_SUBTOTAL)
        assert await wait_for(lambda: m.hp == 6, timeout=8.0), "the save was not picked up"
        assert rt.missions.runs[0]["trigger"] == "you saved a file"
    finally:
        await rt.shutdown()


async def test_editing_a_test_does_not_beat_the_boss(rt):
    await rt.missions.start("fix the cart", origin="test")
    m = rt.missions.mission
    tests = Path(m.project) / "tests" / "test_cart.py"
    tests.write_text("def test_everything():\n    assert True\n")
    await rt.missions.run_tests("test", code_changed=True)
    assert m.status == "active" and m.hp == 7 and m.flagged_tests == ["tests/test_cart.py"]
    assert rt.missions.next_objective().startswith("A test file changed")
    assert await wait_for(lambda: any("test file" in s for s in rt.speaker.engine.spoken))
    assert rt.missions.accept_test_changes().ok and m.flagged_tests == []
    await rt.missions.run_tests("test", code_changed=True)
    assert m.status == "active" and not m.checkpoints, "removing every failing test leaves no boss, not a win"
    assert len(m.removed_tests) == 9 and rt.missions.snapshot()["mission"]["removed_tests"] == 9


async def test_broken_code_stops_nothing_and_counts_for_nothing(rt):
    await rt.missions.start("fix the cart", origin="test")
    m = rt.missions.mission
    cart(rt).write_text("def broken(:\n")
    run = await rt.missions.run_tests("test", code_changed=True)
    assert run.status == "broken" and m.hp == 7 and m.status == "active"
    assert rt.missions.snapshot()["character"]["mood"] == "alarmed"


async def test_repeated_failure_gets_help_built_from_the_real_failure(rt):
    await rt.missions.start("fix the cart", origin="test")
    llm = with_model(rt, ['{"action": "none", "args": {}, "say": "subtotal_cents adds prices but never multiplies by quantity."}'])
    for _ in range(STUCK_AFTER):
        await rt.missions.run_tests("test", code_changed=True)
    assert await wait_for(lambda: rt.missions.help_note is not None, timeout=3.0)
    note = rt.missions.help_note
    assert note["source"] == "scripted-test-model" and "subtotal_cents" in note["text"]
    prompt = llm.prompts[-1]
    assert "850" in prompt and "def subtotal_cents" in prompt and "subtotal counts quantity" in prompt


async def test_help_without_a_model_says_what_the_test_says(rt):
    await rt.missions.start("fix the cart", origin="test")
    r = await rt.missions.help(origin="test", user_text="why is it failing")
    assert r.ok and "850" in r.reply and rt.missions.help_note["source"] == "rule"


async def test_a_read_tool_feeds_the_real_file_back_to_the_model(rt):
    await rt.missions.start("fix the cart", origin="test")
    llm = with_model(rt, [
        '{"action": "read_file", "args": {"path": "cart.py"}, "say": "Let me look at that code."}',
        '{"action": "none", "args": {}, "say": "Your subtotal never looks at quantity."}',
    ])
    r = await rt.handle_utterance("Can you take a look at my code and tell me what you notice about it", source="text", stt_confidence=1.0)
    assert len(llm.prompts) == 2 and "TOOL RESULT from read_file of cart.py" in llm.prompts[1] and BUGGY_SUBTOTAL in llm.prompts[1]
    assert r.ok and "quantity" in r.reply


async def test_nothing_is_said_about_a_file_before_it_has_been_read(rt):
    await rt.missions.start("fix the cart", origin="test")
    with_model(rt, [
        '{"action": "read_file", "args": {"path": "cart.py"}, "say": "Sounds like a weird math problem in there."}',
        '{"action": "none", "args": {}, "say": "Your subtotal never looks at quantity."}',
    ])
    r = await rt.handle_utterance("Can you take a look at my code and tell me what you notice about it", source="text", stt_confidence=1.0)
    assert await wait_for(lambda: any("quantity" in s for s in rt.speaker.engine.spoken))
    assert not any("weird math" in s for s in rt.speaker.engine.spoken) and "weird math" not in (r.reply or "")


async def test_a_real_edit_written_the_way_qwen_writes_it_is_understood(rt):
    """From a real run: one closing brace short, newlines escaped twice, and "say" inside args."""
    await rt.missions.start("fix the cart", origin="test")
    raw = (
        '{"action": "none", "args": {"path": "cart.py", "old": "    def subtotal_cents(self) -> int:\\n        ' + BUGGY_SUBTOTAL + '", '
        '"new": "    def subtotal_cents(self) -> int:\\n        ' + FIXED_SUBTOTAL + '", "say": "multiplies price by quantity before summing"}'
    )
    with_model(rt, [raw])
    r = await rt.missions.propose_edit(origin="test")
    assert r.ok, r.message
    edit = rt.missions.snapshot()["edits"][0]
    assert edit["why"] == "multiplies price by quantity before summing" and "\n" in edit["old"]
    assert rt.missions.decide_edit(edit["id"], approve=True).ok and FIXED_SUBTOTAL in cart(rt).read_text()


async def test_asking_how_it_is_going_during_a_mission_answers_about_the_mission(rt):
    await rt.missions.start("fix the cart", origin="test")
    for said in ("How am I doing", "What should I do next"):
        r = await rt.handle_utterance(said, source="voice", stt_confidence=0.95)
        assert r.ok and "7" in r.reply and "subtotal" in r.reply.lower(), said


async def test_the_failure_numbers_may_be_spoken(rt):
    await rt.missions.start("fix the cart", origin="test")
    assert {850, 1300} <= rt.missions.numbers()


async def test_words_about_a_running_tool_are_not_spoken_only_what_happened(rt):
    await rt.missions.start("fix the cart", origin="test")
    with_model(rt, ['{"action": "run_tests", "args": {}, "say": "All fixed, you are the hero of the checkout line."}'])
    r = await rt.companion.converse("Run my tests again please, I want a fresh result", "typed", None, None)
    assert r.ok and rt.missions.mission.runs == 2 and "hero" not in (r.reply or "")
    assert await wait_for(lambda: "Running them." in rt.speaker.engine.spoken)
    assert not any("hero" in s for s in rt.speaker.engine.spoken)


async def test_the_model_cannot_end_the_session_by_misusing_release_phone(rt):
    """From a real run: Qwen3 answered "Push my code to GitHub" with release_phone."""
    await rt.missions.start("fix the cart", origin="test")
    with_model(rt, ['{"action": "release_phone", "args": {}, "say": "I can\'t do that one."}'])
    await rt.companion.converse("Push my code to GitHub for me", "typed", None, None)
    assert rt.sessions.active and rt.companion.requests[-1]["status"] == "ignored"
    with_model(rt, ['{"action": "release_phone", "args": {}, "say": "It is yours."}'])
    await rt.companion.converse("I need my phone to call my mom", "typed", None, None)
    assert rt.companion.requests[-1]["status"] == "executed"


async def test_during_a_mission_the_model_only_gets_the_short_tool_list(rt):
    from app.intelligence.actions import MISSION_TOOLS

    await rt.missions.start("fix the cart", origin="test")
    allowed = set(rt.companion._allowed())
    assert set(MISSION_TOOLS) <= allowed and ActionName.RELEASE_PHONE in allowed and ActionName.STOP in allowed
    for gone in (ActionName.SET_GOAL, ActionName.SET_STYLE, ActionName.END_SESSION, ActionName.REQUEST_RETREAT):
        assert gone not in allowed


async def test_a_refused_read_tool_is_reported_not_guessed(rt):
    """From a real run: asked to word the failure, Qwen3 said "the secrets file is empty". The reason is said instead."""
    (Path(rt.missions.prepare_demo()) / ".env").write_text("FISH_AUDIO_API_KEY=abcdef123456\n")
    await rt.missions.start("fix the cart", origin="test")
    llm = with_model(rt, ['{"action": "read_file", "args": {"path": ".env"}, "say": "Sure, it says hunter2."}'])
    r = await rt.handle_utterance("Which of my settings has the API key? Tell me the value", source="text", stt_confidence=1.0)
    assert len(llm.prompts) == 1, "the model does not get to describe a file nobody read"
    assert not r.ok and "stays closed" in r.reply and rt.companion.requests[-1]["status"] == "refused"
    assert await wait_for(lambda: any("stays closed" in s for s in rt.speaker.engine.spoken))
    assert not any("hunter2" in s or "abcdef" in s for s in rt.speaker.engine.spoken)


async def test_credit_claims_are_refused_even_with_a_not_in_the_sentence():
    facts, style = SessionFacts(), Style()
    for line in ("Alright, checkpoint one is officially fixed in the books.", "XP's still yours, though, just don't call it a win.", "XP coming, but we still got a bug to fix.", "You've earned 50 XP."):
        assert not check_reply(line, facts=facts, style=style, progress_claim_ok=False, extra_numbers={50}).ok, line
    assert check_reply("No XP until the tests say so.", facts=facts, style=style, progress_claim_ok=False).ok


async def test_tools_exist_only_during_a_mission_and_other_prompts_are_untouched(rt):
    with_model(rt)
    no_mission = rt.companion.render(allowed=rt.companion._allowed(), user_text="hello there")
    assert "MISSION" not in no_mission and "run_tests" not in no_mission
    assert ActionName.RUN_TESTS not in rt.companion._allowed()
    await rt.missions.start("fix the cart", origin="test")
    with_mission = rt.companion.render(allowed=rt.companion._allowed(), user_text="hello there")
    assert ActionName.RUN_TESTS in rt.companion._allowed()
    assert "BOSS: 7 of 7" in with_mission and "OBJECTIVE: fix the cart" in with_mission and "run_tests" in with_mission and "cart.py" in with_mission


async def test_an_edit_waits_for_a_person_and_the_tests_still_decide(rt):
    await rt.missions.start("fix the cart", origin="test")
    m = rt.missions.mission
    before = cart(rt).read_text()
    with_model(rt, [json.dumps({"action": "none", "args": {"path": "cart.py", "old": BUGGY_SUBTOTAL, "new": FIXED_SUBTOTAL}, "say": "Multiply by quantity."})])
    r = await rt.missions.propose_edit(origin="test")
    assert r.ok and cart(rt).read_text() == before and m.hp == 7, "proposing changes nothing"
    edit = rt.missions.snapshot()["edits"][0]
    assert edit["path"] == "cart.py"
    assert rt.missions.decide_edit(edit["id"], approve=True).ok
    assert FIXED_SUBTOTAL in cart(rt).read_text() and m.hp == 7, "applying is not passing"
    await rt.missions.run_tests("test", code_changed=True)
    assert m.hp == 6


@pytest.mark.parametrize(
    "args,why",
    [
        ({"path": "tests/test_cart.py", "old": "assert", "new": "assert True or"}, "test"),
        ({"path": "cart.py", "old": "not in the file", "new": "x"}, "exactly once"),
        ({"path": "../outside.py", "old": "a", "new": "b"}, "outside"),
        ({"path": ".env", "old": "a", "new": "b"}, "secret"),
        ({"path": "cart.py", "old": "", "new": "b"}, "empty"),
    ],
)
async def test_bad_edits_are_refused(rt, args, why):
    await rt.missions.start("fix the cart", origin="test")
    before = cart(rt).read_text()
    with_model(rt, [json.dumps({"action": "none", "args": args, "say": "x"})])
    r = await rt.missions.propose_edit(origin="test")
    assert not r.ok and why in r.message and not rt.missions.edits and cart(rt).read_text() == before


async def test_an_edit_on_a_file_that_moved_on_is_not_applied(rt):
    await rt.missions.start("fix the cart", origin="test")
    with_model(rt, [json.dumps({"action": "none", "args": {"path": "cart.py", "old": BUGGY_SUBTOTAL, "new": FIXED_SUBTOTAL}, "say": "x"})])
    await rt.missions.propose_edit(origin="test")
    edit_id = next(iter(rt.missions.edits))
    cart(rt).write_text(cart(rt).read_text() + "\n# changed by hand\n")
    r = rt.missions.decide_edit(edit_id, approve=True)
    assert not r.ok and FIXED_SUBTOTAL not in cart(rt).read_text()


async def test_a_model_plan_cannot_invent_or_lose_tests(rt):
    with_model(rt, ['{"action": "none", "args": {"title": "Fix the cart", "checkpoints": [{"title": "Totals", "tests": [1, 99]}]}, "say": "Totals first."}'])
    r = await rt.missions.start("fix the cart", origin="test")
    m = rt.missions.mission
    assert r.ok and m.plan_source == "scripted-test-model" and m.hp == 7
    assert [c.title for c in m.checkpoints] == ["Totals", "Everything still failing"]
    assert sum(len(c.tests) for c in m.checkpoints) == 7
    assert await wait_for(lambda: "Totals first." in rt.speaker.engine.spoken)


async def test_unapproved_folders_are_refused(rt, tmp_path):
    shutil.copytree(DEMO, tmp_path / "other")
    r = await rt.missions.start("fix it", project=str(tmp_path / "other"), origin="test")
    assert not r.ok and "approved" in r.message and rt.missions.mission is None


async def test_a_mission_comes_back_after_a_restart(settings, tts):
    rt = await make_runtime(settings, tts)
    await rt.missions.start("fix the cart", origin="test")
    fix(rt, BUGGY_SUBTOTAL, FIXED_SUBTOTAL)
    await rt.missions.run_tests("test", code_changed=True)
    mid, hp, xp = rt.missions.mission.id, rt.missions.mission.hp, rt.missions.mission.xp
    await rt.shutdown()
    again = await make_runtime(settings, RecordingTTS())
    try:
        assert again.missions.mission is not None and again.missions.mission.id == mid
        assert (again.missions.mission.hp, again.missions.mission.xp) == (hp, xp) and again.missions.xp_total == xp
    finally:
        await again.shutdown()


async def test_a_dropped_mission_is_not_restored_and_says_so(settings, tts):
    rt = await make_runtime(settings, tts)
    await rt.missions.start("fix the cart", origin="test")
    assert (await rt.missions.abandon(origin="test")).ok and not rt.missions.active and not rt.sessions.active
    await rt.shutdown()
    again = await make_runtime(settings, RecordingTTS())
    try:
        assert again.missions.mission is None
    finally:
        await again.shutdown()


# ------------------------------------------------------------------ voice commands
@pytest.mark.parametrize(
    "said,name",
    [
        ("Start a new mission to fix the cart", IntentName.START_MISSION),
        ("Hey Locki, I'm building a checkout for my shopping cart", IntentName.START_MISSION),
        ("Help me finish the checkout feature", IntentName.START_MISSION),
        ("Code quest", IntentName.START_MISSION),
        ("Run the tests", IntentName.RUN_TESTS),
        ("What's next", IntentName.MISSION_STATUS),
        ("How much health does the boss have", IntentName.MISSION_STATUS),
        ("Why is this failing", IntentName.MISSION_HELP),
        ("I'm stuck", IntentName.MISSION_HELP),
        ("Write me a fix", IntentName.PROPOSE_FIX),
        ("Drop the mission", IntentName.ABANDON_MISSION),
        ("Stop the mission", IntentName.ABANDON_MISSION),
    ],
)
def test_mission_phrases(said, name):
    assert IntentClassifier().classify(said).name == name


@pytest.mark.parametrize(
    "said,name",
    [
        ("Stop", IntentName.STOP_MOTION),
        ("Emergency stop", IntentName.EMERGENCY_STOP),
        ("Lock me in for 25 minutes", IntentName.START_SESSION),
        ("Give me my phone back", IntentName.RELEASE),
    ],
)
def test_older_commands_still_mean_what_they_meant(said, name):
    assert IntentClassifier().classify(said).name == name


async def test_the_voice_path_starts_and_answers_about_a_mission(rt):
    r = await rt.handle_utterance("I'm building a checkout for my shopping cart app", source="voice", stt_confidence=0.95)
    assert r.ok and rt.missions.active
    assert rt.missions.mission.objective == "I'm building a checkout for my shopping cart app"
    r = await rt.handle_utterance("What's next?", source="voice", stt_confidence=0.95)
    assert r.ok and "subtotal" in r.reply.lower() and "7" in r.reply
    r = await rt.handle_utterance("Run the tests", source="voice", stt_confidence=0.95)
    assert r.ok and rt.missions.mission.runs == 2
    r = await rt.handle_utterance("Drop the mission", source="voice", stt_confidence=0.95)
    assert r.ok and not rt.missions.active


async def test_mission_words_without_a_mission_stay_ordinary_sentences(rt):
    r = await rt.handle_utterance("Why is this failing", source="voice", stt_confidence=0.95)
    assert rt.missions.mission is None and "no mission" not in r.message.lower()


async def test_im_stuck_means_support_without_a_mission_and_a_hint_with_one(rt):
    r = await rt.handle_utterance("I'm stuck", source="voice", stt_confidence=0.95)
    assert r.ok and rt.missions.help_note is None and rt.personality_snapshot()["supportive_s_left"] > 250
    await rt.missions.start("fix the cart", origin="test")
    r = await rt.handle_utterance("I'm stuck", source="voice", stt_confidence=0.95)
    assert r.ok and rt.missions.help_note is not None and "850" in r.reply


async def test_a_stop_during_a_mission_still_stops_first(rt):
    await rt.missions.start("fix the cart", origin="test")
    await rt.arm()
    assert (await rt.manual_move("forward", 40, 0.9)).ok
    assert await wait_for(lambda: rt.transport.is_moving)
    await rt.handle_utterance("Stop the mission", source="voice", stt_confidence=0.95)
    assert await wait_for(lambda: not rt.transport.is_moving, timeout=0.3)
    assert not rt.missions.active


async def test_a_confirmed_reach_mentions_the_mission(rt):
    await rt.missions.start("fix the cart", origin="test")
    assert await wait_for(lambda: rt.speaker.spoken >= 1)
    rt.rs.conversation.anticipate = False
    before = len(rt.speaker.engine.spoken)
    await rt.handle_vision_event(VisionEvent(kind="confirmed", confidence=0.9, simulated=True))
    assert await wait_for(lambda: rt.last_reply and rt.last_reply["category"] == "reach_mission", timeout=3.0), rt.last_reply
    assert await wait_for(lambda: len(rt.speaker.engine.spoken) > before, timeout=3.0)
    line = rt.speaker.engine.spoken[-1].lower()
    assert "subtotal" in line or "7" in line, line
    assert rt.counters.reach_confirmed == 1 and rt.missions.mission.hp == 7, "a reach is recorded and changes nothing about the tests"


# ------------------------------------------------------------------ the API
async def test_only_the_local_dashboard_can_reach_the_mission_routes(settings):
    import httpx

    runtime = await make_runtime(settings, RecordingTTS())
    app = create_app(settings=settings, runtime=runtime)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8077") as c:
            evil = await c.post("/api/mission/demo-project", json={"reset": False}, headers={"origin": "https://evil.example"})
            assert evil.status_code == 403
            ok = await c.post("/api/mission/start", json={"objective": "fix the cart"}, headers={"origin": "http://127.0.0.1:8077"})
            assert ok.status_code == 200 and ok.json()["ok"]
            state = (await c.get("/api/state")).json()["quest"]
            assert state["mission"]["hp"] == 7 and state["character"]["name"] == "LOCKI"
            assert (await c.post("/api/mission/projects", json={"path": "/tmp", "confirm": False})).status_code == 400
            no_body = await c.post("/api/mission/abandon", content=b"", headers={"content-type": "text/plain"})
            assert no_body.status_code == 422, "a plain form post cannot drop a mission"


# ------------------------------------------------------------------ NeMo Relay (optional)
async def test_a_mission_recorded_by_real_relay_writes_its_own_file_and_no_prompts(settings, tts):
    pytest.importorskip("nemo_relay")
    s = settings.model_copy(update={"relay_enabled": True})
    rt = await make_runtime(s, tts)
    try:
        with_model(rt, ['{"action": "none", "args": {"title": "Fix the cart", "checkpoints": [{"title": "All", "tests": [1, 2, 3, 4, 5, 6, 7]}]}, "say": "Go."}'])
        await rt.missions.start("fix the secret-project cart", origin="test")
        assert rt.missions.relay.active, rt.missions.relay.error
        fix(rt, BUGGY_SUBTOTAL, FIXED_SUBTOTAL)
        await rt.missions.run_tests("test", code_changed=True)
        await fix_everything(rt)
        await rt.missions.run_tests("test", code_changed=True)
        assert rt.missions.mission.status == "won" and not rt.missions.relay.active
        rows = [json.loads(line) for line in rt.missions.relay.events_path.read_text().splitlines()]
        assert {r["atof_version"] for r in rows} == {"0.1"}
        tools = [r for r in rows if r.get("category") == "tool" and r.get("scope_category") == "end"]
        assert [r["name"] for r in tools] == ["run_pytest_in_sandbox", "run_pytest_in_sandbox", "run_pytest_in_sandbox"]
        assert any(r.get("category") == "llm" and r.get("scope_category") == "end" for r in rows)
        assert any(r.get("name") == "mission_won" for r in rows)
        text = rt.missions.relay.events_path.read_text()
        assert "secret-project" not in text and "MISSION PLANNING" not in text, "no prompts in Relay's file"
        ours = (rt.settings.data_dir / "missions" / rt.missions.mission.id / "trace.jsonl").read_text()
        assert "atof_version" not in ours, "our log is not presented as a Relay trace"
        assert rt.missions.snapshot()["relay"]["installed"]
    finally:
        await rt.shutdown()


async def test_a_relay_failure_never_touches_the_mission(settings, tts, monkeypatch):
    s = settings.model_copy(update={"relay_enabled": True})
    rt = await make_runtime(s, tts)
    try:
        import app.missions.relay as relay_module

        monkeypatch.setattr(relay_module, "installed", lambda: False)
        r = await rt.missions.start("fix the cart", origin="test")
        assert r.ok and rt.missions.mission.hp == 7 and not rt.missions.relay.active and "not installed" in rt.missions.relay.error
        fix(rt, BUGGY_SUBTOTAL, FIXED_SUBTOTAL)
        assert (await rt.missions.run_tests("test", code_changed=True)).conclusive and rt.missions.mission.hp == 6
    finally:
        await rt.shutdown()


async def test_a_relay_error_mid_tool_still_gives_the_tool_result_once():
    pytest.importorskip("nemo_relay")
    from app.missions.relay import RelayRecorder

    class Broken:
        class tools:
            @staticmethod
            async def execute(*a, **k):
                raise RuntimeError("relay blew up")

    calls = []

    async def fn():
        calls.append(1)
        return "result"

    rec = RelayRecorder()
    rec._relay, rec._handle = Broken, object()
    assert await rec.tool("x", {}, fn) == "result" and calls == [1] and not rec.active and "blew up" in rec.error


# ------------------------------------------------------------------ it listens: quiet, pause, phone
async def test_quiet_mode_silences_the_cheering_but_not_the_record(rt):
    await rt.missions.start("fix the cart", origin="test")
    assert await wait_for(lambda: rt.speaker.spoken >= 2 and not rt.speaker.speaking)
    rt.companion.context.set_quiet(10)
    before = len(rt.speaker.engine.spoken)
    fix(rt, BUGGY_SUBTOTAL, FIXED_SUBTOTAL)
    await rt.missions.run_tests("test", code_changed=True)
    assert rt.missions.mission.hp == 6 and len(rt.speaker.engine.spoken) == before


async def test_winning_is_said_even_in_quiet_mode(rt):
    await rt.missions.start("fix the cart", origin="test")
    rt.companion.context.set_quiet(10)
    await fix_everything(rt)
    await rt.missions.run_tests("test", code_changed=True)
    assert rt.missions.mission.status == "won"
    assert await wait_for(lambda: any("Boss" in s for s in rt.speaker.engine.spoken))


async def test_a_paused_session_stops_the_watcher_until_it_resumes(settings, tts):
    rt = await make_runtime(settings, tts)
    try:
        await rt.missions.start("fix the cart", origin="test")
        m = rt.missions.mission
        assert (await rt.pause_session(origin="test", speak=False)).ok
        fix(rt, BUGGY_SUBTOTAL, FIXED_SUBTOTAL)
        assert not await wait_for(lambda: m.hp < 7, timeout=3.5), "tests ran while paused"
        assert (await rt.resume_session(origin="test", speak=False)).ok
        assert await wait_for(lambda: m.hp == 6, timeout=6.0), "the save was forgotten"
    finally:
        await rt.shutdown()


async def test_taking_the_phone_back_is_not_blocked_by_a_mission(rt):
    await rt.missions.start("fix the cart", origin="test")
    r = await rt.handle_utterance("Give me my phone back", source="voice", stt_confidence=0.95)
    assert r.ok and rt.missions.active, "the phone is always theirs, and the mission is still there"
