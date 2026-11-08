"""The contracts teammates build against: valid samples pass, anything else is refused, and the published schemas match the code."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.security.contracts import CONTRACTS, AgentStepV1, IncidentUpdateV1, LockRequestV1, SecurityEventV1

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import airlock_contracts  # noqa: E402

FIXTURES = airlock_contracts.OUT / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_the_published_schemas_and_fixtures_match_the_code():
    for path, text in airlock_contracts.expected_files().items():
        assert path.read_text() == text, f"{path.name} is out of date: run scripts/airlock_contracts.py"


@pytest.mark.parametrize("filename, contract", [(f, n) for f, (n, _) in airlock_contracts.FIXTURES.items()])
def test_every_fixture_validates_and_says_it_is_a_fixture(filename, contract):
    body = fixture(filename)
    assert body["is_fixture"] is True
    CONTRACTS[contract].model_validate(body)


def test_the_fields_the_prd_names_are_required_or_present():
    required = set(SecurityEventV1.model_json_schema()["required"])
    assert required >= {"event_id", "device_id", "source", "kind", "observed_at", "seq"}
    assert {"schema_version", "confidence", "metadata"} <= set(SecurityEventV1.model_fields)
    assert {"incident_id", "severity", "state", "evidence_ids", "action_required"} <= set(IncidentUpdateV1.model_fields)
    assert {"session_id", "action_id", "expiry", "reason", "owner_policy_hash"} <= set(LockRequestV1.model_fields)
    assert {"run_id", "step", "tool_name", "status", "evidence_id", "duration_ms"} <= set(AgentStepV1.model_fields)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"event_id": ""},
        {"event_id": "has spaces"},
        {"seq": -1},
        {"observed_at": -5},
        {"confidence": 1.5},
        {"unexpected": "field"},
        {"kind": "made_up_kind"},
        {"source": "made_up_source"},
        {"source": "jetson_vision"},  # a Jetson cannot report a USB arrival
        {"kind": "person_in_zone"},  # nor can the Mac's USB monitor report a person
    ],
)
def test_a_bad_event_is_refused(change):
    with pytest.raises(ValidationError):
        SecurityEventV1.model_validate({**fixture("security_event_usb_unknown.json"), **change})


@pytest.mark.parametrize(
    "metadata_change",
    [{"serial_number": "ABC"}, {"vendor_id": "1234"}, {"category": "malware"}, {"allowlist_status": "malicious"}, {"fingerprint": "x"}, {"extra": 1}],
)
def test_usb_metadata_is_checked_field_by_field(metadata_change):
    body = fixture("security_event_usb_unknown.json")
    body["metadata"] = {**body["metadata"], **metadata_change}
    with pytest.raises(ValidationError):
        SecurityEventV1.model_validate(body)


@pytest.mark.parametrize("key", ["usb_serial", "api_key", "password", "session_token", "frame", "jpeg_bytes", "image", "video_url", "face_id", "biometric_hash"])
def test_no_event_can_carry_a_secret_or_a_camera_frame(key):
    body = fixture("security_event_person_in_zone.json")
    body["metadata"] = {"zone": "desk", key: "x"}
    with pytest.raises(ValidationError):
        SecurityEventV1.model_validate(body)


def test_metadata_is_bounded_and_plain_json():
    body = fixture("security_event_person_in_zone.json")
    with pytest.raises(ValidationError):
        SecurityEventV1.model_validate({**body, "metadata": {"note": "x" * 5000}})
    with pytest.raises(ValidationError):
        SecurityEventV1.model_validate({**body, "metadata": {"BadKey": 1}})
    health = fixture("security_event_camera_unavailable.json")  # the Jetson's own health publisher is still free-form
    SecurityEventV1.model_validate({**health, "metadata": {"nested": {"ok": [1, "two", None]}}})
    with pytest.raises(ValidationError):  # camera observations are typed: no free-form fields
        SecurityEventV1.model_validate({**body, "metadata": {"nested": {"ok": [1, "two", None]}}})


def test_contracts_cannot_be_changed_after_creation():
    event = SecurityEventV1.model_validate(fixture("security_event_usb_unknown.json"))
    with pytest.raises(ValidationError):
        event.seq = 99


def test_an_agent_step_says_whose_record_it_is_and_defaults_to_our_own_log():
    step = AgentStepV1(run_id="r1", step=1, tool_name="inspect_usb_event", status="succeeded")
    assert step.trace_source == "locki_log"
    with pytest.raises(ValidationError):
        AgentStepV1(run_id="r1", step=1, tool_name="inspect_usb_event", status="succeeded", trace_source="made_up")
    with pytest.raises(ValidationError):
        AgentStepV1(run_id="r1", step=1, tool_name="rm -rf /", status="succeeded")


def test_a_lock_request_carries_no_command():
    assert set(LockRequestV1.model_fields) == {"schema_version", "session_id", "action_id", "expiry", "reason", "owner_policy_hash", "incident_id", "is_fixture"}
    with pytest.raises(ValidationError):
        LockRequestV1.model_validate({**fixture("lock_request.json"), "command": "shutdown -h now"})


def test_camera_observations_are_typed_and_never_carry_identity_or_pictures():
    body = fixture("security_event_person_in_zone.json")
    ok = SecurityEventV1.model_validate({**body, "source": "mac_vision"})
    assert ok.metadata["zone"] == "desk"
    for bad in ({"face_embedding": [0.1]}, {"image_jpeg": "..."}, {"name": "Dhruv"}, {"zone": "the_kitchen"}):
        with pytest.raises(ValidationError):
            SecurityEventV1.model_validate({**body, "metadata": {**body["metadata"], **bad}})
    with pytest.raises(ValidationError):  # the Mac's USB monitor cannot report what a camera saw
        SecurityEventV1.model_validate({**body, "source": "mac_usb"})
    for kind in ("owner_face_match", "unrecognized_face", "face_uncertain", "no_face_visible", "usb_zone_interaction", "camera_obstructed"):
        SecurityEventV1.model_validate({**body, "source": "mac_vision", "kind": kind})
