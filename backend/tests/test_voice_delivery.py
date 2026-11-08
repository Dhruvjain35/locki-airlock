"""One voice in every security mode, with the delivery changing: the warning and the next reply sound like the same speaker, a DEFENSIVE line is
slower and firmer, OWNER_RETURNED is the usual voice again at once, and every security line has short, evidence-only variants."""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.voice.delivery import CATEGORY_MODE, MODE_SPEED, speed_for
from app.voice.kokoro_tts import KokoroTTS
from app.voice.personality import LIBRARY
from app.voice.tts import P0_SAFETY, NullTTS, SpeechRequest, TTSEngine, VoiceRouter

from .conftest import wait_for


# ------------------------------------------------------------------ the policy
def test_each_security_line_is_spoken_in_its_own_mode():
    assert speed_for("security_warning_interaction", "NORMAL") == MODE_SPEED["DEFENSIVE"] < 1.0
    assert speed_for("security_warning_usb", "NORMAL") == MODE_SPEED["CRITICAL"]
    assert speed_for("security_warning_presence", "CRITICAL") == MODE_SPEED["WATCHFUL"]
    assert speed_for("security_owner_seen", "DEFENSIVE") is None  # the usual voice again, at once
    assert speed_for("security_owner_seen_incident", "CRITICAL") is None  # even while the open incident keeps the mode CRITICAL


def test_any_other_line_follows_the_current_mode():
    assert speed_for("generated", "DEFENSIVE") == MODE_SPEED["DEFENSIVE"]
    assert speed_for("reply", "NORMAL") is None and speed_for("reply", None) is None and speed_for("reply", "nonsense") is None


# ------------------------------------------------------------------ Kokoro renders the same text at another speed as another clip
class FakeKokoroModel:
    def __init__(self):
        self.calls = []

    def create(self, text, voice, speed):
        self.calls.append((text, voice, speed))
        return np.full(2400, 0.1 * speed, dtype=np.float32), 24000

    def get_voices(self):
        return ["am_onyx", "af_bella"]


def ready_kokoro(voice="am_onyx"):
    k = KokoroTTS(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()), voice=voice)
    k._k, k.state = FakeKokoroModel(), "ready"
    return k


def test_a_line_at_another_speed_is_its_own_clip():
    k = ready_kokoro()
    k.render("Step away from the computer.", persist=True, speed=0.88)
    assert k._k.calls[-1] == ("Step away from the computer.", "am_onyx", 0.88)
    assert k.clip_for("Step away from the computer.", speed=0.88) is not None
    assert k.clip_for("Step away from the computer.") is None  # the usual speed was never rendered
    assert k._path("x", 0.88) != k._path("x") and k._path("x", None) == k._path("x", k.speed)


def test_prewarm_renders_each_line_at_its_speed_and_keeps_it_on_disk():
    k = ready_kokoro()
    k.prewarm([("Stop.", 0.93), "Welcome back."])
    assert ("Stop.", "am_onyx", 0.93) in k._k.calls and ("Welcome back.", "am_onyx", 1.0) in k._k.calls
    fresh = KokoroTTS(Path(tempfile.mkdtemp()), k.cache_dir, voice="am_onyx")
    assert fresh.clip_for("Stop.", speed=0.93) is not None  # read back from disk


# ------------------------------------------------------------------ with security on, the router never switches to the cloud voice
class Profile:
    provider, reference_id, volume, local_voice = "fish_audio", "voice-1", 1.0, "kokoro"


class Cloud:
    def __init__(self):
        self.asked = 0

    def text_for(self, req, p):
        return req.text

    def cached(self, text, p):
        self.asked += 1
        return np.zeros(10, dtype=np.int16), 24000


class Audio:
    last_start_mono = last_latency_ms = None

    def __init__(self):
        self.played = []

    async def play(self, pcm, sr, device=None, volume=1.0):
        self.played.append(pcm)


