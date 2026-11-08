"""Turn-taking: when a turn ends, when an unfinished turn waits for the rest, when the owner may talk over LOCKI, and when LOCKI stays
silent. The audio here is synthetic levels and probabilities; the physical behaviour still needs a live trial with a person."""

from __future__ import annotations

import queue
import time
import types

import numpy as np

from app.voice import service as vs
from app.voice.input import BLOCK_S, VADConfig, end_silence_ms
from app.voice.service import VoiceService, sounds_unfinished


# ------------------------------------------------------------------ turn endings
def test_short_turns_close_quickly_and_long_ones_tolerate_a_pause():
    cfg = VADConfig()
    assert end_silence_ms(cfg, int(0.8 / BLOCK_S)) == 650
    assert end_silence_ms(cfg, int(2.5 / BLOCK_S)) == 950
    assert cfg.max_utterance_s >= 20  # a long question is no longer cut at 8 s


def test_unfinished_sounding_words():
    for text in ("Did someone plug something in and", "I wanted to ask, um", "Was it the", "so the person at my desk was,"):
        assert sounds_unfinished(text), text
    for text in ("Did someone plug something in?", "What happened while I was away", "Stand down.", ""):
        assert not sounds_unfinished(text), text


class Captured:
    def __init__(self):
        self.events = []

    def __call__(self, kind, data):
        self.events.append((kind, data))

    def utterances(self):
        return [d for k, d in self.events if k == "utterance"]


def voice(mode="always", speaker=None):
    emit = Captured()
    v = VoiceService(types.SimpleNamespace(vad_engine="energy", models_dir=None), emit, speaker=speaker, transcriber=object())
    v.mode, v.running = mode, True
    return v, emit


def part(text, conf=0.9, during=False):
    return {"text": text, "confidence": conf, "latency_ms": 300, "audio_s": 1.0, "during_playback": during}


def test_an_unfinished_turn_waits_and_the_rest_is_joined_into_one_question():
    v, emit = voice()
    v._last_seg_end = time.monotonic()
    v._deliver(part("Did the person at my desk plug in"))  # ends on "in": not a cue, so it goes straight through
    assert [u["text"] for u in emit.utterances()] == ["Did the person at my desk plug in"]
    v._deliver(part("I wanted to ask whether they touched my keyboard and"))
    assert emit.utterances()[-1]["text"] == "Did the person at my desk plug in" and v._held is not None
    v._deliver(part("whether anything was plugged in", conf=0.7))
    joined = emit.utterances()[-1]
    assert joined["text"] == "I wanted to ask whether they touched my keyboard and whether anything was plugged in"
    assert joined["confidence"] == 0.7 and joined["parts"] == 2 and v.joined_turns == 1 and v._held is None
    assert joined["capture_epoch"] == v.capture_epoch


def test_a_held_turn_is_answered_once_the_owner_stays_quiet():
    v, emit = voice()
    v._last_seg_end = time.monotonic() - vs.CONTINUATION_GRACE_S - 0.1
    v._deliver(part("What did they do with the"))
    assert v._held is not None and not emit.utterances()
    v._flush_held()
    assert emit.utterances()[-1]["text"] == "What did they do with the" and v._held is None


def test_push_to_talk_is_never_held():
    v, emit = voice(mode="push_to_talk")
    v._deliver(part("Is anyone near my desk and"))
    assert emit.utterances()[-1]["text"] == "Is anyone near my desk and"


def test_stopping_discards_a_held_turn_instead_of_flushing_it_on_worker_exit():
    v, emit = voice()
    old_epoch = v.capture_epoch
    v._deliver(part("I wanted to secure the workstation and"))
    assert v._held is not None
    v.stop()
    v._stt_loop(old_epoch)
    v._deliver({**part("secure my workstation"), "capture_epoch": old_epoch})
    assert v._held is None and not emit.utterances() and v.capture_epoch > old_epoch


def test_a_late_transcription_cannot_be_relabelled_after_the_same_capture_restarts(monkeypatch):
    v, emit = voice()
    v.running = False
    workers = []

    def thread(**kwargs):
        worker = types.SimpleNamespace(**kwargs, start=lambda: None, join=lambda **_kwargs: None)
        workers.append(worker)
        return worker

    monkeypatch.setattr(vs, "threading", types.SimpleNamespace(Thread=thread))
    monkeypatch.setattr(vs, "AudioCapture", lambda _device: types.SimpleNamespace(
        queue=queue.Queue(), start=lambda: None, stop=lambda: None,
    ))

    class LateTranscriber:
        state = "ready"

        def transcribe(self, _audio):
            v.stop()
            v.start(mode="always")
            v._deliver(part("A new question about the"))
            return {"text": "secure my workstation", "confidence": 0.9}

    v.transcriber = LateTranscriber()
    v.start(mode="always")
    old_epoch, old_queue = v.capture_epoch, v._stt_q
    assert all(w.args == (old_epoch,) for w in workers)
    v._submit(np.zeros(16, dtype=np.float32))
    v._stt_loop(old_epoch)  # the fake restarts the service while the old worker transcribes
    assert v.capture_epoch > old_epoch and v._stt_q is not old_queue
    assert all(w.args == (v.capture_epoch,) for w in workers[-2:])
    assert not emit.utterances() and v.last_text is None and v.utterances == 0
    assert v._held["payload"]["capture_epoch"] == v.capture_epoch  # the old worker cannot flush the new held turn
    v._submit(np.zeros(16, dtype=np.float32), capture_epoch=old_epoch)
    assert v._stt_q.empty()  # old input callbacks cannot queue audio into the new capture
    v.stop()


