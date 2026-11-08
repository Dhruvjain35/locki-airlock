"""Run the AIRLOCK camera pipeline on a real camera and print what it reports.

  .venv/bin/python scripts/airlock_vision.py --seconds 30             # the Mac's webcam (camera 0)
  .venv/bin/python scripts/airlock_vision.py --source 1 --label logitech_usb
  .venv/bin/python scripts/airlock_vision.py --source trial.mp4       # a recorded, consented trial

Each event is the SecurityEventV1 the security publisher built and validated, from the camera's own
device id (data/airlock_vision/identity.json), delivered local_only: nothing goes to SpacetimeDB.
Frames stay in memory and nothing is saved. The camera light is on while this runs.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.security.identity import load_identity  # noqa: E402
from app.security.publisher import LocalOnlyTransport, SecurityEventPublisher  # noqa: E402
from app.vision.airlock.events import SecurityEventSink  # noqa: E402
from app.vision.airlock.geometry import ZoneMap  # noqa: E402
from app.vision.airlock.pipeline import AirlockVision  # noqa: E402

DATA = BACKEND.parent / "data"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="0", help="camera index, MJPEG URL or video file (default 0, the Mac's webcam)")
    ap.add_argument("--label", default="mac_webcam", help="camera label carried in events (never a serial)")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--zones", type=Path, default=DATA / "airlock" / "zones.json", help="zone file (defaults if missing)")
    ap.add_argument("--desk-min-height", type=float, default=None, help="override the zone file's desk_min_height")
    ap.add_argument("--full", action="store_true", help="print each full SecurityEventV1")
    ap.add_argument("--faces", action="store_true", help="also report face states (compared with the owner template if one is enrolled)")
    ap.add_argument("--hands", action="store_true", help="also report hands in the calibrated zones (scripts/airlock_zones.py)")
    ap.add_argument("--summary", type=Path, default=None, help="also write the final snapshot as JSON here")
    args = ap.parse_args()

    zones = ZoneMap.load(args.zones)
    if args.desk_min_height is not None:
        zones.desk_min_height = args.desk_min_height
    directory = DATA / "airlock_vision"
    identity = load_identity(directory, prefix="cam")
    publisher = SecurityEventPublisher(identity.device_id, LocalOnlyTransport(), directory=directory)
    t_start = time.monotonic()

    def show(event, ev):
        m = event.metadata
        conf = "" if event.confidence is None else f" conf={event.confidence:.2f}"
        extra = " ".join(f"{k}={m[k]}" for k in ("zone", "dwell_ms", "persons_in_view", "detail") if k in m)
        print(f"+{time.monotonic() - t_start:6.2f}s  {event.kind:<20}{conf} {extra}  capture->event {m.get('capture_to_event_ms')} ms  seq {event.seq}", flush=True)
        if args.full:
            print("   " + json.dumps(event.model_dump(mode="json"), sort_keys=True), flush=True)

    faces = None
    if args.faces:
        from app.vision.airlock.enrollment import load_template, template_path
        from app.vision.airlock.faces import FaceStage

        template = load_template(template_path(DATA))
        faces = FaceStage(BACKEND / "models", template)
        print(f"faces: {'compared with the enrolled owner' if template is not None else 'nobody enrolled, so faces can only be uncertain'}", flush=True)
    hands = None
    if args.hands:
        from app.vision.airlock.interaction import HandStage

        hands = HandStage(BACKEND / "models", zones)
        print(f"hands: watching {hands.tracker.watched() or 'no zones yet: draw them with scripts/airlock_zones.py'}", flush=True)
    vision = AirlockVision(BACKEND / "models", SecurityEventSink(publisher, on_event=show), zones=zones, camera_label=args.label, faces=faces, hands=hands)
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    print(f"device {identity.device_id}; camera {args.source} ({args.label}); zones {'calibrated' if zones.calibrated else 'default'}; "
          f"desk_min_height {zones.desk_min_height}; {args.seconds:g} s. Frames are not saved.", flush=True)
    vision.start(args.source)
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and not stop["now"] and (vision.running or time.monotonic() - t_start < 2):
        time.sleep(0.1)
    snap = vision.snapshot()
    vision.close()
    publisher.flush(2.0)
    publisher.close()
    s = snap["stats"]
    cap = snap["capture"] or {}
    print(
        f"\nframes processed {s['frames_processed']}, capture {cap.get('fps')} fps, processing {s['processing_fps']} fps, dropped {cap.get('dropped')}\n"
        f"detect p50 {s['detect_ms_p50']} ms p95 {s['detect_ms_p95']} ms; capture->result p50 {s['capture_to_result_ms_p50']} p95 {s['capture_to_result_ms_p95']} ms; "
        f"capture->event p50 {s['capture_to_event_ms_p50']} p95 {s['capture_to_event_ms_p95']} ms\n"
        f"process memory {s['process_rss_mb']} MB; camera {snap['camera']['state']} (grey mean {snap['camera']['grey_mean']}, spread {snap['camera']['grey_spread']}); "
        f"events {snap['events']}; suppressed {snap['suppressed']}; sink errors {snap['sink_errors']}; error {snap['error']}"
        + ("" if snap["faces"] is None else f"\nfaces: pass p50 {snap['faces']['ms_p50']} ms p95 {snap['faces']['ms_p95']} ms every {snap['faces']['every_n_frames']} frames; enrolled {snap['faces']['enrolled']}")
        + ("" if snap["hands"] is None else f"\nhands: pass p50 {snap['hands']['ms_p50']} ms p95 {snap['hands']['ms_p95']} ms every {snap['hands']['every_n_frames']} frames; zones {snap['hands']['watching']}"),
        flush=True,
    )
    if args.summary:
        args.summary.write_text(json.dumps(snap, indent=2, default=str) + "\n")
    return 0 if snap["error"] is None and snap["sink_errors"] == 0 else 1


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
