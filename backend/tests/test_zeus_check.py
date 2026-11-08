"""scripts/zeus_check.py, the guided no-motion checks: what it treats as a broken motor lock, how it
judges each window of samples, and that it can only read from the backend."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "zeus_check.py"
sys.path.insert(0, str(SCRIPT.parent))

import zeus_check as zc  # noqa: E402

LOCKED = {"armed": False, "permitted": False, "motor_commands": "disabled", "link_mode": "monitor", "motion": "idle", "tx": [], "family": "single_client", "sent": 0}


def fails(checks) -> list[str]:
    return [c.label for c in checks if c.ok is False]


def test_the_default_lock_has_no_violations():
    assert zc.lock_violations(LOCKED) == []


def test_every_way_the_lock_can_break_is_reported():
    assert zc.lock_violations(LOCKED | {"armed": True})
    assert zc.lock_violations(LOCKED | {"permitted": True})
    assert zc.lock_violations(LOCKED | {"motor_commands": "bench"})
    assert zc.lock_violations(LOCKED | {"link_mode": "control"})
    assert zc.lock_violations(LOCKED | {"motion": "moving"})
    assert zc.lock_violations(LOCKED | {"sent": 1})
    assert zc.lock_violations(LOCKED | {"tx": [{"dir": "tx", "frame": '{"F":true}'}]})
    assert zc.lock_violations(LOCKED | {"tx": [{"dir": "tx", "frame": '{"K":[0,40]}'}]})


def test_text_ping_is_allowed_only_on_multi_client_firmware():
    multi = LOCKED | {"family": "multi_client", "sent": 9, "tx": [{"dir": "tx", "frame": "ping"}]}
    assert zc.lock_violations(multi) == []
    assert zc.lock_violations(multi | {"family": "single_client"})


def live(t: float, **kw) -> dict:
    return {"t": t, "camera": "live", "camera_label": zc.CAMERA_LABEL, "link": "connected", "simulated": False, "armed": False, "reconnects": 0, "attempt": 1} | kw


def test_a_camera_or_robot_fallback_fails_the_outage_check():
    window = [live(0), live(2, camera="retrying", link="reconnecting"), live(14, reconnects=1)]
    assert fails(zc.eval_outage(window, t_off=0, t_on=10)) == []
    webcam = [live(0), live(2, camera="live", camera_label="Mac webcam", link="reconnecting"), live(14)]
    assert any("another camera" in x for x in fails(zc.eval_outage(webcam, 0, 10)))
    simulated = [live(0), live(2, camera="retrying", link="reconnecting", simulated=True), live(14)]
    assert any("simulated" in x for x in fails(zc.eval_outage(simulated, 0, 10)))


def test_an_outage_that_never_ends_or_is_never_reported_fails():
    stuck = [live(0), live(2, camera="retrying", link="reconnecting"), live(30, camera="retrying", link="reconnecting")]
    assert any("came back" in x for x in fails(zc.eval_outage(stuck, 0, 10)))
    silent = [live(0), live(9), live(20)]
    assert any("camera loss" in x for x in fails(zc.eval_outage(silent, 0, 10)))
    assert any("link loss" in x for x in fails(zc.eval_outage(silent, 0, 10)))
    slow = [live(0), live(8, camera="retrying", link="reconnecting"), live(20)]
    assert any("camera loss" in x for x in fails(zc.eval_outage(slow, 0, 10)))


def test_idle_link_needs_arduino_replies_and_no_frames():
    ok = [live(0, arduino=0, **LOCKED), live(120, arduino=60, **LOCKED)]
    assert fails(zc.eval_idle_link(ok)) == []
    silent_arduino = [live(0, arduino=5, **LOCKED), live(120, arduino=5, **LOCKED)]
    assert any("Arduino replies" in x for x in fails(zc.eval_idle_link(silent_arduino)))
    frame = [live(0, arduino=0, **LOCKED), live(60, arduino=30, **(LOCKED | {"tx": [{"dir": "tx", "frame": '{"F":true}'}]}))]
    assert any("nothing the Arduino acts on" in x for x in fails(zc.eval_idle_link(frame)))
    assert fails(zc.eval_idle_link([]))


def test_a_spoken_turn_needs_the_real_microphone_and_a_voice():
    good = {"turn": {"source": "voice", "text": "Lock me in for 25 minutes.", "reply": "Okay.", "reply_to_audio_ms": 900, "route": "reflex"},
            "speech_parts": {"whisper": "ready", "parakeet": "ready"}, "session": True}
    said = "Lock me in for 25 minutes."
    assert fails(zc.eval_turn(good, said, None, want_session=True)) == []
    typed = good | {"turn": good["turn"] | {"source": "text"}}
    assert any("microphone" in x for x in fails(zc.eval_turn(typed, said, None)))
    misheard = good | {"turn": good["turn"] | {"text": "Thank you."}}
    assert any("heard" in x for x in fails(zc.eval_turn(misheard, said, None)))
    no_voice = good | {"turn": good["turn"] | {"reply_to_audio_ms": None}}
    assert any("reply was spoken" in x for x in fails(zc.eval_turn(no_voice, said, None)))
    one_engine = good | {"speech_parts": {"whisper": "ready", "parakeet": "error"}}
    assert any("both recognisers" in x for x in fails(zc.eval_turn(one_engine, said, None)))
    assert any("route" in x for x in fails(zc.eval_turn(good, said, "converse")))


def test_the_start_check_requires_the_real_locked_robot():
    s = {"mode": zc.LOCKED_LABEL, "connected": True, "link": "connected", "simulated": False, "offline": True, "identified": True}
    assert fails(zc.eval_start(s)) == []
    assert fails(zc.eval_start(s | {"simulated": True}))
    assert fails(zc.eval_start(s | {"mode": "SIMULATED"}))
    assert fails(zc.eval_start(s | {"connected": False}))


def test_the_script_can_only_read():
    """No POST, no request bodies, no sockets, no serial: only urlopen on a plain URL string."""
    tree = ast.parse(SCRIPT.read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"socket", "serial", "websockets", "requests", "httpx", "subprocess", "http.client"}
    urlopens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "urlopen"]
    assert len(urlopens) == 1
    assert isinstance(urlopens[0].args[0], ast.JoinedStr) and "/api/state" in ast.unparse(urlopens[0].args[0])
    assert all(k.arg != "data" for k in urlopens[0].keywords) and len(urlopens[0].args) == 1
    assert "urllib.request.Request" not in SCRIPT.read_text()
