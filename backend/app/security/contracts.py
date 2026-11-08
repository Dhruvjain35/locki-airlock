"""The versioned contracts between Dhruv's Mac agent, Areeb's Jetson publisher and Sid's SpacetimeDB module.

JSON Schemas and sample fixtures generated from these models live in docs/airlock/contracts/, so nobody else needs Python to
use them (scripts/airlock_contracts.py writes them; a test keeps them in step).

Times are integer milliseconds since the Unix epoch. Anything with is_fixture=true is sample data for development and must
never be presented as a real observation.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 4096
Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
EpochMs = Annotated[int, Field(ge=0, description="Milliseconds since the Unix epoch")]
# A raw serial, credential or camera frame never travels in an event, whichever device sends it.
_FORBIDDEN_METADATA_WORDS = frozenset("serial password passwd secret token apikey credential credentials image images frame frames jpeg jpg png video base64 face faces biometric".split())
_ALLOWED_METADATA_KEY = frozenset({"has_serial", "frame_rate", "frame_count", "frames_dropped"})  # say something about them, not carry them
_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SecuritySource(StrEnum):
    MAC_USB = "mac_usb"
    MAC_APP = "mac_app"
    MAC_AGENT = "mac_agent"  # the agent's own health and actions
    MAC_GATE = "mac_gate"  # the protected tool gateway for our own managed agent
    MAC_VISION = "mac_vision"  # the AIRLOCK camera pipeline running on the Mac (development: the Mac's own webcam)
    JETSON_VISION = "jetson_vision"
    JETSON_HEALTH = "jetson_health"


class SecurityKind(StrEnum):
    USB_DEVICE_ADDED = "usb_device_added"
    USB_DEVICE_REMOVED = "usb_device_removed"
    APP_LAUNCHED = "app_launched"
    MONITOR_HEALTH = "monitor_health"
    LOCK_STATUS = "lock_status"
    PROTECTED_TOOL_DECISION = "protected_tool_decision"
    PERSON_IN_ZONE = "person_in_zone"
    HAND_NEAR_KEYBOARD = "hand_near_keyboard"
    ZONE_CLEAR = "zone_clear"
    CAMERA_UNAVAILABLE = "camera_unavailable"
    CAMERA_OFFLINE = "camera_offline"
    JETSON_HEALTH = "jetson_health"
    # Camera observations (backend/app/vision/airlock). Each says what the camera saw, never who someone is or why.
    PERSON_IN_VIEW = "person_in_view"
    PERSON_APPROACHING = "person_approaching"
    PERSON_LEFT_ZONE = "person_left_zone"
    OWNER_FACE_MATCH = "owner_face_match"
    UNRECOGNIZED_FACE = "unrecognized_face"
    FACE_UNCERTAIN = "face_uncertain"
    NO_FACE_VISIBLE = "no_face_visible"
    KEYBOARD_INTERACTION = "keyboard_interaction"
    LAPTOP_INTERACTION = "laptop_interaction"
    HAND_NEAR_USB_ZONE = "hand_near_usb_zone"
    USB_ZONE_INTERACTION = "usb_zone_interaction"
    PROTECTED_DEVICE_INTERACTION = "protected_device_interaction"
    CAMERA_OBSTRUCTED = "camera_obstructed"
    CAMERA_RESTORED = "camera_restored"


VISION_SOURCES = frozenset({SecuritySource.MAC_VISION, SecuritySource.JETSON_VISION})
CAMERA_KINDS = frozenset({SecurityKind.CAMERA_UNAVAILABLE, SecurityKind.CAMERA_OFFLINE, SecurityKind.CAMERA_OBSTRUCTED, SecurityKind.CAMERA_RESTORED})
VISION_KINDS = frozenset({
    SecurityKind.PERSON_IN_VIEW, SecurityKind.PERSON_APPROACHING, SecurityKind.PERSON_IN_ZONE, SecurityKind.PERSON_LEFT_ZONE,
    SecurityKind.ZONE_CLEAR, SecurityKind.OWNER_FACE_MATCH, SecurityKind.UNRECOGNIZED_FACE, SecurityKind.FACE_UNCERTAIN,
    SecurityKind.NO_FACE_VISIBLE, SecurityKind.HAND_NEAR_KEYBOARD, SecurityKind.KEYBOARD_INTERACTION,
    SecurityKind.LAPTOP_INTERACTION, SecurityKind.HAND_NEAR_USB_ZONE, SecurityKind.USB_ZONE_INTERACTION,
    SecurityKind.PROTECTED_DEVICE_INTERACTION,
}) | CAMERA_KINDS


class _Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")




UsbCategory = Literal["storage", "input", "audio", "video", "network", "hub", "phone_or_camera", "printer", "smart_card", "wireless", "vendor_specific", "other", "unknown"]


class UsbMetadata(_Metadata):
    """Identifiers the OS reports for a device. An unfamiliar device is unknown, never "malicious"."""

    fingerprint: str = Field(min_length=16, max_length=64, description="Salted hash of vendor, product and serial. The serial itself never leaves the Mac.")
    has_serial: bool
    vendor_id: str = Field(pattern=r"^0x[0-9a-f]{4}$")
    product_id: str = Field(pattern=r"^0x[0-9a-f]{4}$")
    vendor_name: str | None = Field(None, max_length=120)
    product_name: str | None = Field(None, max_length=120)
    device_class: int | None = None
    interface_classes: list[int] = Field(default_factory=list, max_length=32)
    category: UsbCategory
    location_id: str | None = None
    allowlist_status: Literal["baseline", "allowlisted", "unknown", "not_armed"]
    detection: Literal["iokit_notification", "ioreg_poll"]
    armed: bool
    session_id: str | None = None


class AppSignature(_Metadata):
    status: Literal["signed", "adhoc", "unsigned", "unknown"]
    identifier: str | None = Field(None, max_length=160)
    team_id: str | None = Field(None, max_length=40)
    authority: str | None = Field(None, max_length=160)


class AppMetadata(_Metadata):
    """A launch the OS reported. A remote-access tool opening is evidence to look at, not proof of anything."""

    name: str | None = Field(None, max_length=160)
    bundle_id: str | None = Field(None, max_length=200)
    executable_path: str | None = Field(None, max_length=512, description="Home folder shortened to ~")
    pid: int | None = None
    launched_at: EpochMs | None = None
    signature: AppSignature
    watchlist_match: bool
    watchlist_category: str | None = Field(None, max_length=60)
    detection: Literal["nsworkspace_notification", "process_snapshot"]
    armed: bool
    session_id: str | None = None


class MonitorHealthMetadata(_Metadata):
    monitor: Literal["usb", "apps", "publisher", "lock"]
    status: Literal["started", "stopped", "degraded", "recovered"]
    method: str | None = Field(None, max_length=60)
    detail: str | None = Field(None, max_length=240)


VisionZone = Literal["desk", "keyboard_zone", "laptop_zone", "usb_port_zone", "protected_device_zone"]


class VisionMetadata(_Metadata):
    """What the camera pipeline saw. Never who someone is, a picture, a face template or what anyone meant."""

    camera: str = Field(max_length=60, description="A label such as mac_webcam or logitech_usb, never a device serial")
    inference: Literal["mac_mediapipe", "opencv_dnn", "tensorrt", "fixture", "unreported"] = Field(
        description="unreported: a remote camera's row from SpacetimeDB, which does not say which detector ran")
    detector: str | None = Field(None, max_length=80)
    zone: VisionZone | None = None
    started_at: EpochMs | None = Field(None, description="The first frame of this observation")
    dwell_ms: int | None = Field(None, ge=0)
    persons_in_view: int | None = Field(None, ge=0, le=50)
    capture_to_event_ms: float | None = Field(None, ge=0, description="From the frame that settled the observation to this event")
    frame_rate: float | None = Field(None, ge=0)
    detail: str | None = Field(None, max_length=240)
    track_id: int | None = Field(None, ge=0, description="Which tracked face or person, as a number for this session only")
    armed: bool
    session_id: str | None = Field(None, max_length=128)


class LockStatus(StrEnum):
    LOCK_REQUESTED = "LOCK_REQUESTED"
    LOCK_EXECUTED = "LOCK_EXECUTED"
    LOCK_CONFIRMED = "LOCK_CONFIRMED"
    LOCK_REFUSED = "LOCK_REFUSED"
    LOCK_FAILED = "LOCK_FAILED"
    LOCK_DRY_RUN = "LOCK_DRY_RUN"


class LockMetadata(_Metadata):
    action_id: str = Field(max_length=128)
    session_id: str | None = Field(None, max_length=128)
    incident_id: str | None = Field(None, max_length=128)
    status: LockStatus
    mechanism: str | None = Field(None, max_length=60)
    detail: str | None = Field(None, max_length=240)
    dry_run: bool


class ToolDecisionMetadata(_Metadata):
    run_id: str = Field(max_length=128)
    tool_name: str = Field(max_length=80)
    decision: Literal["allowed", "denied"]
    reason: str | None = Field(None, max_length=240)
    callback_ran: bool


_TYPED_METADATA: dict[SecurityKind, type[_Metadata]] = {
    SecurityKind.USB_DEVICE_ADDED: UsbMetadata,
    SecurityKind.USB_DEVICE_REMOVED: UsbMetadata,
    SecurityKind.APP_LAUNCHED: AppMetadata,
    SecurityKind.MONITOR_HEALTH: MonitorHealthMetadata,
    SecurityKind.LOCK_STATUS: LockMetadata,
    SecurityKind.PROTECTED_TOOL_DECISION: ToolDecisionMetadata,
}
_SOURCES_FOR: dict[SecurityKind, frozenset[SecuritySource]] = {
    SecurityKind.USB_DEVICE_ADDED: frozenset({SecuritySource.MAC_USB}),
    SecurityKind.USB_DEVICE_REMOVED: frozenset({SecuritySource.MAC_USB}),
    SecurityKind.APP_LAUNCHED: frozenset({SecuritySource.MAC_APP}),
    SecurityKind.MONITOR_HEALTH: frozenset({SecuritySource.MAC_AGENT}),
    SecurityKind.LOCK_STATUS: frozenset({SecuritySource.MAC_AGENT}),
    SecurityKind.PROTECTED_TOOL_DECISION: frozenset({SecuritySource.MAC_GATE}),
    SecurityKind.PERSON_IN_ZONE: frozenset({SecuritySource.JETSON_VISION}),
    SecurityKind.HAND_NEAR_KEYBOARD: frozenset({SecuritySource.JETSON_VISION}),
    SecurityKind.ZONE_CLEAR: frozenset({SecuritySource.JETSON_VISION}),
    SecurityKind.JETSON_HEALTH: frozenset({SecuritySource.JETSON_HEALTH}),
}
_SOURCES_FOR.update({kind: VISION_SOURCES for kind in VISION_KINDS - CAMERA_KINDS})
_SOURCES_FOR.update({kind: VISION_SOURCES | {SecuritySource.JETSON_HEALTH} for kind in CAMERA_KINDS})


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecurityEventV1(_Contract):
    """One observation, Mac or Jetson to SpacetimeDB. It says what was observed, never what it means."""

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: Id
    device_id: Id
    source: SecuritySource
    kind: SecurityKind
    observed_at: EpochMs
    seq: int = Field(ge=0, description="Monotonic per device_id, across restarts")
    confidence: float | None = Field(None, ge=0.0, le=1.0, description="For detectors such as vision. None for facts the OS reports.")
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_fixture: bool = Field(False, description="Sample data for development. Never treat it as a real observation.")

    @field_validator("metadata")
    @classmethod
    def _bounded_and_clean(cls, value: dict[str, Any]) -> dict[str, Any]:
        def check(node: Any) -> None:
            if isinstance(node, dict):
                for key, item in node.items():
                    if not isinstance(key, str) or not _METADATA_KEY.match(key):
                        raise ValueError(f"metadata key {str(key)[:40]!r} is not snake_case")
                    if key not in _ALLOWED_METADATA_KEY and (_FORBIDDEN_METADATA_WORDS & set(key.split("_")) or "api_key" in key):
                        raise ValueError(f"metadata key {key!r} could carry a secret or a camera frame")
                    check(item)
            elif isinstance(node, list):
                for item in node:
                    check(item)
            elif not (node is None or isinstance(node, (str, int, float, bool))):
                raise ValueError(f"metadata holds a {type(node).__name__}: only JSON values")

        check(value)
        if len(json.dumps(value)) > MAX_METADATA_BYTES:
            raise ValueError(f"metadata is over {MAX_METADATA_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _kind_matches(self) -> SecurityEventV1:
        if self.source not in _SOURCES_FOR[self.kind]:
            raise ValueError(f"{self.source.value} cannot report {self.kind.value}")
        typed = _TYPED_METADATA.get(self.kind)
        if typed is None and self.kind in VISION_KINDS and self.source in VISION_SOURCES:
            typed = VisionMetadata  # the Jetson's own health publisher (jetson_health) stays free-form for now
        if typed is not None:
            typed.model_validate(self.metadata)
        return self


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentState(StrEnum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    OBSERVING = "OBSERVING"
    SUSPECTED_INTRUSION = "SUSPECTED_INTRUSION"
    LOCK_REQUESTED = "LOCK_REQUESTED"
    SCREEN_LOCKED = "SCREEN_LOCKED"
    REVIEWING = "REVIEWING"
    RESOLVED = "RESOLVED"
    DEGRADED = "DEGRADED"


class IncidentUpdateV1(_Contract):
    """SpacetimeDB to voice, Nemotron and the consoles. SpacetimeDB decides these; nothing on the Mac does."""

    schema_version: Literal[1] = SCHEMA_VERSION
    incident_id: Id
    session_id: Id | None = None
    severity: Severity
    state: IncidentState
    evidence_ids: list[Id] = Field(default_factory=list, max_length=200)
    action_required: Literal["none", "acknowledge", "review", "lock_requested", "owner_return"] | None = None
    updated_at: EpochMs | None = None
    digital_event_id: Id | None = Field(None, description="The digital event the correlation rule used, if any")
    physical_event_id: Id | None = Field(None, description="The physical (camera) event the correlation rule used, if any")
    summary: str | None = Field(None, max_length=240)
    rule_id: str | None = Field(None, max_length=80)
    is_fixture: bool = False
    created_at: EpochMs | None = Field(None, description="When SpacetimeDB opened the incident")
    acknowledged_by: str | None = Field(None, max_length=80, description="The identity that acknowledged it in SpacetimeDB (the owner or an authorized reviewer)")


class LockRequestV1(_Contract):
    """SpacetimeDB to the Mac: lock the screen, once. Carries no command of any kind; the Mac still checks the owner's opt-in."""

    schema_version: Literal[1] = SCHEMA_VERSION
    session_id: Id
    action_id: Id
    expiry: EpochMs
    reason: str = Field(min_length=1, max_length=240)
    owner_policy_hash: str = Field(min_length=8, max_length=128)
    incident_id: Id | None = None
    is_fixture: bool = False


