"""Combining Parakeet (NVIDIA, NeMo-Speech.cpp) and whisper.cpp readings of the same audio
(app/voice/asr_combine.py, app/voice/transcription.py). Pure logic and fake engines; the real
Parakeet server is exercised only when its files are downloaded."""

from __future__ import annotations

import shutil
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from app.config import RuntimeSettings
from app.voice import nemo_asr
from app.voice.asr_combine import PARAKEET_ONLY_CONFIDENCE, combine
from app.voice.intents import IntentClassifier, IntentName
from app.voice.transcription import CombinedTranscriber

MODELS = Path(__file__).resolve().parents[1] / "models"


def intent(text: str) -> IntentName | None:
    return IntentClassifier().classify(text).name if text else None


def test_parakeet_hearing_nothing_beats_a_whisper_phantom():
    assert combine("Get out of here.", 0.71, "")[:2] == ("", 0.0)
    assert combine("Thank you.", 0.98, "  ")[:2] == ("", 0.0)  # "thanks" would otherwise be an intent


def test_a_stop_heard_only_by_whisper_is_still_a_stop():
    assert combine("Stop.", 0.6, "")[:2] == ("Stop.", 0.6)
    assert combine("Emergency stop!", 0.9, "Top.")[0] == "Emergency stop!"


def test_agreement_takes_parakeets_words_and_whispers_confidence():
    assert combine("Pause my session.", 0.91, "Pause my session.") == ("Pause my session.", 0.91, "agree")
    text, conf, why = combine("I have a calculus test to morrow", 0.8, "I have a calculus test tomorrow.")
    assert (text, conf, why) == ("I have a calculus test tomorrow.", 0.8, "agree")


def test_a_command_beats_a_misheard_non_command_from_either_side():
    assert combine("Cause my session.", 0.7, "Pause my session.")[:2] == ("Pause my session.", PARAKEET_ONLY_CONFIDENCE)
    assert combine("Give me my phone back.", 0.88, "Liven my zone track.")[:2] == ("Give me my phone back.", 0.88)


def test_motion_is_never_taken_from_a_disagreement():
    assert intent("Come back to me.") == IntentName.RETURN_ROBOT and intent("Calm, black tea.") == IntentName.UNKNOWN
    for whisper_text, parakeet_text in (("Come back to me.", "Calm, black tea."), ("Calm, black tea.", "Come back to me.")):
        text, _conf, _why = combine(whisper_text, 0.97, parakeet_text)
        assert intent(text) != IntentName.RETURN_ROBOT


def test_motion_heard_by_both_still_needs_the_runtime_motion_gate():
    text, conf, why = combine("come back here", 0.62, "Come back here.")
    assert why == "agree" and intent(text) == IntentName.RETURN_ROBOT
    assert conf == 0.62 < RuntimeSettings().voice.min_motion_command_confidence


def test_a_parakeet_only_reading_acts_on_commands_but_can_never_move_the_robot():
    v = RuntimeSettings().voice
    assert v.min_command_confidence <= PARAKEET_ONLY_CONFIDENCE < v.min_motion_command_confidence


class FakeEngine:
    def __init__(self, state: str, result: dict | None):
        self.state, self.result, self.calls, self.closed = state, result, 0, False

    def transcribe(self, audio, timeout_s: float = 10.0):
        self.calls += 1
        return dict(self.result) if self.result else None

    def describe(self) -> dict:
        return {"state": self.state}

    def close(self) -> None:
        self.closed = True


AUDIO = np.zeros(16000, dtype=np.float32)


def test_the_pair_transcribes_with_both_and_records_why():
    w = FakeEngine("ready", {"text": "Cause my session.", "confidence": 0.7, "roundtrip_ms": 80.0, "audio_s": 1.0})
    p = FakeEngine("ready", {"text": "Pause my session.", "roundtrip_ms": 40.0})
    pair = CombinedTranscriber(w, p)
    r = pair.transcribe(AUDIO)
    assert (r["text"], r["confidence"], r["decided_by"]) == ("Pause my session.", PARAKEET_ONLY_CONFIDENCE, "command heard by Parakeet")
    assert r["readings"]["whisper"]["text"] == "Cause my session." and r["readings"]["parakeet"]["ms"] == 40.0
    assert w.calls == p.calls == 1 and r["roundtrip_ms"] >= 0 and r["engine"] == "parakeet+whisper"
    assert pair.describe()["decisions"] == {"command heard by Parakeet": 1}
    pair.close()
    assert w.closed and p.closed


def test_without_parakeet_the_pair_is_exactly_whisper():
    w = FakeEngine("ready", {"text": "Pause my session.", "confidence": 0.7, "roundtrip_ms": 80.0})
    p = FakeEngine("unavailable", None)
    r = CombinedTranscriber(w, p).transcribe(AUDIO)
    assert (r["text"], r["confidence"]) == ("Pause my session.", 0.7) and r["decided_by"].startswith("whisper.cpp only") and p.calls == 0


def test_without_whisper_a_parakeet_reading_never_passes_the_motion_gate():
    r = CombinedTranscriber(FakeEngine("error", None), FakeEngine("ready", {"text": "Come back here.", "roundtrip_ms": 40.0})).transcribe(AUDIO)
    assert r["text"] == "Come back here." and r["confidence"] == PARAKEET_ONLY_CONFIDENCE < RuntimeSettings().voice.min_motion_command_confidence


def test_pair_state_follows_whichever_engine_is_ready():
    assert CombinedTranscriber(FakeEngine("loading", None), FakeEngine("loading", None)).state == "loading"
    assert CombinedTranscriber(FakeEngine("loading", None), FakeEngine("ready", None)).state == "ready"
    assert CombinedTranscriber(FakeEngine("error", None), FakeEngine("unavailable", None)).state == "error"


def test_missing_nvidia_files_mean_unavailable_not_a_crash(tmp_path):
    assert not nemo_asr.available(tmp_path)
    asr = nemo_asr.NemoSpeechASR(tmp_path)
    assert asr.state == "unavailable" and "download_models.py --nvidia" in asr.error
    assert asr.transcribe(AUDIO) is None
    asr.close()


@pytest.mark.skipif(not nemo_asr.available(MODELS) or shutil.which("say") is None, reason="NVIDIA files not downloaded (download_models.py --nvidia)")
def test_parakeet_transcribes_a_spoken_sentence_with_the_network_blocked(tmp_path):
    wav = tmp_path / "said.wav"
    subprocess.run(["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", "--", "Give me my phone back."], check=True, capture_output=True, timeout=30)
    with wave.open(str(wav)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    asr = nemo_asr.NemoSpeechASR(MODELS)
    try:
        t0 = time.monotonic()
        while asr.state == "loading" and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        assert asr.state == "ready", asr.error
        out = asr.transcribe(audio)
        assert out is not None and intent(out["text"]) == IntentName.RELEASE, out
        assert asr.transcribe(np.zeros(32000, dtype=np.float32))["text"] == ""  # silence stays silence
    finally:
        asr.close()
