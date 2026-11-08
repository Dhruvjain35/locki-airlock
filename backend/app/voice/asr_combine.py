"""Two recognisers, one transcript. Parakeet TDT 0.6B v3 (NVIDIA, through NeMo-Speech.cpp) and
whisper.cpp hear the same audio at the same time; this decides what the robot heard
(docs/NVIDIA_EVALUATION.md).

Measured on the same 413 clips (docs/benchmarks/m55-asr-*.json): Parakeet makes far fewer word
errors on real human and informal speech and never turned non-speech into words; whisper.cpp
holds up better in steady noise and background talk, and is the only one of the two with a
calibrated confidence (NeMo-Speech.cpp reports 1.0 for every RNNT/TDT result). The rule:

1. Parakeet heard nothing: nothing was said (whisper's text on noise is a phantom), unless
   whisper heard a stop. A stop is always acted on: stopping is the safe direction.
2. Both readings mean the same thing (the same command, or both conversation): Parakeet's
   words with whisper's confidence, so the command gates keep their meaning.
3. They disagree: a stop wins; otherwise a reading that is a command wins over one that is not;
   two different commands go to whisper when it is confident (>= 0.8), else to Parakeet.
4. A command that moves the robot is never taken from a disagreement. It needs both readings
   (rule 2) and then still passes the runtime's own 0.8 motion gate on whisper's confidence.

A reading taken from Parakeet alone gets confidence 0.5: enough for ordinary commands (the gate
is 0.45), never enough to move the robot (0.8). Two readings that agree are never trusted less than
one (Milestone 5.6 audit: an emergency stop both heard used to carry whisper's 0.3 and fail the
0.45 gate that Parakeet alone would pass). Whisper's stock outputs for noise ("Thank you.",
"[SOUND]") count as hearing nothing, so they cannot outvote a real command.
"""

from __future__ import annotations

import re

from .intents import MOVES_ROBOT, IntentClassifier, IntentName

PARAKEET_ONLY_CONFIDENCE = 0.5
STOPS = frozenset({IntentName.EMERGENCY_STOP, IntentName.STOP_MOTION})
_classifier = IntentClassifier()


def _said(text: str | None) -> bool:
    return bool(text) and any(c.isalnum() for c in text)


def _intent(text: str | None) -> IntentName | None:
    return _classifier.classify(text).name if _said(text) else None


def is_stop(text: str | None) -> bool:
    return _intent(text) in STOPS


def _command(name: IntentName | None) -> bool:
    return name is not None and name != IntentName.UNKNOWN and name not in MOVES_ROBOT


def _whisper_phantom(text: str) -> bool:
    """A sound label or one of whisper's well-known outputs for silence and noise."""
    from .transcription import is_hallucination

    return is_hallucination(text) or bool(re.fullmatch(r"\s*[\[(][^\])]*[\])]\s*", text))


def combine(whisper_text: str | None, whisper_confidence: float | None, parakeet_text: str | None) -> tuple[str, float, str]:
    """(transcript, confidence, which rule decided)."""
    w, p = (whisper_text or "").strip(), (parakeet_text or "").strip()
    wc = float(whisper_confidence or 0.0)
    if w and _whisper_phantom(w) and not is_stop(w):
        w, wc = "", 0.0
    iw, ip = _intent(w), _intent(p)
    if not _said(p):
        return (w, wc, "stop heard by whisper only") if iw in STOPS else ("", 0.0, "no speech (Parakeet heard nothing)")
    if iw == ip:
        return p, (max(wc, PARAKEET_ONLY_CONFIDENCE) if _said(w) else PARAKEET_ONLY_CONFIDENCE), "agree"
    if iw in STOPS:
        return w, wc, "stop (whisper)"
    if ip in STOPS:
        return p, PARAKEET_ONLY_CONFIDENCE, "stop (Parakeet)"
    if _command(ip) and not _command(iw):
        return p, PARAKEET_ONLY_CONFIDENCE, "command heard by Parakeet"
    if _command(iw) and not _command(ip):
        return w, wc, "command heard by whisper"
    if _command(iw) and _command(ip):
        return (w, wc, "different commands: whisper confident") if wc >= 0.8 else (p, PARAKEET_ONLY_CONFIDENCE, "different commands: Parakeet")
    if ip in MOVES_ROBOT and iw not in MOVES_ROBOT:
        return w, wc, "motion heard by one recogniser only: not taken"
    return p, PARAKEET_ONLY_CONFIDENCE, "conversation (Parakeet's words)"
