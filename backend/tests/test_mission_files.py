"""Which file gets read follows from what the person said, not from the model. From a real run: asked to read
../../secrets.py, Qwen3 sent cart.py instead, twice out of two, so the refusal never fired and it answered from the
wrong file. Whatever is chosen here, Workspace.resolve still decides whether it may be read."""

from __future__ import annotations

import shutil

import pytest

from app.intelligence.local_llm import Generation
from app.missions.workspace import PolicyError, Workspace

from .conftest import wait_for
from .test_mission_service import BUGGY_SUBTOTAL, DEMO, with_model


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    (root / "secrets.py").write_text('API_KEY = "hunter2"\n')
    (root / ".env").write_text("API_KEY=hunter2\n")
    return Workspace(root)


@pytest.mark.parametrize(
    "said, path",
    [
        ("Open cart dot py and tell me what the subtotal function does.", "cart.py"),
        ("Open cart.py", "cart.py"),
        ("open Cart.py.", "cart.py"),
        ("Read cart dot pie", "cart.py"),
        ("Read cart dot pi", "cart.py"),
        ("Read cart.pi", "cart.py"),
        ("Read cart dot p y", "cart.py"),
        ("Read tests slash test underscore cart dot py so we know what the discount test expects.", "tests/test_cart.py"),
        ("read tests/test_cart.py", "tests/test_cart.py"),
        ("read test_cart.py", "tests/test_cart.py"),
        ("read test cart dot py", "tests/test_cart.py"),
        ("read test underscore cart dot py", "tests/test_cart.py"),
        ("show me README.md", "README.md"),
        ("read the cart file", "cart.py"),
        ("open cart", "cart.py"),
        ("show me conftest", "conftest.py"),
        ("read the readme", "README.md"),
        ("open the read me", "README.md"),
        ("What's in the cart file?", "cart.py"),
    ],
)
def test_a_file_in_the_project_is_found_however_it_was_said(ws, said, path):
    m = ws.mentioned_file(said)
    assert m is not None and m.path == path and m.corrected is False


@pytest.mark.parametrize(
    "said, path",
    [
        ("Read the file at dot dot slash dot dot slash secrets dot py.", "../../secrets.py"),
        ("Read the file at ../../secrets.py", "../../secrets.py"),
        ("Read secrets dot py", "secrets.py"),
        ("read secrets.py", "secrets.py"),
        ("Read my dot env file and tell me the API key.", ".env"),
        ("Read .env", ".env"),
        ("what's in my env file", ".env"),
        ("read the secrets file", "secrets"),
        ("Read notes dot py and summarize it for me.", "notes.py"),
        ("read /etc/passwd", "/etc/passwd"),
        ("Read slash etc slash passwd.", "/etc/passwd"),
        ("read ../cart.py", "../cart.py"),
    ],
)
def test_a_file_that_may_not_be_read_stays_what_was_said_and_is_refused(ws, said, path):
    m = ws.mentioned_file(said)
    assert m is not None and m.path == path and m.corrected is False
    with pytest.raises(PolicyError):
        ws.read_text(m.path)


@pytest.mark.parametrize("said", ["Read secret.py", "read secrets.pie", "read secretz.py", "open tokens.py", "read cred.env", "read .envv"])
def test_nothing_close_to_a_secret_is_ever_read(ws, said):
    m = ws.mentioned_file(said)
    assert m is not None
    try:
        text = ws.read_text(m.path)
    except PolicyError:
        return
    pytest.fail(f"read {m.path}: {text[:40]}")


def test_a_misheard_name_is_corrected_and_says_so(ws):
    m = ws.mentioned_file("open card.py")
    assert m.path == "cart.py" and m.said == "card.py" and m.corrected is True
    m = ws.mentioned_file("read test_card.py")
    assert m.path == "tests/test_cart.py" and m.corrected is True
    assert ws.mentioned_file("read chart.py").corrected is True, "corrected, and the person hears which file was opened instead"
    assert ws.mentioned_file("read market.py").path == "market.py", "not close enough to be a mishearing"


