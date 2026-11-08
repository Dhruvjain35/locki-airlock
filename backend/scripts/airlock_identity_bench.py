"""Measure the AIRLOCK camera pipeline end to end on a real camera: detection, face identity, events and the
security mode, as P50/P95. Frames stay in memory; nothing is saved or sent (events are local only).

  .venv/bin/python scripts/airlock_identity_bench.py --seconds 30
  .venv/bin/python scripts/airlock_identity_bench.py --seconds 30 --json data/airlock_runs/identity-bench.json

What each number means:
  person detect     EfficientDet-Lite0 on one frame
  face pass         one YuNet pass (half-size frame) plus any comparisons it needed
  face compare      SFace alignment, embedding and comparison of one face (only when a track needs it)
  presence          first frame with someone at the desk -> person_in_zone (includes the 3-of-5 frame debounce)
  identity          a face track's first frame -> its first settled state (owner, unrecognized or uncertain)
  capture -> event  the frame that settled an observation -> the event built and queued for the publisher
  event -> mode     the event -> the security mode updated (in this process; the voice reacts on the same call)
Identity is only owner or unrecognized once the owner has enrolled (scripts/airlock_face.py enroll --i-consent).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, deque
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.security.identity import load_identity  # noqa: E402
from app.security.modes import ModeEngine  # noqa: E402
from app.security.publisher import LocalOnlyTransport, SecurityEventPublisher  # noqa: E402
from app.vision.airlock.enrollment import load_template, template_path  # noqa: E402
from app.vision.airlock.events import SecurityEventSink  # noqa: E402
from app.vision.airlock.faces import FaceStage  # noqa: E402
from app.vision.airlock.geometry import ZoneMap  # noqa: E402
from app.vision.airlock.interaction import HandStage  # noqa: E402
from app.vision.airlock.pipeline import AirlockVision  # noqa: E402

DATA = BACKEND.parent / "data"


def pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(len(v) * q))], 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="0")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    zones = ZoneMap.load(DATA / "airlock" / "zones.json")
    template = load_template(template_path(DATA))
    faces = FaceStage(BACKEND / "models", template)
    hands = HandStage(BACKEND / "models", zones) if zones.calibrated and len(zones.zones) > 1 else None
    directory = DATA / "airlock_vision"
    identity = load_identity(directory, prefix="cam")
    publisher = SecurityEventPublisher(identity.device_id, LocalOnlyTransport(), directory=directory)
    transitions: list[dict] = []
    modes = ModeEngine(on_change=lambda prev, new, reason, ev, st: transitions.append({"t": time.monotonic(), "from": prev.value, "to": new.value, "reason": reason}))
    modes.set_armed(True, "bench")  # a local bench session: nothing is armed anywhere else
    t_start = time.monotonic()
    publish_ms: deque = deque(maxlen=500)
    mode_ms: deque = deque(maxlen=500)
    presence_ms: deque = deque(maxlen=100)
    kinds: Counter = Counter()
    lines: list[str] = []

    def on_event(event, ev):
        now = time.monotonic()
        publish_ms.append((now - ev.emitted_mono) * 1000)
        t0 = time.perf_counter()
        modes.on_camera(event)
        mode_ms.append((time.perf_counter() - t0) * 1000)
        kinds[event.kind.value] += 1
        m = event.metadata
        if event.kind.value == "person_in_zone" and m.get("dwell_ms") is not None:
            presence_ms.append(m["dwell_ms"] + m.get("capture_to_event_ms", 0))
        line = (f"+{now - t_start:6.2f}s {event.kind.value:<20} conf={event.confidence} track={m.get('track_id')} "
                f"capture->event {m.get('capture_to_event_ms')} ms  mode {modes.state.current_security_mode.value}")
        lines.append(line)
        print(line, flush=True)

    sink = SecurityEventSink(publisher, armed=lambda: (True, None), on_event=on_event)
    vision = AirlockVision(BACKEND / "models", sink, zones=zones, faces=faces, hands=hands)
    print(f"camera {args.source}, {args.seconds:g} s; owner enrolled: {template is not None}; zones calibrated: {zones.calibrated}; hands: {hands is not None}. "
          "Frames are not saved.", flush=True)
    vision.start(args.source)
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and (vision.running or time.monotonic() - t_start < 3):
        time.sleep(0.1)
        modes.tick()
    snap = vision.snapshot()
    vision.close()
    publisher.close()
    s, f = snap["stats"], snap["faces"] or {}
    result = {
        "camera": args.source, "seconds": args.seconds, "owner_enrolled": template is not None, "zones_calibrated": zones.calibrated,
        "frames_processed": s["frames_processed"], "processing_fps": s["processing_fps"], "process_rss_mb": s["process_rss_mb"],
        "person_detect_ms": {"p50": s["detect_ms_p50"], "p95": s["detect_ms_p95"]},
        "face_pass_ms": {"p50": f.get("ms_p50"), "p95": f.get("ms_p95")},
        "face_detect_ms": {"p50": f.get("detect_ms_p50"), "p95": f.get("detect_ms_p95")},
        "face_compare_ms": {"p50": f.get("compare_ms_p50"), "p95": f.get("compare_ms_p95"), "n": f.get("compares")},
        "identity_ms": f.get("identity_ms"),
        "presence_ms": {"p50": pct(presence_ms, 0.5), "p95": pct(presence_ms, 0.95), "n": len(presence_ms)},
        "capture_to_event_ms": {"p50": s["capture_to_event_ms_p50"], "p95": s["capture_to_event_ms_p95"]},
        "event_to_sink_done_ms": {"p50": pct(publish_ms, 0.5), "p95": pct(publish_ms, 0.95)},
        "event_to_mode_ms": {"p50": pct(mode_ms, 0.5), "p95": pct(mode_ms, 0.95)},
        "events": dict(kinds), "transitions": [{**t, "t": round(t["t"] - t_start, 2)} for t in transitions],
        "face_tracks": [{k: v for k, v in tr.items() if k in ("id", "state", "compares", "cached_passes")} for tr in f.get("tracks", [])],
        "error": snap["error"],
    }
    print("\n" + json.dumps({k: v for k, v in result.items() if k not in ("transitions",)}, indent=1), flush=True)
    print("mode transitions: " + ", ".join(f"{t['t']} s {t['from']}->{t['to']} ({t['reason']})" for t in result["transitions"]), flush=True)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({**result, "event_lines": lines}, indent=1) + "\n")
    return 0 if snap["error"] is None else 1


def _locked_main(lock_factory) -> int:
    """One user of the device at a time: if the backend (a demo or an armed session) or another script holds it, say who."""
    from app.security.device_lock import DeviceBusy

    try:
        with lock_factory(DATA):
            return main()
    except DeviceBusy as exc:
        print(exc)
        return 3


if __name__ == "__main__":
    from app.security.device_lock import camera_lock

    raise SystemExit(_locked_main(camera_lock))