async def test_with_one_voice_a_cached_cloud_clip_is_not_used_and_the_line_keeps_its_speed():
    k, cloud, audio = ready_kokoro(), Cloud(), Audio()
    router = VoiceRouter(profile=lambda: Profile, system=NullTTS(), cloud=cloud, audio=audio, neural=k)
    router.local_only = lambda: True
    result = await router.speak_request(SpeechRequest("Step away from the computer.", priority=P0_SAFETY, speed=0.88))
    assert cloud.asked == 0 and result.source == "kokoro_live" and k._k.calls[-1][2] == 0.88


async def test_without_security_the_cloud_clip_is_still_preferred():
    k, cloud, audio = ready_kokoro(), Cloud(), Audio()
    router = VoiceRouter(profile=lambda: Profile, system=NullTTS(), cloud=cloud, audio=audio, neural=k)
    result = await router.speak_request(SpeechRequest("Hello."))
    assert cloud.asked == 1 and result.source == "fish_cache"


# ------------------------------------------------------------------ the runtime: the voice, the speed and the style follow the mode
class Requests(TTSEngine):
    name = "requests"

    def __init__(self):
        self.reqs: list[SpeechRequest] = []

    def available(self):
        return True

    async def speak(self, text):
        await asyncio.sleep(0)

    async def speak_request(self, req):
        self.reqs.append(req)
        await asyncio.sleep(0)
        from app.voice.tts import SpeechResult

        return SpeechResult("kokoro_cache")


class Mode:
    def __init__(self, value):
        self.value = value


def secured(rt, mode="NORMAL"):
    engine = Requests()
    rt.speaker.engine = engine
    rt.security.enabled = True
    rt.security.modes.state.current_security_mode = type(rt.security.modes.state.current_security_mode)(mode)
    return engine


async def test_a_defensive_warning_is_slower_and_firmer_in_the_same_voice(rt):
    engine = secured(rt, "DEFENSIVE")
    rt._say("security_warning_interaction", force=True)
    assert await wait_for(lambda: engine.reqs)
    assert engine.reqs[-1].speed == MODE_SPEED["DEFENSIVE"]
    assert rt.style().tone == "serious"
    assert rt._local_voice() == rt.settings.security_voice == "am_onyx"
    assert rt._cached_line_check() is None  # never the cloud voice while security is on


async def test_when_the_owner_returns_the_usual_voice_and_tone_come_back_at_once(rt):
    engine = secured(rt, "OWNER_RETURNED")
    rt._say("security_owner_seen")
    assert await wait_for(lambda: engine.reqs)
    assert engine.reqs[-1].speed is None and rt.style().tone != "serious"


async def test_watchful_replies_are_calm_not_serious(rt):
    secured(rt, "WATCHFUL")
    assert rt.style().tone == "gentle"


async def test_with_security_off_the_owners_voice_is_kept(rt):
    rt.security.enabled = False
    assert rt._local_voice() == rt.rs.voice_profile.kokoro_voice


# ------------------------------------------------------------------ wording: short, varied, and only what the evidence says
URGENT = [c for c, m in CATEGORY_MODE.items() if m in ("DEFENSIVE", "CRITICAL")]


@pytest.mark.parametrize("category", sorted(CATEGORY_MODE))
def test_each_security_line_has_variants_that_accuse_no_one(category):
    lines = [ln.text for ln in LIBRARY[category]]
    assert len(set(lines)) >= 3, category
    for text in lines:
        assert not re.search(r"\b(hacker|intruder|thief|criminal|police|recorded|recording)\b", text, re.I), text


@pytest.mark.parametrize("category", sorted(URGENT))
def test_urgent_lines_are_short_commands(category):
    for ln in LIBRARY[category]:
        words = len(ln.text.split())
        assert words <= 18, (category, ln.text)
        assert re.search(r"\b(step away|hands off|stop|move away|back away)\b", ln.text, re.I), ln.text


async def test_a_warning_spoken_twice_in_a_row_is_worded_differently(rt):
    engine = secured(rt, "CRITICAL")
    rt._say("security_warning_usb", force=True)
    await wait_for(lambda: len(engine.reqs) == 1)
    rt.speaker._recent.clear()  # the speaker's own duplicate window is a different rule
    rt._say("security_warning_usb", force=True)
    await wait_for(lambda: len(engine.reqs) == 2)
    assert engine.reqs[0].text != engine.reqs[1].text