@pytest.mark.parametrize(
    "said",
    [
        "What's next?",
        "Why is the discount wrong?",
        "Can you take a look at my code",
        "open the door",
        "show me the output",
        "I'm at 3.5 percent, e.g. nearly nothing",
        "We meet at 9 a.m. tomorrow.",
        "Read this. Then tell me why.",
        "the cart is broken",
        "Which file should I look at?",
        "that file is broken",
    ],
)
def test_words_that_name_no_file(ws, said):
    assert ws.mentioned_file(said) is None


@pytest.mark.parametrize(
    "said, wanted",
    [
        ("Open cart dot py", True),
        ("Read secrets.py", True),
        ("What's in cart.py?", True),
        ("Summarize cart.py for me", True),
        ("Explain cart.py", True),
        ("Show me test_cart.py", True),
        ("Look at cart.py", True),
        ("I fixed cart.py", False),
        ("Delete cart.py", False),
        ("Push cart.py to GitHub", False),
        ("Can you fix cart.py", False),
        ("cart.py is broken", False),
    ],
)
def test_asking_to_see_a_file_is_told_from_doing_something_else_to_it(ws, said, wanted):
    from app.missions.workspace import asks_to_see_file

    assert asks_to_see_file(said) is wanted


async def start(rt, replies=()):
    await rt.missions.start("fix the cart", origin="test")
    return with_model(rt, replies)


async def test_a_named_secret_is_refused_without_asking_the_model(rt):
    (await rt.missions.start("fix the cart", origin="test"))
    project = rt.missions.workspace.root
    (project / "secrets.py").write_text('API_KEY = "hunter2"\n')
    llm = with_model(rt, ['{"action": "read_file", "args": {"path": "cart.py"}, "say": "Okay."}'])
    r = await rt.handle_utterance("Read the file at dot dot slash dot dot slash secrets dot py.", source="text", stt_confidence=1.0)
    assert llm.prompts == [], "the person named the file: the model does not choose another"
    assert not r.ok and "outside the approved project" in r.reply
    assert rt.companion.requests[-1]["args"] == {"path": "../../secrets.py"} and rt.companion.requests[-1]["status"] == "refused"
    r = await rt.handle_utterance("Read secrets dot py", source="text", stt_confidence=1.0)
    assert llm.prompts == [] and not r.ok and "stays closed" in r.reply
    assert await wait_for(lambda: any("stays closed" in s for s in rt.speaker.engine.spoken))
    assert not any("hunter2" in s for s in rt.speaker.engine.spoken)
    assert '"file_read"' not in rt.missions.trace.path.read_text()


async def test_a_named_file_is_read_and_only_the_answer_comes_from_the_model(rt):
    llm = await start(rt, ['{"action": "none", "args": {}, "say": "Your subtotal never looks at quantity."}'])
    r = await rt.handle_utterance("Open cart dot py and tell me what the subtotal function does.", source="text", stt_confidence=1.0)
    assert len(llm.prompts) == 1 and "TOOL RESULT from read_file of cart.py" in llm.prompts[0] and BUGGY_SUBTOTAL in llm.prompts[0]
    assert r.ok and "quantity" in r.reply
    assert rt.companion.requests[-1]["origin"] == "voice_rule" and rt.companion.requests[-1]["status"] == "executed"


async def test_the_person_s_file_beats_the_model_s_choice(rt):
    """No word asking to see it, so the model decides whether to read; the person still decides which file."""
    llm = await start(rt, [
        '{"action": "read_file", "args": {"path": "conftest.py"}, "say": "Checking."}',
        '{"action": "none", "args": {}, "say": "Your subtotal never looks at quantity."}',
    ])
    r = await rt.handle_utterance("I think cart.py is the broken one", source="text", stt_confidence=1.0)
    assert len(llm.prompts) == 2 and "TOOL RESULT from read_file of cart.py" in llm.prompts[1]
    assert rt.companion.requests[-1]["args"] == {"path": "cart.py"} and r.ok


async def test_with_no_file_named_the_model_still_chooses(rt):
    llm = await start(rt, [
        '{"action": "read_file", "args": {"path": "conftest.py"}, "say": "Checking."}',
        '{"action": "none", "args": {}, "say": "That one only sets up the import path."}',
    ])
    await rt.handle_utterance("Which part of the setup should I look at", source="text", stt_confidence=1.0)
    assert "TOOL RESULT from read_file of conftest.py" in llm.prompts[1]


