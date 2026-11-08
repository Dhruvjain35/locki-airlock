"""The model pauses while the voice renders a sentence (settings.llm_yields_to_voice): the voice
router reports each live Kokoro render, and the runtime passes it to the model only when the
setting is on. Fakes only: nothing renders, nothing plays."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.config import RuntimeSettings, Settings
from app.runtime import LockedInRuntime
from app.voice.tts import NullTTS, SpeechRequest, VoiceRouter


class FakeKokoro:
    def __init__(self, fail: bool = False):
        self.fail, self.renders = fail, 0

    def available(self) -> bool:
        return True

    def clip_for(self, text: str):
        return None

    def render(self, text: str, cacheable: bool = False):
        self.renders += 1
        if self.fail:
            raise RuntimeError("render failed")
        return np.zeros(2400, dtype=np.float32), 24000


class SilentAudio:
    last_start_mono = None
    last_latency_ms = None

    async def play(self, pcm, sample_rate: int, device=None, volume: float = 1.0) -> None:
        await asyncio.sleep(0)


def router_with(kokoro) -> tuple[VoiceRouter, list]:
    events: list = []
    profile = SimpleNamespace(provider="system", local_voice="kokoro", volume=1.0)
    r = VoiceRouter(profile=lambda: profile, system=NullTTS(), audio=SilentAudio(), neural=kokoro)
    r.on_render = events.append
    return r, events


def test_a_live_render_is_announced_before_and_after():
    r, events = router_with(FakeKokoro())
    res = asyncio.run(r._system(SpeechRequest("Thirty minutes. Go."), r.profile()))
    assert res.source == "kokoro_live" and events == [True, False]


def test_the_end_is_announced_even_when_the_render_fails():
    r, events = router_with(FakeKokoro(fail=True))
    asyncio.run(r._system(SpeechRequest("Thirty minutes. Go."), r.profile()))
    assert events == [True, False]


class FakeLLM:
    def __init__(self):
        self.calls: list[bool] = []

    def voice_rendering(self, busy: bool) -> None:
        self.calls.append(busy)


async def _runtime(yields: bool) -> LockedInRuntime:
    s = Settings(_env_file=None, data_dir=Path(tempfile.mkdtemp()), robot_driver="mock", mock_autoconnect=True, llm_provider="none",
                 tts_engine="none", offline=True, llm_yields_to_voice=yields)
    rt = LockedInRuntime(s, runtime_settings=RuntimeSettings(), persist_settings=False, enable_vision=False, enable_voice=False, enable_llm=False)
    await rt.start()
    return rt


async def test_the_runtime_passes_renders_to_the_model_only_when_enabled():
    for yields, expected in ((False, []), (True, [True, False])):
        rt = await _runtime(yields)
        try:
            llm = FakeLLM()
            rt.companion.llm = llm
            rt._voice_rendering(True)
            rt._voice_rendering(False)
            assert llm.calls == expected
        finally:
            rt.companion.llm = None
            await rt.shutdown()


async def test_ai_diagnostics_reports_every_part_of_the_pipeline():
    rt = await _runtime(False)
    try:
        ai = rt.snapshot()["ai"]
        assert set(ai) == {"speech", "model", "voice", "latency_ms", "camera", "memory", "offline", "robot"}
        assert ai["robot"]["driver"] == "mock" and ai["offline"]["enabled"] is True and ai["model"] is None
        assert set(ai["latency_ms"]) >= {"speech_to_text_p50", "model_reply_p50", "voice_render_p50", "end_of_speech_to_voice_p50"}
        for _ in range(100):  # the memory sample is taken in the background
            if rt.ai_diagnostics()["memory"]["processes"]:
                break
            await asyncio.sleep(0.05)
        mem = rt.ai_diagnostics()["memory"]
        assert mem["processes"] and all(p["mb"] is None or p["mb"] > 0 for p in mem["processes"].values())
    finally:
        await rt.shutdown()
