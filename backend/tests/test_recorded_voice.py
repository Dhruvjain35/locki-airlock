"""Recorded voice input: a WAV of the owner's speech goes through the same speech-to-text, conversation and security rules as the
microphone, labelled "recorded", with a fresh reply every time. The speech-to-text engine here is a stand-in that returns a given text,
so these tests cover the plumbing and the rules; real recognition of a real recording is verified live."""

from __future__ import annotations

import asyncio
import io
import wave

import numpy as np
import pytest

from app.voice.recorded import MAX_BYTES, RecordedAudioError, decode_wav


def wav(seconds=1.0, rate=44_100, channels=2, width=2, amplitude=0.3) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    tone = (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    frames = np.repeat(tone[:, None], channels, axis=1).reshape(-1)
    if width == 2:
        raw = (frames * 32767).astype("<i2").tobytes()
    elif width == 1:
        raw = (frames * 127 + 128).astype(np.uint8).tobytes()
    elif width == 3:
        v = (frames * (2**23 - 1)).astype(np.int32)
        raw = np.stack([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF], axis=1).astype(np.uint8).tobytes()
    else:
        raw = (frames * (2**31 - 1)).astype("<i4").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(raw)
    return buf.getvalue()


# ------------------------------------------------------------------ decoding
@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_common_wav_formats_become_16k_mono(width):
    audio, info = decode_wav(wav(seconds=1.0, rate=48_000, channels=2, width=width))
    assert audio.dtype == np.float32 and abs(len(audio) - 16_000) <= 1 and info["channels"] == 2 and info["bits"] == 8 * width
    assert 0.2 < float(np.max(np.abs(audio))) < 0.4


@pytest.mark.parametrize("data, why", [
    (b"", "empty"), (b"ID3" + b"\0" * 64, "not a WAV"), (wav(seconds=0.1), "too short"), (wav(seconds=31, rate=8_000, channels=1), "limited to 30"),
    (wav(amplitude=0.0), "silent"),
])
def test_unusable_files_are_refused_with_a_reason(data, why):
    with pytest.raises(RecordedAudioError, match=why):
        decode_wav(data)


def test_oversized_files_are_refused_before_decoding():
    with pytest.raises(RecordedAudioError, match="MB"):
        decode_wav(b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * MAX_BYTES)


# ------------------------------------------------------------------ the runtime
class SaysWhatItIsTold:
    """A ready speech-to-text engine that returns the next text it was given (or None, a failure)."""

    engine, error = "stand-in", None

    def __init__(self, *texts):
        self.state, self.texts, self.calls = "ready", list(texts), 0

    def transcribe(self, audio, timeout_s=10.0):
        self.calls += 1
        text = self.texts.pop(0) if self.texts else None
        return None if text is None else {"text": text, "confidence": 0.93}

    def describe(self):
        return {"engine": self.engine, "state": self.state, "error": self.error}


class Mic:
    running = True

    def __init__(self):
        self.holds, self.transcriber, self.input_held = [], None, False

    def hold_input(self, held):
        self.holds.append(held)
        self.input_held = held


async def test_several_recordings_in_any_order_are_each_transcribed_and_answered_fresh(rt):
    rt._recorded_tr = SaysWhatItIsTold("how much time is left", "is anyone near my desk", "how much time is left")
    first = await rt.handle_recorded(wav(), "time.wav")
    second = await rt.handle_recorded(wav(), "desk.wav")
    third = await rt.handle_recorded(wav(), "time.wav")
    assert [r["transcript"] for r in (first, second, third)] == ["how much time is left", "is anyone near my desk", "how much time is left"]
    assert all(r["source"] == "recorded" and r["input_mode"] == "recorded" and r["stt_ms"] >= 0 for r in (first, second, third))
    assert rt._recorded_tr.calls == 3  # every file is transcribed: nothing is looked up from an earlier run
    turns = [t for t in rt.voice_turns if t["source"] == "recorded"]
    assert len(turns) == 3 and len({t["id"] for t in turns}) == 3  # three separate turns through the live pipeline, replies composed each time


async def test_a_failed_or_empty_transcription_is_reported_and_nothing_is_acted_on(rt):
    rt._recorded_tr = SaysWhatItIsTold(None, "")
    failed = await rt.handle_recorded(wav(), "a.wav")
    empty = await rt.handle_recorded(wav(), "b.wav")
    assert not failed["ok"] and "failed or timed out" in failed["error"]
    assert not empty["ok"] and "no speech recognised" in empty["error"]
    assert not any(t["source"] == "recorded" for t in rt.voice_turns)


async def test_speech_to_text_still_loading_says_so_without_failing_the_backend(rt):
    tr = SaysWhatItIsTold("hello")
    tr.state = "loading"
    rt._recorded_tr = tr
    r = await rt.handle_recorded(wav(), "a.wav")
    assert not r["ok"] and "loading" in r["error"] and tr.calls == 0


async def test_context_carries_from_one_recorded_command_to_the_next(rt):
    rt._recorded_tr = SaysWhatItIsTold("don't talk unless something important happens", "how much time is left")
    await rt.handle_recorded(wav(), "quiet.wav")
    assert rt.context.quiet()  # the first command changed the conversation's state...
    await rt.handle_recorded(wav(), "time.wav")
    assert rt.context.quiet()  # ...and the next command runs in that same state
    rt.context.end_quiet()


async def test_the_live_microphone_is_ignored_while_a_recording_is_handled_and_answered(rt):
    mic = Mic()
    rt.voice = mic
    rt._recorded_tr = SaysWhatItIsTold("how much time is left")
    await rt.handle_recorded(wav(), "time.wav")
    assert mic.holds[0] is True
    for _ in range(40):
        if len(mic.holds) > 1:
            break
        await asyncio.sleep(0.1)
    assert mic.holds[-1] is False  # back to the room only after LOCKI finished answering
    rt.voice = None


def test_stop_speaking_stops_locki_and_keeps_security_warnings(rt):
    stopped = []
    rt.speaker.interrupt = lambda reason="": stopped.append(reason)
    r = rt.stop_speaking()
    assert r["ok"] and stopped == ["owner pressed stop speaking"] and "P0" in r["message"]


async def test_a_recorded_command_never_arms_without_the_owners_grant_and_never_disarms(rt):
    from .test_security_arm_delegation import delegating, grant
    from .test_security_spacetime import remote

    h = await delegating(rt)
    rt._recorded_tr = SaysWhatItIsTold("LOCKI, watch my desk", "alright, we're good, stand down", "LOCKI, watch my desk")
    r = await rt.handle_recorded(wav(), "arm.wav")
    assert not r["ok"] and rt.security.session is None and rt.security.transport.calls == []  # no grant: nothing is called
    rt.security.apply_remote_session(remote(session_id="owner-armed"))
    r = await rt.handle_recorded(wav(), "standdown.wav")
    assert not r["ok"] and rt.security.session is not None  # a recording can never disarm
    rt.security.apply_remote_session(remote(session_id="owner-armed", state="DISARMED"))
    rt.security.arm_grants.apply(grant())
    r = await rt.handle_recorded(wav(), "arm.wav")
    assert r["ok"] and rt.security.transport.calls == [("arm_session_delegated", [rt.security.arm_grants.workstation_id, "auth-1"])]


def test_the_routes_are_for_the_local_dashboard_only(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.runtime import LockedInRuntime

    from .test_voice_trials import RecordingTTS

    runtime = LockedInRuntime(settings, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False)
    runtime._recorded_tr = SaysWhatItIsTold("how much time is left")
    with TestClient(create_app(settings, runtime)) as c:
        assert c.post("/api/voice/recorded?name=x.wav", content=wav(), headers={"origin": "http://evil.example"}).status_code == 403
        assert c.post("/api/voice/stop-speaking", headers={"origin": "http://evil.example"}).status_code == 403
        bad = c.post("/api/voice/recorded?name=x.mp3", content=b"ID3....", headers={"origin": "http://127.0.0.1:5174"})
        assert bad.status_code == 400 and "not a WAV" in bad.json()["detail"]
        ok = c.post("/api/voice/recorded?name=time.wav", content=wav(), headers={"origin": "http://127.0.0.1:5174"}).json()
        assert ok["transcript"] == "how much time is left" and ok["source"] == "recorded"
        status = c.get("/api/voice/recorded").json()
        assert status["stt"]["state"] == "ready" and status["turns"][0]["file"] == "time.wav"
