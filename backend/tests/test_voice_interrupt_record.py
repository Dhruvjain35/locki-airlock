"""A line cut off while it is playing is recorded as having played, with its real voice and audio start. From a real run: an interrupted reply
showed "text only", which made a playing reply look silent and an interruption look unproven. Fakes only: nothing renders or plays."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import numpy as np

from app.telemetry.events import EventBus
from app.voice.tts import P0_SAFETY, P2_REPLY, NullTTS, Speaker, VoiceRouter


class CachedKokoro:
    def available(self) -> bool:
        return True

    def clip_for(self, text: str):
        return np.zeros(24000, dtype=np.float32), 24000

    def render(self, text: str, cacheable: bool = False):
        return self.clip_for(text)


class SlowAudio:
    """Starts playing at once; a reply takes 5 s, the warning a moment."""

    last_start_mono = None
    last_latency_ms = 465.8

    async def play(self, pcm, sample_rate: int, device=None, volume: float = 1.0) -> None:
        self.last_start_mono = time.monotonic()
        await asyncio.sleep(5.0 if len(pcm) and volume == 1.0 and SlowAudio.long else 0.01)


SlowAudio.long = True


async def test_a_reply_cut_off_while_playing_keeps_its_voice_and_audio_start():
    audio = SlowAudio()
    profile = SimpleNamespace(provider="system", local_voice="kokoro", volume=1.0)
    router = VoiceRouter(profile=lambda: profile, system=NullTTS(), audio=audio, neural=CachedKokoro())
    speaker = Speaker(router, bus=EventBus())
    speaker.start()
    try:
        speaker.say("A long reply that is playing right now.", priority=P2_REPLY, category="generated")
        for _ in range(200):
            if audio.last_start_mono is not None:
                break
            await asyncio.sleep(0.01)
        assert audio.last_start_mono is not None, "the reply started playing"
        SlowAudio.long = False
        speaker.say("Security alert.", priority=P0_SAFETY, category="security_warning_usb", force=True)
        for _ in range(300):
            if any(h["text"] == "Security alert." for h in speaker.history):
                break
            await asyncio.sleep(0.01)
    finally:
        SlowAudio.long = True
        await speaker.close()
    reply = next(h for h in speaker.history if h["text"].startswith("A long reply"))
    alert = next(h for h in speaker.history if h["text"] == "Security alert.")
    assert reply["outcome"] == "interrupted" and reply["source"] == "kokoro_cache" and reply.get("detail") == "cut off while playing"
    assert alert["outcome"] == "finished" and alert["source"] == "kokoro_cache"


async def test_a_reply_cut_off_before_it_made_a_sound_is_still_text_only():
    class NeverStarts:
        last_start_mono = None
        last_latency_ms = None

        async def play(self, pcm, sample_rate: int, device=None, volume: float = 1.0) -> None:
            await asyncio.sleep(0.01)

    class SlowRender(CachedKokoro):
        def clip_for(self, text):
            return None

        def render(self, text, cacheable=False):
            time.sleep(0.3 if "long" in text else 0.0)
            return np.zeros(2400, dtype=np.float32), 24000

    profile = SimpleNamespace(provider="system", local_voice="kokoro", volume=1.0)
    router = VoiceRouter(profile=lambda: profile, system=NullTTS(), audio=NeverStarts(), neural=SlowRender())
    speaker = Speaker(router, bus=EventBus())
    speaker.start()
    try:
        speaker.say("A long reply still rendering.", priority=P2_REPLY, category="generated")
        await asyncio.sleep(0.05)
        speaker.say("Security alert.", priority=P0_SAFETY, category="security_warning_usb", force=True)
        for _ in range(300):
            if any(h["text"] == "Security alert." for h in speaker.history):
                break
            await asyncio.sleep(0.01)
    finally:
        await speaker.close()
    reply = next(h for h in speaker.history if h["text"].startswith("A long reply"))
    assert reply["outcome"] == "interrupted" and reply["source"] == "text_only", "nothing had played, and the record says so"