def test_parts_from_different_capture_epochs_never_form_a_joined_command():
    v, emit = voice()
    v._held = {"payload": {**part("secure my"), "capture_epoch": v.capture_epoch - 1},
               "parts": 1, "deadline": time.monotonic() + 1}
    v._deliver({**part("workstation"), "capture_epoch": v.capture_epoch})
    assert v._held is None and not emit.utterances() and v.joined_turns == 0


# ------------------------------------------------------------------ barge-in onset
class Talking:
    def __init__(self):
        self.speaking = True

    def recently_spoke(self, within_s=0.8):
        return self.speaking


def test_a_strong_voice_over_locki_is_a_barge_in_but_its_own_echo_never_is(monkeypatch):
    spk = Talking()
    v, emit = voice(speaker=spk)
    clock = [100.0]
    monkeypatch.setattr(vs.time, "monotonic", lambda: clock[0])
    v.vad.last_prob = 0.95  # the echo of LOCKI's voice is speech too: only loudness above it can tell the owner apart

    def blocks(db, n):
        for _ in range(n):
            v._watch_barge_in(db)
            clock[0] += BLOCK_S

    blocks(-30.0, int(vs.ECHO_LEARN_S / BLOCK_S) + 1)  # LOCKI's echo, learned
    blocks(-28.0, 40)  # echo keeps playing, a little louder: no barge-in
    assert not [k for k, _ in emit.events if k == "barge_in"]
    blocks(-15.0, vs.BARGE_IN_BLOCKS)  # the owner speaks well over it for 240 ms
    barge = [d for k, d in emit.events if k == "barge_in"]
    assert len(barge) == 1 and barge[0]["over_echo_db"] >= vs.BARGE_IN_OVER_ECHO_DB and v.barge_ins == 1
    blocks(-15.0, 30)  # one barge-in per line
    assert v.barge_ins == 1
    spk.speaking = False
    v._watch_barge_in(-60.0)
    assert not v._was_playing


def test_conversation_state_follows_audio_and_models():
    spk = Talking()
    v, _ = voice(speaker=spk)
    assert v.conversation_state() == "SPEAKING"
    spk.speaking = False
    assert v.conversation_state() == "LISTENING"
    v.thinking = lambda: True
    assert v.conversation_state() == "THINKING"
    v._held = {"payload": part("and"), "parts": 1, "deadline": time.monotonic() + 1}
    assert v.conversation_state() == "ENDPOINTING"
    v.vad.in_speech = True
    assert v.conversation_state() == "USER_SPEAKING"
    v.last_barge_in_at = time.monotonic()
    assert v.conversation_state() == "INTERRUPTED"
    v.running = False
    assert v.conversation_state() == "IDLE"


# ------------------------------------------------------------------ the runtime: talking over LOCKI, and staying silent
async def test_a_real_question_over_locki_stops_it_and_is_answered(rt):
    stopped = []
    rt.speaker.interrupt = lambda reason="": stopped.append(reason)
    r = await rt.handle_utterance("wait, did they plug anything into my laptop", source="voice", stt_confidence=0.9, during_playback=True)
    assert stopped == ["barge-in"] and "ignored" not in r.message


async def test_a_short_unclear_fragment_over_locki_is_not_an_interruption(rt):
    stopped = []
    rt.speaker.interrupt = lambda reason="": stopped.append(reason)
    r = await rt.handle_utterance("yeah", source="voice", stt_confidence=0.9, during_playback=True)
    assert not r.ok and "LOCKI was talking" in r.message and stopped == []


async def test_a_barge_in_never_cuts_a_p0_warning(rt):
    seen = []
    rt.speaker.speaking = True
    rt.speaker.interrupt = lambda reason="": seen.append(reason)  # Speaker.interrupt itself keeps P0 lines; the runtime always goes through it
    rt._barge_in({"over_echo_db": 12.0})
    assert seen == ["barge-in"]


def test_quiet_mode_silences_unasked_lines_but_never_alerts_or_answers(rt):
    rt.context.set_quiet(None, "test")
    rt._say("security_armed", priority=1)
    assert rt.silenced[-1]["reason"].startswith("quiet mode") and rt.silenced[-1]["category"] == "security_armed"
    n = len(rt.silenced)
    rt._say("security_warning_usb", priority=0)  # a P0 alert follows the alert policy, not quiet mode
    rt._say("security_armed", priority=1, force=True)
    assert len(rt.silenced) == n
    rt.context.end_quiet()
    rt._say("security_armed", priority=1)
    assert len(rt.silenced) == n


def test_locki_does_not_talk_over_the_owner(rt):
    rt.voice = types.SimpleNamespace(running=True, vad=types.SimpleNamespace(in_speech=True), _ptt_active=False)
    rt._say("security_armed", priority=1)
    assert rt.silenced[-1]["reason"] == "the owner is talking"
    rt.voice = None


async def test_dont_talk_unless_something_important_happens_turns_on_quiet_mode(rt):
    r = await rt.handle_utterance("Don't talk unless something important happens.", source="text", stt_confidence=1.0)
    assert r.ok and rt.context.quiet() and "quiet" in r.message
    rt.context.end_quiet()


def test_nothing_the_microphone_heard_is_in_the_silence_record(rt):
    rt.context.set_quiet(None, "test")
    rt._say("security_armed", priority=1)
    assert set(rt.silenced[-1]) == {"at", "category", "priority", "reason"}
    rt.context.end_quiet()
