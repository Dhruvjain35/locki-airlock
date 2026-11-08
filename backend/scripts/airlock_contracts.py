"""Writes the JSON Schemas and sample fixtures for the AIRLOCK contracts to docs/airlock/contracts/, for teammates who are not using
Python. Run it after changing app/security/contracts.py; tests/test_security_contracts.py fails if the files are out of step.

    .venv/bin/python scripts/airlock_contracts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.security.contracts import CONTRACTS  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "airlock" / "contracts"
T = 1794100000000  # sample time: these are fixtures, not observations
USB = {
    "fingerprint": "6f1d2c3b4a5968778695a4b3c2d1e0f0", "has_serial": True, "vendor_id": "0x0781", "product_id": "0x5581", "vendor_name": "Example Vendor", "product_name": "Example USB Stick",
    "device_class": 0, "interface_classes": [8], "category": "storage", "location_id": "0x00130000", "allowlist_status": "unknown", "detection": "iokit_notification", "armed": True, "session_id": "fixture-session-1",
}
FIXTURES = {
    "security_event_usb_unknown.json": ("SecurityEventV1", {"event_id": "fixture-usb-1", "device_id": "fixture-mac", "source": "mac_usb", "kind": "usb_device_added", "observed_at": T, "seq": 11, "metadata": USB, "is_fixture": True}),
    "security_event_usb_baseline.json": ("SecurityEventV1", {"event_id": "fixture-usb-2", "device_id": "fixture-mac", "source": "mac_usb", "kind": "usb_device_added", "observed_at": T + 500, "seq": 12, "metadata": {**USB, "allowlist_status": "baseline"}, "is_fixture": True}),
    "security_event_app_watchlist.json": ("SecurityEventV1", {
        "event_id": "fixture-app-1", "device_id": "fixture-mac", "source": "mac_app", "kind": "app_launched", "observed_at": T + 1000, "seq": 13, "is_fixture": True,
        "metadata": {"name": "Example Remote Tool", "bundle_id": "com.example.remote", "executable_path": "/Applications/Example.app/Contents/MacOS/Example", "pid": 4242, "launched_at": T + 950,
                     "signature": {"status": "signed", "identifier": "com.example.remote", "team_id": "EXAMPLE123", "authority": "Developer ID Application: Example"}, "watchlist_match": True,
                     "watchlist_category": "remote_access", "detection": "nsworkspace_notification", "armed": True, "session_id": "fixture-session-1"},
    }),
    "security_event_lock_status.json": ("SecurityEventV1", {"event_id": "fixture-lock-1", "device_id": "fixture-mac", "source": "mac_agent", "kind": "lock_status", "observed_at": T + 1500, "seq": 14, "is_fixture": True,
                                                            "metadata": {"action_id": "fixture-action-1", "session_id": "fixture-session-1", "incident_id": "fixture-incident-1", "status": "LOCK_DRY_RUN", "mechanism": "none", "detail": "dry run", "dry_run": True}}),
    "security_event_person_in_zone.json": ("SecurityEventV1", {"event_id": "fixture-jetson-1", "device_id": "fixture-jetson", "source": "jetson_vision", "kind": "person_in_zone", "observed_at": T - 2000, "seq": 51, "confidence": 0.82, "metadata": {"camera": "logitech_usb", "inference": "fixture", "detector": "example-person-detector", "zone": "desk", "dwell_ms": 600, "persons_in_view": 1, "armed": True}, "is_fixture": True}),
    "security_event_camera_unavailable.json": ("SecurityEventV1", {"event_id": "fixture-jetson-2", "device_id": "fixture-jetson", "source": "jetson_health", "kind": "camera_unavailable", "observed_at": T - 1000, "seq": 52, "metadata": {"detail": "no data from the camera for 3 s"}, "is_fixture": True}),
    "incident_update.json": ("IncidentUpdateV1", {"incident_id": "fixture-incident-1", "session_id": "fixture-session-1", "severity": "high", "state": "SUSPECTED_INTRUSION", "evidence_ids": ["fixture-usb-1", "fixture-jetson-1"], "action_required": "review", "updated_at": T + 200, "is_fixture": True}),
    "lock_request.json": ("LockRequestV1", {"session_id": "fixture-session-1", "action_id": "fixture-action-1", "expiry": T + 30000, "reason": "Unknown USB device while armed", "owner_policy_hash": "0123456789abcdef0123456789abcdef", "incident_id": "fixture-incident-1", "is_fixture": True}),
    "agent_step.json": ("AgentStepV1", {"run_id": "fixture-run-1", "step": 1, "tool_name": "inspect_usb_event", "status": "succeeded", "evidence_id": "fixture-usb-1", "duration_ms": 3.2, "model_id": "fixture-model", "incident_id": "fixture-incident-1", "trace_id": "fixture-trace-1", "trace_source": "locki_log", "is_fixture": True}),
}


def expected_files() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for name, model in CONTRACTS.items():
        files[OUT / f"{name}.schema.json"] = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
    for filename, (name, body) in FIXTURES.items():
        files[OUT / "fixtures" / filename] = json.dumps(CONTRACTS[name].model_validate(body).model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    return files


def main() -> int:
    for path, text in expected_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print("wrote", path.relative_to(OUT.parents[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
