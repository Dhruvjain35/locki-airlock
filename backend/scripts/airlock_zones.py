"""Draw the AIRLOCK zones on the live camera picture, and watch the detectors against them.

  .venv/bin/python scripts/airlock_zones.py [--source 0] [--label mac_webcam]

A window shows the camera with the zones, person boxes and hand landmarks drawn on it.
  1-5        choose a zone: 1 desk, 2 keyboard_zone, 3 laptop_zone, 4 usb_port_zone, 5 protected_device_zone
  click      add a corner to the chosen zone (3 to 16 corners)
  u / c      undo the last corner / clear the chosen zone
  [ / ]      how tall a person must look to be at the desk (desk_min_height), down / up by 0.05
  s          save to data/airlock/zones.json        q   quit (without saving unsaved changes)

The picture is shown here only; nothing is saved but the zone corners.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import cv2  # noqa: E402

from app.vision.airlock.geometry import ZONE_NAMES, ZoneMap, clean_polygon  # noqa: E402
from app.vision.airlock.inference import MediaPipePersonDetector  # noqa: E402
from app.vision.hands import HAND_CONNECTIONS, HandTracker  # noqa: E402
from app.vision.webcam import CameraSource  # noqa: E402

DATA = BACKEND.parent / "data"
COLOURS = {"desk": (200, 200, 200), "keyboard_zone": (255, 200, 0), "laptop_zone": (0, 200, 255), "usb_port_zone": (60, 60, 255), "protected_device_zone": (200, 0, 200)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="0")
    ap.add_argument("--label", default="mac_webcam")
    ap.add_argument("--zones", type=Path, default=DATA / "airlock" / "zones.json")
    args = ap.parse_args()

    zones = ZoneMap.load(args.zones)
    drafts = {name: list(pts) for name, pts in zones.zones.items()}
    state = {"zone": "usb_port_zone", "dirty": False}
    people = MediaPipePersonDetector(BACKEND / "models")
    hands = HandTracker(BACKEND / "models")
    cam = CameraSource(args.source, 640, 480, label=args.label)
    cam.start()
    size = [640, 480]

    def on_mouse(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            drafts.setdefault(state["zone"], []).append((round(x / size[0], 4), round(y / size[1], 4)))
            state["dirty"] = True

    cv2.namedWindow("AIRLOCK zones")
    cv2.setMouseCallback("AIRLOCK zones", on_mouse)
    last, message, message_until = 0, "", 0.0
    try:
        while True:
            item = cam.read_latest(last, timeout=1.0)
            if item is None:
                if cv2.waitKey(10) & 0xFF == ord("q"):
                    break
                continue
            last, bgr, _t = item
            h, w = bgr.shape[:2]
            size[:] = [w, h]
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            ts = int(time.time() * 1000)
            view = bgr.copy()
            for name, pts in drafts.items():
                if not pts:
                    continue
                poly = [(int(x * w), int(y * h)) for x, y in pts]
                thick = 3 if name == state["zone"] else 1
                for a, b in zip(poly, poly[1:] + poly[:1] if len(poly) >= 3 else poly[1:], strict=False):  # closed once it is a polygon
                    cv2.line(view, a, b, COLOURS[name], thick)
                for p in poly:
                    cv2.circle(view, p, 3, COLOURS[name], -1)
                cv2.putText(view, name, poly[0], cv2.FONT_HERSHEY_SIMPLEX, 0.45, COLOURS[name], 1)
            for det in people.detect(rgb, ts):
                b = det.box
                tall = b.h >= zones.desk_min_height
                cv2.rectangle(view, (int(b.x * w), int(b.y * h)), (int((b.x + b.w) * w), int((b.y + b.h) * h)), (0, 255, 0) if tall else (0, 140, 255), 2)
                cv2.putText(view, f"person {det.score:.2f} h={b.h:.2f}{' desk-height' if tall else ''}", (int(b.x * w), max(12, int(b.y * h) - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
            for hand in hands.detect(rgb, ts):
                pts = [(int(x * w), int(y * h)) for x, y in hand.landmarks]
                for a, b in HAND_CONNECTIONS:
                    cv2.line(view, pts[a], pts[b], (255, 255, 255), 1)
                for i in (4, 8, 12, 16, 20):
                    cv2.circle(view, pts[i], 4, (0, 255, 255), -1)
            cv2.putText(view, f"editing {state['zone']} (1-5)  desk_min_height {zones.desk_min_height:.2f} ([ ])  s save  q quit"
                        + ("  *unsaved*" if state["dirty"] else ""), (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
            if time.monotonic() < message_until:
                cv2.putText(view, message, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
            cv2.imshow("AIRLOCK zones", view)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if ord("1") <= key <= ord("5"):
                state["zone"] = ZONE_NAMES[key - ord("1")]
            elif key == ord("u") and drafts.get(state["zone"]):
                drafts[state["zone"]].pop()
                state["dirty"] = True
            elif key == ord("c"):
                drafts[state["zone"]] = []
                state["dirty"] = True
            elif key in (ord("["), ord("]")):
                zones.desk_min_height = round(min(1.0, max(0.0, zones.desk_min_height + (0.05 if key == ord("]") else -0.05))), 2)
                state["dirty"] = True
            elif key == ord("s"):
                try:
                    clean = {name: clean_polygon(pts) for name, pts in drafts.items() if pts}
                    zones.zones, zones.calibrated, zones.camera = clean, True, args.label
                    if "desk" not in clean:
                        raise ValueError("draw the desk zone first")
                    zones.save(args.zones)
                    state["dirty"] = False
                    message = f"saved {', '.join(clean)}"
                except ValueError as exc:
                    message = f"not saved: {exc}"
                message_until = time.monotonic() + 3
    finally:
        cam.stop()
        people.close()
        hands.close()
        cv2.destroyAllWindows()
    if state["dirty"]:
        print("quit with unsaved changes")
    print(f"zones file: {args.zones}")
    return 0


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
