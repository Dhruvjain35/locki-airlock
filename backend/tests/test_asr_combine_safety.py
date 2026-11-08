"""Milestone 5.6 audit of the two-recogniser path: a stop heard by either recogniser must reach
the runtime's established stop rules, never weaker and never later because of the other one.
Pure logic, fake engines and the real runtime with a simulated robot; nothing plays."""

from __future__ import annotations

import itertools
import time

import numpy as np
import pytest

from app.config import RuntimeSettings
from app.voice.asr_combine import PARAKEET_ONLY_CONFIDENCE, combine
from app.voice.intents import IntentClassifier, IntentName
from app.voice.transcription import CombinedTranscriber

STOPS = {IntentName.STOP_MOTION, IntentName.EMERGENCY_STOP}
AUDIO = np.zeros(16000, dtype=np.float32)


def intent(text: str) -> IntentName | None:
    return IntentClassifier().classify(text).name if text else None


READINGS = ["", "Stop.", "Emergency stop!", "Pause my session.", "Come back here.", "I have a calculus test tomorrow.",
            "Thank you.", "[SOUND]", "Top."]


@pytest.mark.parametrize(("w", "p"), list(itertools.product(READINGS, READINGS)))
@pytest.mark.parametrize("wc", [0.1, 0.3, 0.6, 0.95])
def test_every_pair_of_readings_keeps_the_stop_and_motion_rules(w, p, wc):
    text, conf, why = combine(w, wc, p)
    heard = {intent(w), intent(p)}
    if heard & STOPS:  # a stop heard by either recogniser is the transcript
        assert intent(text) in STOPS, (w, p, text, why)
    if intent(text) == IntentName.RETURN_ROBOT:  # motion only when both heard it, then the runtime gate decides
        assert intent(w) == intent(p) == IntentName.RETURN_ROBOT and conf <= max(wc, PARAKEET_ONLY_CONFIDENCE)
    assert conf < RuntimeSettings().voice.min_motion_command_confidence or (conf == wc and intent(w) == intent(text))


@pytest.mark.parametrize("wc", [0.1, 0.3, 0.6])
def test_both_recognisers_agreeing_is_never_weaker_than_parakeet_alone(wc):
    for said in ("Emergency stop!", "Stop.", "Pause my session."):
        agree = combine(said, wc, said)[1]
        parakeet_alone = combine("", 0.0, said)[1] if intent(said) not in STOPS else combine("uh", 0.9, said)[1]
        assert agree >= parakeet_alone, (said, wc, agree, parakeet_alone)


def test_a_whisper_phantom_never_overrules_a_real_command():
    for phantom in ("Thank you.", "Bye.", "[SOUND]", "(tapping)"):
        text, _conf, why = combine(phantom, 0.98, "Pause my session.")
        assert text == "Pause my session.", (phantom, why)


async def test_an_emergency_stop_both_heard_latches_even_when_whisper_was_unsure(rt):
    text, conf, _ = combine("Emergency stop!", 0.3, "Emergency stop!")
    await rt.handle_utterance(text, source="voice", stt_confidence=conf)
    assert rt.safety.estop_latched


class SlowEngine:
    def __init__(self, text: str, delay_s: float, state: str = "ready", confidence: float | None = 0.9):
        self.text, self.delay_s, self.state, self.confidence, self.calls = text, delay_s, state, confidence, 0

    def transcribe(self, audio, timeout_s: float = 10.0):
        self.calls += 1
        time.sleep(self.delay_s)
        out = {"text": self.text, "roundtrip_ms": self.delay_s * 1000}
        if self.confidence is not None:
            out["confidence"] = self.confidence
        return out

    def describe(self) -> dict:
        return {"state": self.state}

    def close(self) -> None:
        pass


def timed(pair, audio=AUDIO):
    t = time.perf_counter()
    r = pair.transcribe(audio)
    return r, time.perf_counter() - t


def test_a_stop_from_whisper_is_not_held_back_by_a_stalled_parakeet():
    r, s = timed(CombinedTranscriber(SlowEngine("Stop.", 0.05), SlowEngine("", 3.0, confidence=None)))
    assert intent(r["text"]) == IntentName.STOP_MOTION and s < 0.6, (r, s)


def test_a_stop_from_parakeet_is_not_held_back_by_a_slow_whisper():
    r, s = timed(CombinedTranscriber(SlowEngine("", 3.0), SlowEngine("Emergency stop!", 0.05, confidence=None)))
    assert intent(r["text"]) == IntentName.EMERGENCY_STOP and r["confidence"] >= PARAKEET_ONLY_CONFIDENCE and s < 0.6, (r, s)


def test_a_stalled_parakeet_costs_at_most_a_short_grace_then_whisper_decides():
    pair = CombinedTranscriber(SlowEngine("Pause my session.", 0.05), SlowEngine("", 4.0, confidence=None))
    r, s = timed(pair)
    assert r["text"] == "Pause my session." and s < 1.6 and "Parakeet" in r["decided_by"], (r, s)
    r2, s2 = timed(pair)  # Parakeet is still stuck on the first clip: whisper alone, at once
    assert r2["text"] == "Pause my session." and s2 < 0.6, (r2, s2)


class CrashingEngine(SlowEngine):
    def transcribe(self, audio, timeout_s: float = 10.0):
        raise BrokenPipeError("worker process gone")


def test_a_crashed_engine_leaves_the_other_one_working():
    r, _ = timed(CombinedTranscriber(CrashingEngine("", 0.0), SlowEngine("Stop.", 0.05, confidence=None)))
    assert intent(r["text"]) == IntentName.STOP_MOTION
    r, _ = timed(CombinedTranscriber(SlowEngine("Pause my session.", 0.05), CrashingEngine("", 0.0, confidence=None)))
    assert r["text"] == "Pause my session." and r["decided_by"].startswith("whisper.cpp only")
