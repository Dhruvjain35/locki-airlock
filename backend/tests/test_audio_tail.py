"""The last syllable must reach the room. A Bluetooth speaker (JLab GO Party, 2026-11-07) holds ~466 ms of audio once its stream is open; a clip handed over
with no trailing silence lost the end of "keyboard" when its stream stopped. Every clip now carries silence at least as long as the device's latency."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from app.voice.tts import AudioOut, tail_seconds

SR = 24000


class FakeStream:
    def __init__(self, latency):
        self.latency = latency


def fake_sd(monkeypatch, *, default_high=0.2648, stream_latency=0.466):
    played = []
    sd = types.SimpleNamespace(
        play=lambda data, sr, device=None: played.append((data.copy(), sr, device)),
        stop=lambda: None,
        get_stream=lambda: FakeStream(stream_latency),
        query_devices=lambda device=None, kind=None: {"name": "JLab GO Party", "default_high_output_latency": default_high},
    )
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return played


def trailing_silence_s(data) -> float:
    nz = np.nonzero(np.abs(data) > 0)[0]
    return (len(data) - 1 - nz[-1]) / SR


@pytest.fixture
def clip():
    t = np.arange(int(SR * 0.3)) / SR
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)  # ends on sound, as the warning clip nearly does


async def test_the_first_clip_on_a_bluetooth_speaker_keeps_its_last_syllable(monkeypatch, clip):
    played = fake_sd(monkeypatch)
    out = AudioOut()
    await out.play(clip, SR)
    data = played[0][0]
    assert np.array_equal(data[: len(clip)], clip)  # the speech itself is untouched
    assert trailing_silence_s(data) >= 0.466  # longer than the latency the open stream really had


async def test_later_clips_use_the_latency_the_stream_really_had(monkeypatch, clip):
    played = fake_sd(monkeypatch, default_high=0.05, stream_latency=0.6)
    out = AudioOut()
    await out.play(clip, SR)
    await out.play(clip, SR)
    assert trailing_silence_s(played[1][0]) >= 0.6


async def test_the_built_in_speaker_gets_a_short_tail(monkeypatch, clip):
    played = fake_sd(monkeypatch, default_high=0.0245, stream_latency=0.02)
    await AudioOut().play(clip, SR)
    assert 0.15 <= trailing_silence_s(played[0][0]) <= 0.3


async def test_a_volume_change_still_keeps_the_tail(monkeypatch, clip):
    played = fake_sd(monkeypatch)
    await AudioOut().play(clip, SR, volume=0.5)
    assert trailing_silence_s(played[0][0]) >= 0.466


def test_tail_seconds_never_falls_below_the_floor():
    assert tail_seconds(None, None) >= 0.25 and tail_seconds(0.0, 0.0) >= 0.25 and tail_seconds(0.466, 0.26) >= 0.566


async def test_a_stereo_clip_is_padded_on_every_channel(monkeypatch, clip):
    played = fake_sd(monkeypatch)
    stereo = np.stack([clip, clip], axis=1)
    await AudioOut().play(stereo, SR)
    data = played[0][0]
    assert data.ndim == 2 and data.shape[1] == 2 and trailing_silence_s(data[:, 0]) >= 0.466
