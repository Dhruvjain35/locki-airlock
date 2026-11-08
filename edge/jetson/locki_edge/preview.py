"""Owner preview: the newest camera frame as JPEG, for the owner's own dashboard.

Off unless AIRLOCK_PREVIEW_STREAM=on. It is served only by the loopback API (GET /stream.mjpg), which the owner's Mac reaches
through its SSH tunnel. A preview frame is never logged, stored or published, and nothing here reaches SpacetimeDB.
Drawn on it: each desk zone, green while this frame supports person_in_zone there, with the provider's confidence.
"""

import time

BOUNDARY = b"frame"
MAX_WIDTH = 640
FRESH_S = 1.0
GREEN = (0, 185, 118)   # BGR for #76b900
GREY = (170, 170, 170)


def render_preview(image, zones, observations=(), fresh=True, quality=70):
    """JPEG bytes of `image` (BGR numpy array) with the zones drawn, or None when there is nothing to encode."""
    if image is None:
        return None
    import cv2  # a hard dependency of capture and every image provider already

    h, w = image.shape[:2]
    if w > MAX_WIDTH:
        image = cv2.resize(image, (MAX_WIDTH, int(round(h * MAX_WIDTH / float(w)))), interpolation=cv2.INTER_AREA)
        h, w = image.shape[:2]
    else:
        image = image.copy()
    occupied = {}
    if fresh:
        for o in observations or ():
            if getattr(o, "kind", None) == "person_in_zone":
                occupied[o.zone_id] = o
    for z in zones or ():
        p1 = (int(z.x1 * w), int(z.y1 * h))
        p2 = (max(p1[0] + 1, int(z.x2 * w) - 1), max(p1[1] + 1, int(z.y2 * h) - 1))
        o = occupied.get(z.zone_id)
        color = GREEN if o is not None else GREY
        cv2.rectangle(image, p1, p2, color, 2)
        if o is None:
            label = "%s clear" % z.zone_id
        elif o.confidence is None:
            label = "PERSON %s" % z.zone_id
        else:
            label = "PERSON %s %.2f" % (z.zone_id, o.confidence)
        cv2.putText(image, label, (p1[0] + 8, p1[1] + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(image, label, (p1[0] + 8, p1[1] + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else None


def part(jpeg):
    """One multipart/x-mixed-replace part."""
    return b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode("ascii") + b"\r\n\r\n" + jpeg + b"\r\n"


def is_fresh(at_mono, now=None):
    return at_mono is not None and ((now if now is not None else time.monotonic()) - at_mono) <= FRESH_S
