"""Recorded voice input: a WAV file of the owner's own speech, recorded somewhere quiet, used instead of the room's microphone.

The audio goes through the same speech-to-text, conversation and voice as the live microphone; only the source is different, and it
is labelled "recorded" everywhere (transcript, history, trial records) so a recorded turn can never read as a live-microphone result.
Nothing here holds a transcript or a reply: every turn is transcribed and answered fresh.
"""

from __future__ import annotations

import io
import wave

import numpy as np

TARGET_RATE = 16_000
MAX_BYTES = 4 * 1024 * 1024  # a 20 s command at 48 kHz stereo 16-bit; longer files are refused before decoding
MAX_SECONDS = 30.0
MIN_SECONDS = 0.3
SILENT_PEAK = 1e-3  # below this the file holds no audible speech


class RecordedAudioError(ValueError):
    """The file cannot be used as a spoken command; the message says why, for the dashboard."""


def decode_wav(data: bytes) -> tuple[np.ndarray, dict]:
    """PCM WAV bytes -> 16 kHz mono float32 in [-1, 1], plus what the file was. Refuses anything else with a reason."""
    if not data:
        raise RecordedAudioError("the file is empty")
    if len(data) > MAX_BYTES:
        raise RecordedAudioError(f"the file is {len(data) / 1e6:.1f} MB; recorded commands are limited to {MAX_BYTES / 1e6:.0f} MB")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise RecordedAudioError("not a WAV file (record or export the command as WAV)")
    try:
        with wave.open(io.BytesIO(data)) as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if not 1 <= channels <= 8 or width not in (1, 2, 3, 4) or not 4_000 <= rate <= 192_000:
                raise RecordedAudioError(f"unsupported WAV format ({channels} channels, {8 * width}-bit, {rate} Hz)")
            seconds = frames / rate
            if seconds > MAX_SECONDS:
                raise RecordedAudioError(f"the recording is {seconds:.1f} s; commands are limited to {MAX_SECONDS:.0f} s")
            if seconds < MIN_SECONDS:
                raise RecordedAudioError("the recording is too short to hold a command")
            raw = w.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise RecordedAudioError(f"the WAV file could not be read ({exc}); only uncompressed PCM WAV is supported") from exc
    if len(raw) != frames * channels * width or len(raw) % (channels * width):
        raise RecordedAudioError("the WAV audio is truncated or has incomplete PCM frames")
    if width == 1:
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        pcm = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float32) / float(1 << 23)
    else:
        pcm = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    pcm = pcm[: len(pcm) - len(pcm) % channels].reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        n = int(round(len(pcm) * TARGET_RATE / rate))
        pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype(np.float32)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak < SILENT_PEAK:
        raise RecordedAudioError("the recording is silent")
    info = {"seconds": round(seconds, 2), "rate": rate, "channels": channels, "bits": 8 * width, "peak_dbfs": round(20 * np.log10(peak), 1)}
    return pcm.astype(np.float32), info
