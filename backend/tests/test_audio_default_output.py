"""Playback follows the Mac's current default output: a Bluetooth speaker connected after the backend started is used, PortAudio is
re-initialised only when the default actually changed, only between clips, and the microphone's stream is reopened around it."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from app.voice.input import AudioCapture
from app.voice.tts import AudioOut, PortAudioHost, default_output_device_id


class FakeSD:
    def __init__(self):
        self.terminated = self.initialised = self.stopped = 0
        self.played: list = []
        self.streams_open_at_reinit: list = []

    def _terminate(self):
        self.terminated += 1

    def _initialize(self):
        self.initialised += 1

    def stop(self):
        self.stopped += 1

    def play(self, data, sr, device=None):
        self.played.append((len(data), sr, device))

    def query_devices(self, device=None, kind=None):
        return {"default_high_output_latency": 0.02}

    def get_stream(self):
        class S:
            latency = 0.25
        return S()


class FakeCapture:
    def __init__(self):
        self._stream = object()
        self.suspended = self.resumed = 0

    def suspend(self):
        self.suspended += 1
        self._stream = None

    def resume(self):
        self.resumed += 1
        self._stream = object()


class FakeInputStream:
    def __init__(self, *, fail_stop=False, fail_start=False):
        self.fail_stop, self.fail_start = fail_stop, fail_start
        self.starts = self.stops = self.closes = 0

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("input cannot start")

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("input device disappeared")

    def close(self):
        self.closes += 1


@pytest.mark.parametrize("operation", ["suspend", "stop"])
def test_a_capture_attempts_close_even_when_stopping_raises(operation):
    capture = AudioCapture()
    stream = FakeInputStream(fail_stop=True)
    capture._stream = stream
    with pytest.raises(RuntimeError, match="input device disappeared"):
        getattr(capture, operation)()
    assert stream.stops == stream.closes == 1
    assert capture._stream is None and capture.state == "error"


def test_a_capture_closes_a_stream_that_fails_to_start(monkeypatch):
    stream = FakeInputStream(fail_start=True)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=lambda **_kwargs: stream))
    capture = AudioCapture()
    with pytest.raises(RuntimeError, match="input cannot start"):
        capture._open()
    assert stream.closes == 1 and capture._stream is None
    assert capture.state == "error" and "input cannot start" in capture.error


@pytest.mark.parametrize("reopen_fails", [False, True])
async def test_a_mic_close_or_reopen_failure_preserves_warning_playback_and_other_captures(monkeypatch, reopen_fails):
    sd, host = FakeSD(), PortAudioHost(lookup=lambda: 41)
    failed, healthy = AudioCapture(), FakeCapture()
    old, replacement = FakeInputStream(fail_stop=True), FakeInputStream()
    failed._stream = old

    def open_input(**_kwargs):
        if reopen_fails:
            raise RuntimeError("input still unavailable")
        return replacement

    sd.InputStream = open_input
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    host.register_capture(failed)
    host.register_capture(healthy)
    await AudioOut(host=host).play(np.zeros(24, dtype=np.int16), 24000)
    assert old.closes == 1 and sd.initialised == sd.terminated == 1
    assert len(sd.played) == 1 and sd.played[0][0] > 24  # the warning still has its latency tail
    assert healthy.suspended == healthy.resumed == 1 and healthy._stream is not None
    if reopen_fails:
        assert failed._stream is None and failed.state == "error"
        assert "input still unavailable" in failed.error
    else:
        assert failed._stream is replacement and failed.state == "running" and failed.error is None


def test_voice_snapshot_reports_a_failed_capture_reopen(settings, monkeypatch):
    from app.voice.service import VoiceService

    def unavailable(**_kwargs):
        raise RuntimeError("microphone unavailable")

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=unavailable))
    capture = AudioCapture()
    capture._suspended = True
    with pytest.raises(RuntimeError, match="microphone unavailable"):
        capture.resume()
    service = VoiceService(settings.model_copy(update={"vad_engine": "energy"}), emit=lambda *_args: None)
    service.running, service.capture = True, capture  # active service, with no actual threads or microphone
    snapshot = service.snapshot()
    assert snapshot["running"] and snapshot["stalled"] and snapshot["capture_state"] == "error"
    assert snapshot["conversation_state"] == "IDLE"
    assert "microphone unavailable" in snapshot["warning"] and snapshot["reason"] == snapshot["warning"]


def host_with(devices):
    seq = iter(devices)
    current = {"id": None}

    def lookup():
        current["id"] = next(seq, current["id"])
        return current["id"]

    return PortAudioHost(lookup=lookup)


def test_the_first_clip_reinitialises_once_and_an_unchanged_default_never_again():
    sd, host = FakeSD(), host_with([41, 41, 41, 41])
    assert host.follow_default(sd) and sd.terminated == sd.initialised == 1 and host.default_id == 41
    assert not host.follow_default(sd) and not host.follow_default(sd)
    assert sd.terminated == 1 and host.reinits == 1


def test_a_speaker_connected_later_becomes_the_output_and_the_mic_is_reopened():
    sd, host, mic = FakeSD(), host_with([41, 41, 83]), FakeCapture()
    host.follow_default(sd)
    host.register_capture(mic)
    assert not host.follow_default(sd)
    assert host.follow_default(sd) and host.default_id == 83 and sd.terminated == 2
    assert mic.suspended == 1 and mic.resumed == 1 and mic._stream is not None


def test_no_coreaudio_answer_changes_nothing():
    sd, host = FakeSD(), PortAudioHost(lookup=lambda: None)
    assert not host.follow_default(sd) and sd.terminated == 0


def test_a_mic_that_cannot_reopen_never_stops_the_warning():
    class Gone(FakeCapture):
        def resume(self):
            raise RuntimeError("input device vanished")

    sd, host = FakeSD(), host_with([41, 83])
    host.register_capture(Gone())
    assert host.follow_default(sd) and sd.initialised == 1  # playback goes on; the failure is logged


async def test_playback_follows_the_default_and_keeps_each_devices_own_latency():
    sd, host = FakeSD(), host_with([41, 41, 41, 83, 83])
    out = AudioOut(host=host)  # __init__ warms the lookup (first value)
    import sys

    sys.modules["sounddevice"], real = sd, sys.modules.get("sounddevice")
    try:
        pcm = np.zeros(2400, dtype=np.int16)
        await out.play(pcm, 24000)
        await out.play(pcm, 24000)
        assert sd.terminated == 1 and set(out._measured) == {"default:41"}
        await out.play(pcm, 24000)  # the JBL connected between clips
        assert sd.terminated == 2 and "default:83" in out._measured and all(d is None for _, _, d in sd.played)
        await out.play(pcm, 24000, device="JBL Go 3")  # a named device never consults the default
        assert sd.terminated == 2 and sd.played[-1][2] == "JBL Go 3"
    finally:
        if real is not None:
            sys.modules["sounddevice"] = real
        else:
            del sys.modules["sounddevice"]


def test_the_real_lookup_answers_on_this_platform_or_says_none():
    value = default_output_device_id()
    assert value is None or (isinstance(value, int) and value > 0)


def test_the_device_list_refreshes_only_while_nothing_is_sounding():
    import time as _t

    sd, host = FakeSD(), host_with([41, 83])
    host.playing_until = _t.monotonic() + 5
    assert not host.refresh_if_idle(sd) and sd.terminated == 0  # a warning is playing: never cut it off
    host.playing_until = 0.0
    assert host.refresh_if_idle(sd) and sd.terminated == 1
