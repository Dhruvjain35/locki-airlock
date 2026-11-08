"""Event categorization and per-category metadata allowlists.

The live vocabulary is Sid's module contract (docs/airlock/INVESTIGATOR_INTEGRATION.md,
docs/airlock/CONTRACTS.md): kinds `usb_added`, `usb_removed`, `app_launched`,
`person_in_zone`, `person_cleared`, `camera_offline`, `camera_online`, `heartbeat`;
sources `mac`, `jetson`, `service` and explicitly simulated `fixture`; metadata keys
`deviceKey`, `appId`, `label`, `detail`, `captureMode`, `bootId`. The older names
below remain only because the fixture scenarios use them. This module is the single
place for these strings; tools never hardcode them elsewhere.
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
_KIND_MAP = {  # the live module's exact kinds win over any source guess
    "usb_added": USB, "usb_removed": USB, "app_launched": APPLICATION,
    "person_in_zone": VISION, "person_cleared": VISION, "camera_offline": VISION, "camera_online": VISION,
    "heartbeat": SYSTEM,
}
_VISION_KIND_PREFIXES = ("person", "face", "owner", "hand", "desk", "presence", "motion", "occupancy", "camera")
_APP_KIND_HINTS = ("app_", "application", "frontmost", "process_")


def categorize(ev: DeviceEventRow) -> str:
    kind_exact = ev.kind.strip().lower()
    if kind_exact in _KIND_MAP:
        return _KIND_MAP[kind_exact]
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
    USB: ("deviceKey", "captureMode", "bootId",
          "vendor_id", "product_id", "serial_hash", "location_id", "device_class",
          "speed", "mass_storage", "is_known", "known_device", "port"),
    VISION: ("captureMode", "bootId",
             "track_id", "zone", "camera_id", "face_match", "face_match_score",
             "identity_label", "duration_ms", "owner_present", "frame_count"),
    APPLICATION: ("appId", "captureMode", "bootId",
                  "bundle_id", "approved", "pid", "signed", "team_id", "launch_type",
                  "parent_bundle_id"),
    SYSTEM: ("captureMode", "bootId", "state", "reason", "component"),
}
# Free-text keys that can be attacker-controlled (USB descriptors, app names,
# window titles). They are passed only inside `untrusted_text` and scanned.
UNTRUSTED_TEXT_KEYS = {
    USB: ("label", "detail", "product_name", "vendor_name", "manufacturer", "serial_display", "name"),
    VISION: ("label", "detail", "description", "note"),
    APPLICATION: ("label", "detail", "app_name", "window_title", "name", "path"),
    SYSTEM: ("label", "detail", "note", "message"),
}
