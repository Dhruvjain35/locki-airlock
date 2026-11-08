"""An optional second look at a camera frame, for later (NVIDIA Cosmos Reason2 is the intended first user).

MediaPipe keeps finding events quickly. When one happens, an analyzer may be asked what it means. What comes back
is only ever a labelled observation in the companion's context, so the mission agent can choose its words: it is
never a motor command, never evidence for a checkpoint, and it does not touch the state machine. No analyzer ships
today: ``NoFrameAnalyzer`` is the only implementation, and nothing simulates one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FrameAnalysis:
    text: str
    model: str  # the real model that produced it, shown wherever the text is
    ms: float


@runtime_checkable
class FrameAnalyzer(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    async def analyze(self, jpeg: bytes, question: str) -> FrameAnalysis | None: ...


class NoFrameAnalyzer:
    name = "none"
    available = False

    async def analyze(self, jpeg: bytes, question: str) -> FrameAnalysis | None:
        return None