class AgentStepStatus(StrEnum):
    REQUESTED = "requested"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentStepV1(_Contract):
    """One step of a real agent run, Nemotron and Relay to SpacetimeDB and the dashboard."""

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: Id
    step: int = Field(ge=0)
    tool_name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    status: AgentStepStatus
    evidence_id: Id | None = None
    duration_ms: float | None = Field(None, ge=0)
    model_id: str | None = Field(None, max_length=120, description="The model that asked for this step, exactly as the runtime reports it.")
    incident_id: Id | None = None
    trace_id: Id | None = Field(None, description="Correlates with the Relay trace when there is one.")
    trace_source: Literal["nemo_relay", "locki_log"] = Field("locki_log", description="Whose record this is. Our own log is never labelled as a Relay span.")
    detail: str | None = Field(None, max_length=240)
    input_type: str | None = Field(None, max_length=80, description="Names of the arguments, e.g. event_id")
    args_json: str | None = Field(None, max_length=600, description="The arguments, shortened: long text never goes in")
    is_fixture: bool = False


CONTRACTS: dict[str, type[_Contract]] = {
    "SecurityEventV1": SecurityEventV1,
    "IncidentUpdateV1": IncidentUpdateV1,
    "LockRequestV1": LockRequestV1,
    "AgentStepV1": AgentStepV1,
}
