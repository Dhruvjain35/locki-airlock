"""Recorded voice input: a WAV of the owner's speech goes through the same speech-to-text, conversation and security rules as the
microphone, labelled "recorded", with a fresh reply every time. The speech-to-text engine here is a stand-in that returns a given text,
so these tests cover the plumbing and the rules; real recognition of a real recording is verified live."""

from __future__ import annotations

import asyncio
import io
import threading
import wave
from types import SimpleNamespace

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


@pytest.mark.parametrize("data", [b"RIFF\xff\xff\xff\xffWAVEfmt ", wav()[:-1], wav(width=3)[:-2], wav()[:50]])
def test_truncated_headers_and_pcm_are_rejected_as_audio_errors(data):
    with pytest.raises(RecordedAudioError):
        decode_wav(data)


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


async def test_a_recorded_command_never_arms_even_with_the_owners_grant_and_never_disarms(rt):
    from .test_security_arm_delegation import delegating, grant
    from .test_security_spacetime import remote

    await delegating(rt)
    rt._recorded_tr = SaysWhatItIsTold("LOCKI, watch my desk", "alright, we're good, stand down", "LOCKI, watch my desk")
    r = await rt.handle_recorded(wav(), "arm.wav")
    assert not r["ok"] and rt.security.session is None and rt.security.transport.calls == []  # no grant: nothing is called
    rt.security.apply_remote_session(remote(session_id="owner-armed"))
    r = await rt.handle_recorded(wav(), "standdown.wav")
    assert not r["ok"] and rt.security.session is not None  # a recording can never disarm
    rt.security.apply_remote_session(remote(session_id="owner-armed", state="DISARMED"))
    rt.security.arm_grants.apply(grant())
    r = await rt.handle_recorded(wav(), "arm.wav")
    assert not r["ok"] and rt.security.transport.calls == []
    assert "Recorded speech cannot arm or disarm" in r["message"]


@pytest.mark.parametrize("text", ["LOCKI, watch my desk", "Lockie arm the desk", "arm workstation",
                                "disarm workstation", "alright, we're good, stand down",
                                "don't arm the workstation", 'say "arm workstation"',
                                "arm workstation " + " " * 350 + "do not execute this"])
async def test_recorded_alias_negation_quote_and_original_text_never_reach_security_actions(rt, monkeypatch, text):
    from .test_security_arm_delegation import delegating, grant

    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    calls = []
    monkeypatch.setattr(rt.security, 'request_arm_by_voice', lambda: calls.append('ARM'))
    monkeypatch.setattr(rt.security, 'request_disarm_by_voice', lambda: calls.append('DISARM'))
    rt._recorded_tr = SaysWhatItIsTold(text)
    await rt.handle_recorded(wav(), "../../untrusted.wav")
    assert calls == [] and rt.security.transport.calls == []
    assert rt.recorded_turns[-1]['source'] == 'recorded'


def test_recorded_status_does_not_load_a_transcriber(rt, monkeypatch):
    monkeypatch.setattr(rt, 'recorded_transcriber', lambda: pytest.fail('status must not initialize models'))
    rt._recorded_tr = None
    status = rt.recorded_status()
    assert status['stt']['state'] == 'unavailable'


async def test_repeated_cancellation_keeps_transcriber_busy_until_thread_finishes(rt):
    entered, release = threading.Event(), threading.Event()

    class BlockingTranscriber(SaysWhatItIsTold):
        def transcribe(self, audio):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            return {'text': 'watch my desk', 'confidence': 1.0}

    tr = BlockingTranscriber()
    rt._recorded_tr = tr
    task = asyncio.create_task(rt.handle_recorded(wav(), 'cancelled.wav'))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert rt._recorded_busy and not task.done()
        refused = await rt.handle_recorded(wav(), 'overlap.wav')
        assert not refused['ok'] and 'another recorded' in refused['error'] and tr.calls == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not rt._recorded_busy and not any(t['source'] == 'recorded' for t in rt.voice_turns)


async def test_old_recorded_release_cannot_unhold_a_replacement_voice(rt):
    old, new = Mic(), Mic()
    rt.voice = new
    await rt._release_mic_after_reply(old)
    assert new.holds == [] and old.holds == []
    rt.voice = None


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
        assert c.get("/api/voice/recorded", headers={"origin": "http://evil.example"}).status_code == 403
        bad = c.post("/api/voice/recorded?name=x.mp3", content=b"ID3....", headers={"origin": "http://127.0.0.1:5174"})
        assert bad.status_code == 400 and "not a WAV" in bad.json()["detail"]
        ok = c.post("/api/voice/recorded?name=time.wav", content=wav(), headers={"origin": "http://127.0.0.1:5174"}).json()
        assert ok["transcript"] == "how much time is left" and ok["source"] == "recorded"
        status = c.get("/api/voice/recorded").json()
        assert status["stt"]["state"] == "ready" and status["turns"][0]["file"] == "time.wav"


async def test_recorded_upload_deadline_concurrency_and_size_fail_before_transcription(rt, monkeypatch):
    from fastapi import HTTPException

    from app.api import routes

    state = SimpleNamespace(runtime=rt)
    received = asyncio.Event()

    async def slow_stream():
        received.set()
        await asyncio.Event().wait()
        yield b'never'

    request = SimpleNamespace(headers={}, app=SimpleNamespace(state=state), stream=slow_stream)
    monkeypatch.setattr(routes, 'RECORDED_UPLOAD_TIMEOUT_S', 0.05)
    first = asyncio.create_task(routes.recorded_command(request))
    await received.wait()
    with pytest.raises(HTTPException) as busy:
        await routes.recorded_command(request)
    assert busy.value.status_code == 429
    with pytest.raises(HTTPException) as timeout:
        await first
    assert timeout.value.status_code == 408 and state.recorded_upload_active is False

    async def oversized_stream():
        yield b'x' * MAX_BYTES
        yield b'x'

    request.stream = oversized_stream
    with pytest.raises(HTTPException) as oversized:
        await routes.recorded_command(request)
    assert oversized.value.status_code == 413 and state.recorded_upload_active is False
    assert rt._recorded_tr is None and not rt.recorded_turns


async def test_a_recorded_arm_or_disarm_is_answered_aloud_and_touches_nothing(rt):
    from .test_security_arm_delegation import delegating, grant
    from .test_security_spacetime import remote

    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    rt._recorded_tr = SaysWhatItIsTold("LOCKI, secure my workstation.", "alright, we're good, stand down")
    r = await rt.handle_recorded(wav(), "secure.wav")
    assert not r["ok"] and r["reply"] == "I can't arm from a recording. The owner approves arming in the LOCKI owner console."
    assert rt.security.transport.calls == [] and rt.security.arm_grants.usable() is not None  # the grant is still there, unused
    rt.security.apply_remote_session(remote(session_id="owner-armed"))
    r = await rt.handle_recorded(wav(), "standdown.wav")
    assert not r["ok"] and "can't disarm from a recording" in r["reply"] and rt.security.session is not None
    assert "live microphone" not in r["message"]
