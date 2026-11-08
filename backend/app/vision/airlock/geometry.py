"""Image geometry for the AIRLOCK camera: boxes, zone polygons and the local zone file.

Coordinates are fractions of the delivered frame (0..1, origin top-left), so a zone drawn once keeps
working when the resolution changes. Only the standard library: this module also runs on the Jetson.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ZONE_NAMES = ("desk", "keyboard_zone", "laptop_zone", "usb_port_zone", "protected_device_zone")
Point = tuple[float, float]


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def center(self) -> Point:
        return (self.x + self.w / 2, self.y + self.h / 2)


def point_in_polygon(x: float, y: float, poly: list[Point]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def box_fraction_in(box: Box, poly: list[Point], steps: int = 8) -> float:
    """Share of the box inside the polygon, sampled on a steps x steps grid: exact enough for zones."""
    if box.area <= 0 or len(poly) < 3:
        return 0.0
    hits = 0
    for i in range(steps):
        for j in range(steps):
            if point_in_polygon(box.x + box.w * (i + 0.5) / steps, box.y + box.h * (j + 0.5) / steps, poly):
                hits += 1
    return hits / (steps * steps)


def polygon_area(poly: list[Point]) -> float:
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def clean_polygon(points) -> list[Point]:
    """A zone is 3 to 16 points inside the frame. Anything else is refused, not repaired."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if not 3 <= len(pts) <= 16:
        raise ValueError("a zone needs 3 to 16 points")
    if any(not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0) for x, y in pts):
        raise ValueError("zone points are fractions of the frame, between 0 and 1")
    if polygon_area(pts) < 0.0005:
        raise ValueError("zone is too small to watch")
    return pts


# A laptop's own webcam looks at whoever sits in front of it, so the desk is most of the view. A person
# far behind that seat fills only part of the picture height, which is what desk_min_height is for.
DEFAULT_ZONES: dict[str, list[Point]] = {"desk": [(0.08, 0.05), (0.92, 0.05), (0.92, 1.0), (0.08, 1.0)]}
DEFAULT_DESK_MIN_HEIGHT = 0.45


@dataclass
class ZoneMap:
    zones: dict[str, list[Point]] = field(default_factory=lambda: dict(DEFAULT_ZONES))
    desk_min_height: float = DEFAULT_DESK_MIN_HEIGHT  # a person's box must be this tall (fraction of the frame) to be at the desk
    camera: str = "mac_webcam"
    calibrated: bool = False  # False until the owner draws the zones for this camera position

    def get(self, name: str) -> list[Point] | None:
        return self.zones.get(name)

    def to_dict(self) -> dict:
        return {
            "zones": {k: [list(p) for p in v] for k, v in self.zones.items()},
            "desk_min_height": self.desk_min_height,
            "camera": self.camera,
            "calibrated": self.calibrated,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ZoneMap:
        zones = {}
        for name, pts in (data.get("zones") or {}).items():
            if name not in ZONE_NAMES:
                raise ValueError(f"unknown zone {name!r}; zones are {', '.join(ZONE_NAMES)}")
            zones[name] = clean_polygon(pts)
        if "desk" not in zones:
            zones["desk"] = list(DEFAULT_ZONES["desk"])
        height = float(data.get("desk_min_height", DEFAULT_DESK_MIN_HEIGHT))
        if not 0.0 <= height <= 1.0:
            raise ValueError("desk_min_height is a fraction of the frame height")
        return cls(zones=zones, desk_min_height=height, camera=str(data.get("camera") or "mac_webcam")[:60], calibrated=bool(data.get("calibrated")))

    @classmethod
    def load(cls, path: Path) -> ZoneMap:
        try:
            return cls.from_dict(json.loads(Path(path).read_text()))
        except FileNotFoundError:
            return cls()

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        os.replace(tmp, path)
