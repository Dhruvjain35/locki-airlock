"""Guided no-motion checks on the real Zeus Car: Z1-Z11 in docs/legacy/REAL_HARDWARE_TESTS.md, plus the
voice stack (microphone, speech recognition, local model, audible voice) working together.

    terminal 1, on the Zeus_Car Wi-Fi:  make hardware SPEAKER="JLab GO Party"
    terminal 2:                         cd backend && .venv/bin/python scripts/zeus_check.py

It reads http://127.0.0.1:8077/api/state once a second and asks you what only a person can see.
GET is the only request it can make, so it cannot arm anything or send a frame, and it never
contacts the robot itself. It needs no internet, so it works with the Mac on the robot's Wi-Fi.
Every sample is also checked against the motor lock, with an immediate warning if it ever breaks.

Results: data/hardware_runs/<time>/ (samples.jsonl, results.json, report.md; git-ignored).
Numbers are what the backend reported; only the answers you type say what a person saw.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCKED_LABEL = "ZEUS · MOTION LOCKED"
CAMERA_LABEL = "ZEUS ESP32-CAM"


def compact(s: dict) -> dict:
    """The fields of /api/state these checks use, flattened."""
    robot, safety, vision, voice, ai = s["robot"], s["safety"], s["vision"], s["voice"], s.get("ai") or {}
    st, gate, link = robot.get("status") or {}, robot.get("gate") or {}, robot.get("link") or {}
    fw, vl, vs = st.get("firmware") or {}, vision.get("link") or {}, vision.get("stats") or {}
    det, tts, turns = (vision.get("detector") or {}).get("stats") or {}, voice.get("tts") or {}, voice.get("turns") or []
    turn = turns[0] if turns else None
    return {
        "mode": s["mode"]["label"], "simulated": s["mode"]["robot_simulated"], "offline": s["mode"]["offline"], "state": s["state"]["name"],
        "session": bool(s.get("session")), "target": st.get("target"), "connected": st.get("connected"), "link": st.get("link_state"), "error": st.get("last_error"),
        "attempt": link.get("attempt"), "rtt_ms": st.get("rtt_ms"), "sent": st.get("frames_sent"), "received": st.get("frames_received"),
        "motion": st.get("motion"), "generation": fw.get("camera_firmware_generation"), "family": fw.get("camera_firmware_family"),
        "identified": fw.get("identified"), "arduino": fw.get("arduino_messages"), "ping_timeouts": fw.get("ping_timeouts"),
        "patch": fw.get("locked_in_patch"), "tx": [f for f in fw.get("recent_frames") or [] if f.get("dir") != "rx"][-5:],
        "armed": safety.get("armed"), "estop": safety.get("estop_latched"), "permitted": gate.get("motion_permitted"),
        "motor_commands": gate.get("motor_commands"), "link_mode": gate.get("link_mode"),
        "camera": vision.get("camera"), "camera_label": vision.get("source_label"), "camera_reason": vision.get("reason"),
        "size": vision.get("frame_size"), "capture_fps": vs.get("capture_fps"), "processing_fps": vs.get("processing_fps"),
        "device_fps": vl.get("device_fps"), "kbps": vl.get("kbps"), "frame_kb": vl.get("frame_kb"),
        "delay_p50": vl.get("delay_above_best_ms_p50"), "delay_p95": vl.get("delay_above_best_ms_p95"), "missed": vl.get("missed"),
        "decode_errors": vl.get("decode_errors"), "reconnects": vl.get("reconnects"), "hands": vision.get("hands"),
        "confirmed": det.get("confirmed"), "suppressed": det.get("suppressed"), "speech": (ai.get("speech") or {}).get("engine"),
        "speech_parts": (ai.get("speech") or {}).get("parts"), "model": (ai.get("model") or {}).get("state"),
        "tok_s": (ai.get("model") or {}).get("tok_s"), "voice_out": (ai.get("voice") or {}).get("output"), "mic": (voice.get("input") or {}).get("running"),
        "spoken": tts.get("spoken"), "audio_error": tts.get("audio_error"), "history": [{k: h.get(k) for k in ("source_label", "text")} for h in (tts.get("history") or [])[-3:]],
        "turn": None if turn is None else {k: turn.get(k) for k in ("at", "source", "text", "stt_ms", "route", "action", "reply", "reply_source", "reply_to_audio_ms")}
        | {"intent": (turn.get("intent") or {}).get("name")},
        "decision": (s.get("coach") or {}).get("last_decision"),
    }


def lock_violations(s: dict) -> list[str]:
    """Anything in this sample that says the motor lock is not holding."""
    out = []
    if s.get("armed"):
        out.append("the robot is armed")
    if s.get("permitted"):
        out.append("the hardware gate permits motion")
    if s.get("motor_commands") not in (None, "disabled"):
        out.append(f"motor commands are {s['motor_commands']!r}")
    if s.get("link_mode") not in (None, "monitor"):
        out.append(f"the link is in {s['link_mode']!r} mode")
    if s.get("motion") not in (None, "idle"):
        out.append(f"the transport reports motion {s['motion']!r}")
    for f in s.get("tx") or []:
        if f.get("frame") != "ping":  # multi-client firmware answers "ping" itself; nothing reaches the Arduino
            out.append(f"a frame went to the robot: {f.get('frame')}")
    if s.get("family") != "multi_client" and (s.get("sent") or 0) > 0:
        out.append(f"{s['sent']} frames sent on a single-client link")
    return out


@dataclass
class Check:
    label: str
    ok: bool | None  # None: recorded, nothing to pass or fail
    value: str = ""


def p50(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def delta(window: list[dict], key: str) -> int | None:
    xs = [s[key] for s in window if s.get(key) is not None]
    return xs[-1] - xs[0] if xs else None


def eval_start(s: dict) -> list[Check]:
    """Z1: what the robot network should show right after make hardware."""
    return [
        Check(f"top bar says {LOCKED_LABEL}", s.get("mode") == LOCKED_LABEL, str(s.get("mode"))),
        Check("robot link connected", s.get("connected") is True and s.get("link") == "connected", str(s.get("link"))),
        Check("not the simulated robot", s.get("simulated") is False),
        Check("offline mode on", s.get("offline") is True),
        Check("robot identified itself", s.get("identified") is True),
        Check("robot address", None, str(s.get("target"))),
        Check("camera firmware family", None, str(s.get("generation"))),
        Check("LOCKED IN patch reported", None, str(s.get("patch"))),
    ]


def eval_camera(window: list[dict]) -> list[Check]:
    """Z2: the ESP32 camera over a steady window."""
    live = sum(s.get("camera") == "live" for s in window)
    return [
        Check("camera live throughout", bool(window) and live == len(window), f"{live}/{len(window)} samples"),
        Check(f"source is {CAMERA_LABEL}", all(s.get("camera_label") == CAMERA_LABEL for s in window)),
        Check("no reconnects", (delta(window, "reconnects") or 0) == 0, str(delta(window, "reconnects"))),
        Check("resolution", None, str(window[-1].get("size") if window else None)),
        Check("capture FPS p50", None, str(p50(s.get("capture_fps") for s in window))),
        Check("processing FPS p50", None, str(p50(s.get("processing_fps") for s in window))),
        Check("camera clock FPS p50", None, str(p50(s.get("device_fps") for s in window))),
        Check("kbps p50", None, str(p50(s.get("kbps") for s in window))),
        Check("KB per frame p50", None, str(p50(s.get("frame_kb") for s in window))),
        Check("delay above best p50 / p95 ms", None, f"{window[-1].get('delay_p50')} / {window[-1].get('delay_p95')}" if window else ""),
        Check("frames lost in transit", None, str(delta(window, "missed"))),
        Check("decode errors", None, str(delta(window, "decode_errors"))),
    ]


def eval_idle_link(window: list[dict]) -> list[Check]:
    """Z4: the monitor link left alone."""
    connected = sum(s.get("link") == "connected" for s in window)
    replies = delta(window, "arduino")
    broken = sorted({v for s in window for v in lock_violations(s)})
    return [
        Check("link stayed connected", bool(window) and connected == len(window), f"{connected}/{len(window)} samples"),
        Check("Arduino replies counted up (proves the Arduino and UART run)", (replies or 0) > 0, str(replies)),
        Check("nothing the Arduino acts on was sent", not broken, "; ".join(broken)),
        Check("WS ping p50 ms", None, str(p50(s.get("rtt_ms") for s in window))),
        Check("ping timeouts", None, str(delta(window, "ping_timeouts"))),
    ]


def eval_outage(window: list[dict], t_off: float, t_on: float, grace_s: float = 5.0) -> list[Check]:
    """Z3, Z5, Z6: the robot goes away at t_off, comes back from t_on."""

    def first(pred, since):
        return next((s["t"] - since for s in window if s["t"] >= since and pred(s)), None)

    cam_lost = first(lambda s: s.get("camera") != "live", t_off)
    link_lost = first(lambda s: s.get("link") != "connected", t_off)
    gone_until = max((s["t"] for s in window if s.get("link") != "connected" or s.get("camera") != "live"), default=t_on)
    back = [s for s in window if s["t"] > gone_until]
    recovered = bool(back) and back[-1].get("link") == "connected" and back[-1].get("camera") == "live"
    recover_s = round(gone_until - t_on, 1) if recovered else None
    return [
        Check(f"camera loss shown within {grace_s:.0f} s", cam_lost is not None and cam_lost <= grace_s, f"{cam_lost:.1f} s" if cam_lost is not None else "never"),
        Check(f"link loss shown within {grace_s:.0f} s", link_lost is not None and link_lost <= grace_s, f"{link_lost:.1f} s" if link_lost is not None else "never"),
        Check("never fell back to another camera", all(s.get("camera_label") == CAMERA_LABEL for s in window)),
        Check("never fell back to the simulated robot", all(s.get("simulated") is False for s in window)),
        Check("stayed disarmed", not any(s.get("armed") for s in window)),
        Check("came back on its own", recovered, f"{recover_s} s after the robot returned" if recovered else "did not recover"),
        Check("camera reconnects", None, str(delta(window, "reconnects"))),
        Check("link attempts", None, str(max((s.get("attempt") or 0 for s in window), default=0))),
    ]


def eval_turn(s: dict, said: str, want_route: str | None, want_session: bool = False) -> list[Check]:
    """S1, S2: one spoken sentence through the whole stack."""
    t = s.get("turn") or {}
    heard = (t.get("text") or "").lower()
    words = [w for w in said.lower().replace(",", "").replace(".", "").split() if len(w) > 3]
    hits = sum(w in heard for w in words)
    out = [
        Check("came from the microphone", t.get("source") == "voice", str(t.get("source"))),
        Check("the recogniser heard it", bool(words) and hits >= 0.6 * len(words), repr(t.get("text"))),
        Check("both recognisers ready", all(v == "ready" for v in (s.get("speech_parts") or {"none": "missing"}).values()), str(s.get("speech_parts"))),
        Check("a reply was spoken", bool(t.get("reply")) and t.get("reply_to_audio_ms") is not None, repr(t.get("reply"))),
        Check("no audio error", not s.get("audio_error"), str(s.get("audio_error") or "")),
        Check("voice", None, str(t.get("reply_source"))),
        Check("recognition ms", None, str(t.get("stt_ms"))),
        Check("key released to voice ms", None, str(t.get("reply_to_audio_ms"))),
        Check("intent / action", None, f"{t.get('intent')} / {t.get('action')}"),
    ]
    if want_route:
        out.insert(3, Check(f"route is {want_route}", t.get("route") == want_route, str(t.get("route"))))
    if want_session:
        out.append(Check("a session started", s.get("session") is True))
    return out


class Run:
    def __init__(self, base: str, out_dir: Path, scale: float):
        self.base, self.dir, self.scale = base, out_dir, scale
        self.samples: list[dict] = []
        self.backend_up = False
        self.violations: list[dict] = []
        self.results: list[dict] = []
        self._stop = threading.Event()
        self._log = (out_dir / "samples.jsonl").open("a")
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def get_state(self) -> dict | None:
        try:
            with urllib.request.urlopen(f"{self.base}/api/state", timeout=2) as r:  # noqa: S310 - fixed local URL, GET only
                return json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _poll(self) -> None:
        while not self._stop.is_set():
            state, now = self.get_state(), time.time()
            self.backend_up = state is not None
            sample = {"t": now, "backend": False}
            if state is not None:
                sample = {"t": now, "backend": True, **compact(state)}
                broken = lock_violations(sample)
                if broken:
                    self.violations.append({"t": now, "why": broken})
                    if len(self.violations) == 1:
                        print("\n\n  !!! MOTOR LOCK NOT HOLDING: " + "; ".join(broken) + "\n  !!! Switch the car off now, then tell Claude.\n", flush=True)
            self.samples.append(sample)
            self._log.write(json.dumps(sample) + "\n")
            self._log.flush()
            self._stop.wait(1.0 * self.scale)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._log.close()

    def latest(self) -> dict:
        ups = [s for s in self.samples if s.get("backend")]
        return ups[-1] if ups else {}

    def since(self, t: float) -> list[dict]:
        return [s for s in self.samples if s["t"] >= t and s.get("backend")]

    def watch(self, seconds: float, what: str) -> list[dict]:
        t0, seconds = time.time(), seconds * self.scale
        while (left := seconds - (time.time() - t0)) > 0:
            print(f"\r  {what}: {left:4.0f} s left ", end="", flush=True)
            time.sleep(min(1.0, left))
        print("\r" + " " * 60 + "\r", end="")
        return self.since(t0)

    def wait_for(self, pred: Callable[[dict], bool], timeout_s: float, what: str) -> float | None:
        t0 = time.time()
        while time.time() - t0 < timeout_s * self.scale:
            if self.backend_up and pred(self.latest()):
                return time.time() - t0
            print(f"\r  waiting for {what}… {time.time() - t0:4.0f} s ", end="", flush=True)
            time.sleep(0.2)
        print()
        return None

    @staticmethod
    def enter(prompt: str) -> float:
        input(f"  {prompt} [Enter] ")
        return time.time()

    @staticmethod
    def yes(question: str) -> bool | None:
        a = input(f"  {question} [y/n, Enter = could not tell] ").strip().lower()[:1]
        return {"y": True, "n": False}.get(a)


def turn_step(said: str, want_route: str | None, want_session: bool = False) -> Callable[[Run], list[Check]]:
    def run(r: Run) -> list[Check]:
        before = (r.latest().get("turn") or {}).get("at")
        print(f'  Hold Space in the dashboard, say: "{said}", release.')
        if r.wait_for(lambda s: (s.get("turn") or {}).get("at") not in (None, before), 45, "a spoken turn") is None:
            return [Check("a spoken turn arrived", False, "nothing in 45 s: microphone permission, or Space not held?")]
        r.wait_for(lambda s: (s.get("turn") or {}).get("reply_to_audio_ms") is not None, 15, "the voice")
        time.sleep(1.0 * r.scale)
        return eval_turn(r.latest(), said, want_route, want_session)
    return run


def run_z1(r: Run) -> list[Check]:
    r.wait_for(lambda s: s.get("mode") == LOCKED_LABEL, 15, "the robot link")
    return eval_start(r.latest())


def run_z2(r: Run) -> list[Check]:
    return eval_camera(r.watch(60, "camera"))


def run_z4(r: Run) -> list[Check]:
    return eval_idle_link(r.watch(120, "idle link, hands off"))


def run_power_cycle(r: Run) -> list[Check]:
    t_off = r.enter("Press Enter at the moment you switch the car OFF.")
    r.watch(10, "leave it off")
    t_on = r.enter("Switch the car ON and press Enter at that moment.")
    r.wait_for(lambda s: s.get("link") == "connected" and s.get("camera") == "live", 90, "robot and camera to come back")
    r.watch(5, "making sure it stays up")
    return eval_outage(r.since(t_off), t_off, t_on)


def run_network_switch(r: Run) -> list[Check]:
    t_off = r.enter("Press Enter as you switch this Mac's Wi-Fi to your home network.")
    r.watch(20, "stay on the home network")
    t_on = r.enter("Switch back to Zeus_Car and press Enter once it says connected.")
    r.wait_for(lambda s: s.get("link") == "connected" and s.get("camera") == "live", 60, "robot and camera to come back")
    r.watch(5, "making sure it stays up")
    return eval_outage(r.since(t_off), t_off, t_on)


def run_estop(r: Run) -> list[Check]:
    sent0 = r.latest().get("sent")
    print("  Press E-STOP in the top bar (it speaks a line if the voice is on).")
    latched = r.wait_for(lambda s: s.get("estop") is True, 60, "E-STOP")
    frames_during = (r.latest().get("sent") or 0) - (sent0 or 0)
    print("  Press Reset.")
    cleared = r.wait_for(lambda s: s.get("estop") is False, 60, "Reset")
    return [
        Check("E-STOP latched", latched is not None),
        Check("no frame sent", frames_during == 0 or r.latest().get("family") == "multi_client", str(frames_during)),
        Check("Reset cleared it", cleared is not None),
        Check("still disarmed", not r.latest().get("armed")),
    ]


def run_voice_offline(r: Run) -> list[Check]:
    spoken0 = r.latest().get("spoken") or 0
    r.enter("Personality + Voice: press two voice samples. Then type 'how am I doing' in the Voice panel. Press Enter here when you have heard them.")
    s = r.latest()
    labels = [h.get("source_label") for h in s.get("history") or []]
    return [
        Check("lines were spoken", (s.get("spoken") or 0) > spoken0, str((s.get("spoken") or 0) - spoken0)),
        Check("no audio error", not s.get("audio_error"), str(s.get("audio_error") or "")),
        Check("voices used", None, ", ".join(str(x) for x in labels)),
        Check("offline mode on", s.get("offline") is True),
    ]


def run_camera_view(r: Run) -> list[Check]:
    r.enter("Put the car where it will sit in the demo, sit down, draw the zone in Vision > Zone. Press Enter when done.")
    t0 = r.enter("Make 10 deliberate reaches toward the phone. Press Enter to START counting, then reach.")
    conf0, supp0 = r.latest().get("confirmed") or 0, r.latest().get("suppressed") or 0
    r.enter("Press Enter when the 10 reaches are done.")
    conf1, supp1 = r.latest().get("confirmed") or 0, r.latest().get("suppressed") or 0
    hands = sum(1 for s in r.since(t0) if s.get("hands"))
    r.enter("Now 10 other movements: typing, drinking, waving. Press Enter to START, then move.")
    conf2 = r.latest().get("confirmed") or 0
    r.enter("Press Enter when the 10 movements are done.")
    conf3 = r.latest().get("confirmed") or 0
    return [
        Check("reaches confirmed of 10", None, str(conf1 - conf0)),
        Check("reaches suppressed", None, str(supp1 - supp0)),
        Check("seconds with a hand in view", None, str(hands)),
        Check("false confirmations in 10 other movements", None, str(conf3 - conf2)),
    ]


def run_reach_reaction(r: Run) -> list[Check]:
    spoken0, conf0 = r.latest().get("spoken") or 0, r.latest().get("confirmed") or 0
    print("  A session must be running. Reach for the phone in the camera's view.")
    seen = r.wait_for(lambda s: (s.get("confirmed") or 0) > conf0, 60, "a confirmed reach")
    r.watch(6, "waiting for its reaction")
    s = r.latest()
    return [
        Check("reach confirmed", seen is not None),
        Check("it spoke", (s.get("spoken") or 0) > spoken0, str((s.get("spoken") or 0) - spoken0)),
        Check("decision", None, json.dumps(s.get("decision"))[:200]),
        Check("still no motion", not lock_violations(s)),
    ]


def run_exit(r: Run) -> list[Check]:
    t0 = r.enter("Press Ctrl-C in the make hardware terminal and press Enter here at the same moment.")
    while r.backend_up and time.time() - t0 < 15 * r.scale:
        time.sleep(0.1)
    took = time.time() - t0
    return [Check("backend gone within 5 s", not r.backend_up and took <= 5, f"{took:.1f} s" if not r.backend_up else "still running after 15 s: close the dashboard tab, press Ctrl-C again")]


@dataclass
class Step:
    id: str
    title: str
    intro: str
    run: Callable[[Run], list[Check]]
    ask: tuple[str, ...] = ()


STEPS = [
    Step("Z1", "Start on the robot network", "make hardware is running, the Mac is on Zeus_Car, http://127.0.0.1:8077 is open.", run_z1, ("Does the dashboard top bar say ZEUS · MOTION LOCKED?",)),
    Step("Z2", "ESP32 camera, 60 s", "Open the Vision panel and leave it for a minute.", run_z2, ("Is the picture live and from the robot's camera?",)),
    Step("Z4", "Monitor link at idle, 2 min", "Hands off. Watch the wheels.", run_z4, ("Did all four wheels stay still the whole time?",)),
    Step("S1", "Whole stack, fixed line", "Push-to-talk, deterministic parser, cached Fish voice.", turn_step("Lock me in for 25 minutes.", None, want_session=True), ("Did you hear the reply out of the speaker you chose?", "Did the timer start?")),
    Step("S2", "Whole stack, local model", "Push-to-talk, Qwen writes the reply, Kokoro speaks it.", turn_step("I have a calculus test tomorrow and I keep checking my phone.", "converse"), ("Did you hear it?", "Did the reply make sense for what you said?", "Did the wheels stay still?")),
    Step("Z9", "What the robot camera sees", "Reaches and other movements. See docs/legacy/CAMERA_PLACEMENT.md.", run_camera_view, ("Is your hand visible in the video when you reach?", "Is your face visible?")),
    Step("S3", "Camera-triggered reaction", "A reach makes it speak; it must not move.", run_reach_reaction, ("Did you hear a reaction?", "Did the wheels stay still?")),
    Step("Z7", "E-STOP is a software stop here", "Press it, read the top bar, then Reset.", run_estop, ("Did the top bar say Software stop · no motor command sent?",)),
    Step("Z8", "Voice offline", "Cached clips and the local voice with no internet.", run_voice_offline, ("Did cached lines sound like the Fish voice?", "Did the uncached line use the local voice?")),
    Step("Z3+Z5", "Robot power cycle", "Camera and link must drop, report it, and return alone.", run_power_cycle, ("Did the dashboard say DISCONNECTED and never show the Mac webcam?", "Did the wheels stay still when it came back?")),
    Step("Z6", "Network switch", "Leave the robot's Wi-Fi and come back, without restarting.", run_network_switch, ("Did the dashboard stay usable the whole time?",)),
    Step("Z11", "Clean exit", "Last, because it stops the backend.", run_exit, ("Does the ESP32 LED show the client gone?",)),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--only", default="", help="comma-separated step ids, for example Z1,Z2,Z4")
    ap.add_argument("--scale", type=float, default=1.0, help=argparse.SUPPRESS)  # shortens timers in rehearsals
    args = ap.parse_args()
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    out_dir = ROOT / "data" / "hardware_runs" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    r = Run(f"http://127.0.0.1:{args.port}", out_dir, args.scale)
    if (first := r.get_state()) is None:
        print(f"No backend on port {args.port}. Start make hardware first.")
        return 1
    fixed = compact(first)
    if fixed.get("motor_commands") != "disabled" or fixed.get("link_mode") != "monitor" or lock_violations(fixed):
        print("This tool is for the no-motion tests only. The motor lock is not in its default state:")
        print("  motor_commands =", fixed.get("motor_commands"), " link_mode =", fixed.get("link_mode"), " ", "; ".join(lock_violations(fixed)))
        return 1
    r.start()
    time.sleep(1.2 * args.scale)
    print(f"Recording to {out_dir.relative_to(ROOT)}. This tool only reads the backend's state; it sends nothing to the robot.")
    try:
        for step in STEPS:
            if only and step.id not in only:
                continue
            print(f"\n=== {step.id} · {step.title}\n  {step.intro}")
            a = input("  Enter = start, s = skip, q = finish › ").strip().lower()[:1]
            if a == "q":
                break
            if a == "s":
                r.results.append({"id": step.id, "title": step.title, "result": "NOT RUN", "checks": [], "answers": {}, "note": None})
                continue
            checks = step.run(r)
            answers = {q: r.yes(q) for q in step.ask}
            note = input("  Note (Enter = none) › ").strip() or None
            bad = [c.label for c in checks if c.ok is False] + [q for q, v in answers.items() if v is False]
            unknown = [q for q, v in answers.items() if v is None]
            result = "FAIL" if bad else ("PARTIAL" if unknown else "PASS")
            r.results.append({"id": step.id, "title": step.title, "result": result, "checks": [vars(c) for c in checks], "answers": answers, "note": note})
            for c in checks:
                mark = {True: "ok  ", False: "FAIL", None: "    "}[c.ok]
                print(f"  [{mark}] {c.label}" + (f": {c.value}" if c.value else ""))
            print(f"  --> {step.id}: {result}" + (f" ({'; '.join(bad)})" if bad else ""))
            (out_dir / "results.json").write_text(json.dumps(r.results, indent=1))
    except (KeyboardInterrupt, EOFError):
        print("\nStopped early.")
    finally:
        r.stop()
    ups = [s for s in r.samples if s.get("backend")]
    held = not r.violations
    target = next((s.get("target") for s in ups if s.get("target")), None)
    summary = {"target": target, "samples": len(ups), "motor_lock_held": held, "violations": r.violations[:20], "results": r.results}
    (out_dir / "results.json").write_text(json.dumps(summary, indent=1))
    lines = [f"# Zeus no-motion checks, {out_dir.name}", "", f"Robot address: {target}", f"Motor lock held in all {len(ups)} samples: {'yes' if held else 'NO'}", "",
             "| Step | Test | Result |", "|---|---|---|"]
    lines += [f"| {x['id']} | {x['title']} | {x['result']} |" for x in r.results]
    for x in r.results:
        if x["checks"] or x["answers"]:
            lines += ["", f"## {x['id']} · {x['title']}: {x['result']}", ""]
            lines += [f"- {'ok' if c['ok'] else 'FAIL' if c['ok'] is False else 'recorded'}: {c['label']}" + (f": {c['value']}" if c["value"] else "") for c in x["checks"]]
            lines += [f"- person: {q} {'yes' if v else 'no' if v is False else 'could not tell'}" for q, v in x["answers"].items()]
            lines += [f"- note: {x['note']}"] if x["note"] else []
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines[:7 + len(r.results)]))
    print(f"\nSaved in {out_dir.relative_to(ROOT)} (git-ignored).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