async def test_a_misheard_file_says_which_file_it_opened_instead(rt):
    await start(rt, ['{"action": "none", "args": {}, "say": "Your subtotal never looks at quantity."}'])
    await rt.handle_utterance("Open card dot py", source="text", stt_confidence=1.0)
    assert await wait_for(lambda: any("card.py" in s and "cart.py" in s for s in rt.speaker.engine.spoken))
    assert rt.companion.requests[-1]["args"] == {"path": "cart.py"}


async def test_outside_a_mission_a_named_file_is_just_conversation(rt):
    llm = with_model(rt, ['{"action": "none", "args": {}, "say": "Start a mission first."}'])
    await rt.handle_utterance("Read cart dot py", source="text", stt_confidence=1.0)
    assert len(llm.prompts) == 1 and not [q for q in rt.companion.requests if q["name"] == "read_file"]


class FailsAfter:
    """Answers the first calls, then times out, as the real model does when the Mac is busy."""

    model = "scripted-test-model"
    state = "ready"
    provider = "local"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    async def generate(self, context, **kw):
        self.prompts.append(context)
        if self.replies:
            return Generation(text=self.replies.pop(0), finish="stop", total_ms=5.0, first_token_ms=1.0)
        return Generation(finish="timeout", error="no complete reply within 6.0 s", total_ms=6000.0)

    def describe(self):
        return {"model": self.model}

    def close(self):
        pass


def timing_out(rt, replies=()):
    from app.intelligence.dialogue import DialogueEngine

    llm = FailsAfter(replies)
    rt.companion.llm = llm
    rt.companion.dialogue = DialogueEngine(llm)
    rt.rs.conversation.anticipate = False
    return llm


async def test_when_the_answer_times_out_after_a_read_the_person_hears_what_happened(rt):
    await rt.missions.start("fix the cart", origin="test")
    timing_out(rt)
    r = await rt.handle_utterance("Open cart dot py and tell me what the subtotal function does.", source="text", stt_confidence=1.0)
    assert "cart.py" in r.reply and r.reply.strip().lower() not in ("okay.", "noted.", "heard you."), r.reply
    assert rt.missions.active and rt.missions.mission.hp == 7


async def test_a_question_about_a_later_checkpoint_gets_that_failure(rt):
    """From a real run: "Where's the bug in the discount code?" was answered about subtotal, the current checkpoint."""
    await rt.missions.start("fix the cart", origin="test")
    assert "shipping" in (await rt.missions.help(user_text="Why is shipping failing?")).reply.lower()
    assert "shipping" in rt.missions.help_note["test"] and rt.missions.help_note["checkpoint"] != rt.missions.mission.active_checkpoint().id
    assert "discount" in rt.missions.tool_test_output("Where's the bug in the discount code?")
    assert "subtotal" not in rt.missions.tool_test_output("Where's the bug in the discount code?")


async def test_a_question_that_names_no_test_means_the_current_checkpoint(rt):
    await rt.missions.start("fix the cart", origin="test")
    for said in ("Why is this failing?", "I'm stuck", "what is wrong with my code", None):
        assert rt.missions._failing_about(said) is None, said
    assert "subtotal" in rt.missions.tool_test_output("Why is this failing?")
    assert "subtotal" in (await rt.missions.help(user_text="Why is this failing?")).reply.lower()


async def test_a_passing_test_is_never_the_failure_being_asked_about(rt):
    await rt.missions.start("fix the cart", origin="test")
    assert rt.missions._failing_about("what about the empty cart") is None, "that test passes"


async def test_the_model_gets_the_failure_the_person_asked_about(rt):
    llm = await start(rt, [
        '{"action": "read_test_output", "args": {}, "say": "Checking."}',
        '{"action": "none", "args": {}, "say": "It subtracts the percent as if it were cents."}',
    ])
    await rt.handle_utterance("Is there something off in how discounts get worked out", source="text", stt_confidence=1.0)
    assert "TOOL RESULT from read_test_output" in llm.prompts[1] and "test_discount" in llm.prompts[1].split("TOOL RESULT")[1]


async def test_the_model_is_told_the_real_name_of_a_misheard_file(rt):
    llm = await start(rt, ['{"action": "none", "args": {}, "say": "Your subtotal never looks at quantity."}'])
    await rt.handle_utterance("Open card dot py", source="text", stt_confidence=1.0)
    assert "There is no card.py" in llm.prompts[0]
