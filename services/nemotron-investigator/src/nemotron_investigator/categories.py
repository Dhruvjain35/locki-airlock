"""Event categorization and per-category metadata allowlists.

ASSUMPTION (to confirm with Dhruv / Areeb / Sid): the exact `source` and `kind`
strings written by the macOS watchers and the Jetson CV runtime. The schema
fixes the columns (`source`, `kind`, `classification`, `metadata_json`) but not
their vocabularies. This module is the single place to update when the real
vocabularies are confirmed; tools never hardcode strings elsewhere.
"""

from __future__ import annotations

from .contracts import DeviceEventRow

USB = "usb"
VISION = "vision"
APPLICATION = "application"
SYSTEM = "system"

_SOURCE_MAP = {
    "usb": USB, "macos_usb": USB, "mac_usb": USB, "iokit": USB, "ioreg": USB,
    "vision": VISION, "cv": VISION, "camera": VISION, "jetson": VISION,
    "jetson_cv": VISION, "edge_cv": VISION, "logitech": VISION,
    "app": APPLICATION, "apps": APPLICATION, "macos_app": APPLICATION,
    "mac_app": APPLICATION, "workspace": APPLICATION, "nsworkspace": APPLICATION,
}
_VISION_KIND_PREFIXES = ("person", "face", "owner", "hand", "desk", "presence", "motion", "occupancy")
_APP_KIND_HINTS = ("app_", "application", "frontmost", "process_")


def categorize(ev: DeviceEventRow) -> str:
    src = ev.source.strip().lower()
    if src in _SOURCE_MAP:
        return _SOURCE_MAP[src]
    kind = ev.kind.strip().lower()
    if "usb" in kind or "usb" in src:
        return USB
    if kind.startswith(_VISION_KIND_PREFIXES) or "vision" in src or "camera" in src:
        return VISION
    if any(h in kind for h in _APP_KIND_HINTS) or "app" in src:
        return APPLICATION
    return SYSTEM


# Structured metadata keys passed to the model per category (primitive values only).
STRUCTURED_KEYS = {
    USB: ("vendor_id", "product_id", "serial_hash", "location_id", "device_class",
          "speed", "mass_storage", "is_known", "known_device", "port"),
    VISION: ("track_id", "zone", "camera_id", "face_match", "face_match_score",
             "identity_label", "duration_ms", "owner_present", "frame_count"),
    APPLICATION: ("bundle_id", "approved", "pid", "signed", "team_id", "launch_type",
                  "parent_bundle_id"),
    SYSTEM: ("state", "reason", "component"),
}
# Free-text keys that can be attacker-controlled (USB descriptors, app names,
# window titles). They are passed only inside `untrusted_text` and scanned.
UNTRUSTED_TEXT_KEYS = {
    USB: ("product_name", "vendor_name", "manufacturer", "serial_display", "name"),
    VISION: ("label", "description", "note"),
    APPLICATION: ("app_name", "window_title", "name", "path"),
    SYSTEM: ("detail", "note", "message"),
}
