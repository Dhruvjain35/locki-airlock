"""With the SECURITY block in the prompt, what the model actually says passes the investigator's own checks (security/tools.py) before it is spoken:
no verdict or accusation, no lock the record does not confirm, no notification nobody sent. The first sentence that fails is not spoken, nor anything
after it; the record itself is said instead. A sentence that denies the claim ("nothing proves it's a hacker") is fine, and so is any word with
security off."""

from __future__ import annotations

from app.intelligence.dialogue import DialogueResult
from app.intelligence.local_llm import Generation

from .conftest import wait_for
from .test_security_service import CODE, Harness, say as _say


async def say(rt, text):
    r = await _say(rt, text)
    await wait_for(lambda: rt.speaker.spoken > 0 and not rt.speaker._items and not rt.speaker.speaking, timeout=3)  # everything queued has played
    return r


def scripted(rt, sentences, *, stream=True):
    """The model's reply, sentence by sentence as the real dialogue engine hands them to the voice."""

    async def respond(context, *, on_sentence=None, **_):
        text = " ".join(sentences)
        if stream and on_sentence is not None:
            for s in sentences:
                on_sentence(s)
        return DialogueResult(say=text, streamed=list(sentences) if stream else [], generation=Generation(text=text, finish="stop"))

    class Dialogue:
        available, llm = True, None

    d = Dialogue()
    d.respond = respond
    rt.companion.dialogue = d


async def armed(rt):
    h = Harness(rt)
    await rt.security.arm(CODE)
    return h


async def test_an_accusation_is_never_spoken_and_the_record_is_said_instead(rt):
    h = await armed(rt)
    scripted(rt, ["Someone plugged something in.", "That was probably a hacker.", "I'd call the police."])
    r = await say(rt, "Tell me honestly who you think was sitting at my desk while I was gone")
    text = " ".join(h.spoken()) + " " + (r.reply or "")
    assert "Someone plugged something in." in h.spoken()
    assert "hacker" not in text and "police" not in text
    assert any("since you armed" in s or "does not prove" in s for s in h.spoken())  # describe_record(), no model


async def test_a_lock_the_record_does_not_confirm_is_never_claimed(rt):
    h = await armed(rt)
    scripted(rt, ["Don't worry.", "The screen is locked now."])
    await say(rt, "Tell me honestly whether my laptop is safe right now while I am away from it")
    assert "Don't worry." in h.spoken() and not any("locked now" in s for s in h.spoken())


async def test_a_notification_nobody_sent_is_never_claimed(rt):
    h = await armed(rt)
    scripted(rt, ["The owner has been notified."])
    await say(rt, "Tell me honestly whether you told anybody else about what happened at my desk")
    assert h.spoken() and not any("notified" in s for s in h.spoken())


async def test_a_sentence_that_denies_the_claim_is_spoken(rt):
    h = await armed(rt)
    scripted(rt, ["Nothing proves it was a hacker.", "It was just a new USB device."])
    await say(rt, "Tell me honestly whether you think it was some kind of hacker at my desk today")
    assert "Nothing proves it was a hacker." in h.spoken() and "It was just a new USB device." in h.spoken()


async def test_a_reply_that_was_not_streamed_is_checked_too(rt):
    h = await armed(rt)
    scripted(rt, ["An intruder was at your desk."], stream=False)
    r = await say(rt, "Tell me honestly what you think was going on at my desk while I stepped out")
    assert not any("intruder" in s for s in h.spoken()) and "intruder" not in (r.reply or "")


async def test_with_security_off_the_reply_is_not_policed(rt):
    rt.security.enabled = False
    scripted(rt, ["That movie's hacker scene was great."])
    await say(rt, "Tell me honestly whether you liked the movie we were talking about earlier tonight")
    assert "That movie's hacker scene was great." in rt.speaker.engine.spoken
