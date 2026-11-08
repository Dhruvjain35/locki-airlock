"""The annotated preview for the dashboard: the real frame with what the pipeline actually found on it.

Boxes, face states, scores, zones and hand points come from the detectors' own output for that frame;
nothing is drawn that was not detected. The JPEG is made only while someone is watching, is served to
this machine's dashboard only (the backend listens on 127.0.0.1), and is never stored.
"""

from __future__ import annotations

import numpy as np

# BGR, close to the dashboard palette: live cyan, hit magenta, caution amber, ok green
CYAN, MAGENTA, AMBER, GREEN, WHITE, GREY, RED = (255, 230, 60), (190, 60, 255), (40, 200, 255), (150, 220, 90), (235, 235, 235), (150, 150, 150), (60, 60, 230)
STATE_COLOUR = {"owner_face_match": GREEN, "unrecognized_face": MAGENTA, "face_uncertain": AMBER, None: CYAN}
STATE_LABEL = {"owner_face_match": "OWNER", "unrecognized_face": "NOT RECOGNIZED", "face_uncertain": "UNCERTAIN", None: "CHECKING"}
ZONE_COLOUR = {"desk": GREY, "keyboard_zone": CYAN, "laptop_zone": (255, 160, 60), "usb_port_zone": MAGENTA, "protected_device_zone": AMBER}
MODE_COLOUR = {"NORMAL": GREY, "WATCHFUL": AMBER, "DEFENSIVE": MAGENTA, "CRITICAL": RED, "OWNER_RETURNED": GREEN}
HAND_TIPS = (4, 8, 12, 16, 20)


def _text(img, s: str, org, colour, scale: float = 0.45, thick: int = 1) -> None:
    import cv2

    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thick, cv2.LINE_AA)


def render(bgr: np.ndarray, *, persons=(), faces=(), hands=(), zones=None, header: dict | None = None, enrollment: dict | None = None,
           blind: str | None = None, quality: int = 72) -> bytes:
    """``faces``: (FaceDet, state, score or None, problem or None, track id). Returns JPEG bytes."""
    import cv2

    img = bgr.copy()
    h, w = img.shape[:2]
    if zones is not None:
        for name, pts in zones.zones.items():
            poly = np.array([(int(x * w), int(y * h)) for x, y in pts], np.int32)
            cv2.polylines(img, [poly], True, ZONE_COLOUR.get(name, GREY), 1, cv2.LINE_AA)
            _text(img, name.replace("_zone", ""), (int(pts[0][0] * w) + 4, int(pts[0][1] * h) + 14), ZONE_COLOUR.get(name, GREY), 0.4)
    for p in persons:
        b = p.box
        x1, y1, x2, y2 = int(b.x * w), int(b.y * h), int((b.x + b.w) * w), int((b.y + b.h) * h)
        cv2.rectangle(img, (x1, y1), (x2, y2), CYAN, 1, cv2.LINE_AA)
        _text(img, f"person {p.score:.2f}", (x1 + 3, max(14, y1 - 5)), CYAN, 0.42)
    for face, state, score, problem, track in faces:
        b = face.box
        x1, y1, x2, y2 = int(b.x * w), int(b.y * h), int((b.x + b.w) * w), int((b.y + b.h) * h)
        colour = STATE_COLOUR.get(state, CYAN)
        cv2.rectangle(img, (x1, y1), (x2, y2), colour, 2, cv2.LINE_AA)
        label = STATE_LABEL.get(state, "CHECKING")
        if score is not None and score == score:  # not nan
            label += f"  score {score:.2f}"
        if problem:
            label += f"  ({problem.replace('_', ' ')})"
        _text(img, label, (x1, min(h - 6, y2 + 16)), colour, 0.45, 1)
        if track is not None:
            _text(img, f"#{track}", (x1 + 3, y1 + 14), colour, 0.4)
    for hand in hands:
        pts = [(int(x * w), int(y * h)) for x, y in hand.points]
        for i in HAND_TIPS:
            if i < len(pts):
                cv2.circle(img, pts[i], 4, AMBER, -1, cv2.LINE_AA)
        if pts:
            cv2.circle(img, pts[0], 3, WHITE, -1, cv2.LINE_AA)
    if header:
        mode = str(header.get("mode") or "")
        bar = img[0:26, :]
        bar[:] = (bar * 0.35).astype(np.uint8)
        _text(img, mode or "PREVIEW", (8, 18), MODE_COLOUR.get(mode, WHITE), 0.55, 2)
        info = "  ".join(s for s in (header.get("armed"), header.get("fps"), header.get("latency"), header.get("camera")) if s)
        _text(img, info, (160, 18), WHITE, 0.42)
    if enrollment and not enrollment.get("complete"):
        done, need = enrollment.get("accepted", 0), max(1, enrollment.get("needed", 1))
        y = h - 34
        cv2.rectangle(img, (8, y), (w - 8, y + 10), GREY, 1)
        cv2.rectangle(img, (9, y + 1), (9 + int((w - 18) * done / need), y + 9), GREEN, -1)
        _text(img, f"ENROLLING {done}/{need}: {enrollment.get('hint', '')}", (8, y - 6), GREEN, 0.5, 1)
    if blind:
        overlay = img.copy()
        overlay[:] = (overlay * 0.3).astype(np.uint8)
        img = overlay
        _text(img, blind.upper(), (12, h // 2), AMBER, 0.7, 2)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return jpg.tobytes() if ok else b""
