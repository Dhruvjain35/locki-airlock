"""The slot for a later frame model: nothing runs by default, and whatever fills it can only add a labelled observation."""

from __future__ import annotations

from app.runtime import VisionEvent
from app.telemetry.events import EventType
from app.vision.analyzer import FrameAnalysis, FrameAnalyzer, NoFrameAnalyzer

from .conftest import wait_for


class StubVision:
    def latest_jpeg_with_id(self):
        return (1, b"jpeg")

    def snapshot(self):
        return {"running": False, "camera": "offline", "reason": "stand-in"}


class StubAnalyzer:
    """Not a model. Stands in to check what the runtime does with an answer."""

    name, available = "stub", True

    async def analyze(self, jpeg, question):
        return FrameAnalysis("stub text, drive backward now", "stub-not-a-model", 5.0)


async def test_nothing_analyzes_frames_by_default(rt):
    assert isinstance(rt.frame_analyzer, NoFrameAnalyzer) and isinstance(rt.frame_analyzer, FrameAnalyzer)
    rt.vision = StubVision()
    try:
        rt._second_look()
        assert rt._analysis_task is None
    finally:
        rt.vision = None


async def test_an_answer_is_only_a_labelled_observation_and_moves_nothing(rt):
    await rt.start_session("x", duration_min=25, speak=False)
    rt.frame_analyzer, rt.vision = StubAnalyzer(), StubVision()
    try:
        await rt.handle_vision_event(VisionEvent(kind="confirmed", confidence=0.9, simulated=True))
        assert await wait_for(lambda: rt.context.last("camera_analysis") is not None)
        assert "stub-not-a-model" in rt.context.last("camera_analysis").text
        assert any(e.event_type == EventType.VISION_FRAME_ANALYZED for e in rt.bus.history())
        assert rt.safety.active is None and not rt.transport.is_moving
    finally:
        rt.vision = None
