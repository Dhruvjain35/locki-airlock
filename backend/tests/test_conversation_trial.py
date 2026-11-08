"""The live-trial recorder turns real backend events into P50/P95 numbers, and keeps no transcript text."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("conversation_trial", Path(__file__).resolve().parents[1] / "scripts" / "conversation_trial.py")
trial_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial_mod)


def test_events_become_measures_without_any_words():
    t = trial_mod.Trial()
    t.event({"event_type": "voice.transcribed", "payload": {"source": "voice", "latency_ms": 320, "text": "did they plug anything in"}})
    t.event({"event_type": "voice.tts_finished", "payload": {"trace": {"kind": "turn"}, "to_audio_ms": 900, "output_latency_ms": 466, "outcome": "spoken", "text": "No USB device."}})
    t.event({"event_type": "voice.barge_in", "payload": {"over_echo_db": 12.0, "speech_prob": 0.97}})
    t.event({"event_type": "voice.tts_finished", "payload": {"trace": {"kind": "turn"}, "outcome": "interrupted"}})
    t.event({"event_type": "voice.tts_finished", "payload": {"trace": {"kind": "security"}, "to_audio_ms": 339, "outcome": "spoken"}})
    t.event({"event_type": "voice.silenced", "payload": {"category": "security_armed", "reason": "quiet mode"}})
    t.event({"event_type": "voice.command_rejected", "payload": {"reason": "echo of the robot's own speech"}})
    t.event({"event_type": "voice.transcribed", "payload": {"source": "recorded", "latency_ms": 280}})
    r = t.report("unit")
    assert r["counts"]["recorded_turns"] == 1 and r["counts"]["turns"] == 1  # the recorded one never counts as a live turn
    assert r["stt_ms"]["p50"] == 320 and r["reply_to_audio_ms"]["p50"] == 900 and r["audible_reply_ms"]["p50"] == 1366
    assert r["security_to_audio_ms"]["n"] == 1 and r["barge_in_to_stop_ms"]["n"] == 1
    assert r["counts"]["barge_ins"] == 1 and r["counts"]["lines_interrupted"] == 1 and r["counts"]["echo_rejected"] == 1
    assert r["silenced"] == {"quiet mode": 1}
    import json

    dumped = json.dumps(r)
    assert "plug anything" not in dumped and "No USB device" not in dumped
