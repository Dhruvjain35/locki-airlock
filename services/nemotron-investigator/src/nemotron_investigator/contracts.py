"""Row contracts mirroring Sid's SpacetimeDB module `locki-w91bw` (Maincloud).

Field names and types were copied from the live module schema
(`spacetime describe --json -s maincloud locki-w91bw`, fetched 2026-11-07).
The investigator only ever READS these rows, through the identity-scoped
`my_*` views. All underlying tables are private.

Validation is strict (pydantic strict mode). A retyped column is contract drift,
not something to coerce: a string "123" or a float in an integer column, 0/1 in
a boolean column, a number in a string column, a negative value in an unsigned
(u64/u32) column, or a null or non-finite `confidence` all fail. SpacetimeDB
serializes a NaN/inf f64 as null, so such a row fails here too. Unknown extra
columns are ignored, so a compatible schema addition does not break the service.

A failure applies to that ROW only. The evidence adapters validate rows one at a
time. List reads drop the invalid row and report it through
`pop_rejected_rows()`. Single-row reads (`get_incident`, `get_session`) raise
EvidenceContractError. Either way it is drift to coordinate with Sid, never
something to paper over.

Identity columns (`armed_by`, `publisher`, `identity`) are normalized to one
canonical form: lowercase, 64 hex characters, zero-padded, no `0x` prefix. The
live SQL endpoint sends an Identity as a wrapped U256 such as ["0xc200..."],
possibly with leading zeros dropped. Fixtures and the investigator's token file
use bare 64-character hex. Both normalize to the same string, so identities
compare equal across backends.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

U64 = Annotated[int, Field(ge=0, le=2**64 - 1)]
U32 = Annotated[int, Field(ge=0, le=2**32 - 1)]

_HEX_DIGITS = frozenset("0123456789abcdef")
_IDENTITY_WRAPPER_KEYS = ("__identity__", "identity", "hex")


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


def _identity_to_hex(v: Any) -> str:
    """Normalize a SpacetimeDB Identity to its canonical form.

    The canonical form is lowercase, 64 hex characters, zero-padded, without
    `0x`. Accepted inputs:
      * a hex string, with or without `0x`, in any case, leading zeros optional
      * a wrapped product such as ["0x..."] or {"__identity__": "0x..."}
      * the U256 as an integer
    Anything else raises ValueError, so the row fails contract validation.
    """
    for _ in range(4):  # bounded unwrapping of single-element wrappers
        if isinstance(v, dict):
            key = next((k for k in _IDENTITY_WRAPPER_KEYS if k in v), None)
            if key is None:
                break
            v = v[key]
        elif isinstance(v, (list, tuple)) and len(v) == 1:
            v = v[0]
        else:
            break
    if isinstance(v, str):
        s = v.strip().lower()
        if s.startswith("0x"):
            s = s[2:]
        if not s or len(s) > 130 or any(c not in _HEX_DIGITS for c in s):
            raise ValueError("unrecognized identity encoding: not a hex identity")
        s = s.lstrip("0")
        if len(s) > 64:
            raise ValueError("identity exceeds 256 bits")
        return s.rjust(64, "0")
    if isinstance(v, int) and not isinstance(v, bool):
        if not 0 <= v < 1 << 256:
            raise ValueError("identity out of the U256 range")
        return f"{v:064x}"
    raise ValueError(f"unrecognized identity encoding: {type(v).__name__}")


normalize_identity = _identity_to_hex


def try_normalize_identity(v: Any) -> str | None:
    """Canonical identity hex, or None when `v` is not a recognizable identity."""
    try:
        return _identity_to_hex(v)
    except ValueError:
        return None


class IncidentRow(_Row):
    scope: ClassVar[str] = "full"   # a my_incidents row (membership-scoped view)
    id: str
    workstation_id: str
    session_id: str
    severity: str
    state: str
    rule_id: str
    summary: str
    event_ids_json: str
    digital_event_id: str
    physical_event_id: str
    created_at_ms: U64
    updated_at_ms: U64
    acknowledged_by: str
    resolved_by: str
    resolution_note: str
    action_required: bool
    simulated: bool

    def linked_event_ids(self) -> list[str]:
        ids: list[str] = []
        try:
            parsed = json.loads(self.event_ids_json) if self.event_ids_json else []
            if isinstance(parsed, list):
                ids.extend(str(x) for x in parsed if isinstance(x, (str, int)))
        except (ValueError, TypeError):
            pass
        for extra in (self.digital_event_id, self.physical_event_id):
            if extra and extra not in ids:
                ids.append(extra)
        return ids


class SessionRow(_Row):
    id: str
    workstation_id: str
    state: str
    armed_by: str
    armed_at_ms: U64
    expires_at_ms: U64
    disarmed_at_ms: U64
    baseline_json: str
    approved_apps_json: str
    auto_lock_enabled: bool
    owner_policy_hash: str
    simulated: bool

    _ident = field_validator("armed_by", mode="before")(_identity_to_hex)


class DeviceEventRow(_Row):
    id: str
    workstation_id: str
    session_id: str
    device_id: str
    source: str
    kind: str
    observed_at_ms: U64   # device clock (reducer argument): display only, never windows
    received_at_ms: U64   # server clock (set by the module): windows select on this
    seq: U64
    confidence: float = Field(allow_inf_nan=False)
    metadata_json: str
    simulated: bool
    schema_version: U32
    classification: str

    def metadata(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.metadata_json) if self.metadata_json else {}
        except (ValueError, TypeError):
            return {"_unparseable_metadata": True}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}


class BaselineSnapshotRow(_Row):
    id: str
    workstation_id: str
    device_id: str
    publisher: str
    observed_at_ms: U64   # device clock
    received_at_ms: U64   # server clock: the baseline window selects on this
    baseline_json: str
    active_app_id: str
    session_id: str
    camera_status: str
    health_json: str

    _ident = field_validator("publisher", mode="before")(_identity_to_hex)


class DeviceHealthRow(_Row):
    id: str
    workstation_id: str
    device_id: str
    component: str
    status: str
    last_seen_ms: U64
    detail: str


class DeviceRow(_Row):
    id: str
    workstation_id: str
    identity: str
    role: str
    label: str
    enabled: bool
    last_seq: U64
    last_event_at_ms: U64

    _ident = field_validator("identity", mode="before")(_identity_to_hex)


class InvestigatorIncidentRow(_Row):
    """A `my_investigator_incidents` row: the incident as the owner approved it for the
    investigator (Sid's `grant_incident_evidence`). The projection deliberately omits the
    rule, the mutable summary, actor identities, notes and action flags; those read as
    "not in scope" (empty / None), never as a value.
    """

    scope: ClassVar[str] = "owner_approved_evidence"
    id: str
    workstation_id: str
    session_id: str
    severity: str
    state: str
    created_at_ms: U64
    updated_at_ms: U64
    simulated: bool
    approved_event_ids_json: str
    evidence_approved_at_ms: U64
    evidence_complete: bool

    # Fields of the full incident that are outside the approved scope.
    rule_id: ClassVar[str] = ""
    summary: ClassVar[str] = ""
    digital_event_id: ClassVar[str] = ""
    physical_event_id: ClassVar[str] = ""
    acknowledged_by: ClassVar[str] = ""
    resolved_by: ClassVar[str] = ""
    resolution_note: ClassVar[str] = ""
    action_required: ClassVar[None] = None

    def linked_event_ids(self) -> list[str]:
        try:
            parsed = json.loads(self.approved_event_ids_json) if self.approved_event_ids_json else []
        except (ValueError, TypeError):
            return []
        return [str(x) for x in parsed if isinstance(x, (str, int))] if isinstance(parsed, list) else []


class InvestigatorBaselineRow(_Row):
    """A `my_investigator_baselines` row: the frozen `session:<sessionId>` baseline only.
    `baseline_json` is a canonical JSON array of redacted USB device keys. Publisher,
    camera status and health are not in the projection (they read as unknown)."""

    id: str
    workstation_id: str
    device_id: str
    session_id: str
    observed_at_ms: U64
    received_at_ms: U64
    baseline_json: str
    active_app_id: str

    publisher: ClassVar[str] = ""
    camera_status: ClassVar[str] = ""
    health_json: ClassVar[str] = "{}"


# View names in the live module (identity-scoped). The investigator reads Sid's
# owner-granted evidence projection by default (VIEWS_INVESTIGATOR); the broad
# membership views stay empty for an evidence-only principal.
VIEW_INV_INCIDENTS = "my_investigator_incidents"
VIEW_INV_EVENTS = "my_investigator_events"
VIEW_INV_BASELINES = "my_investigator_baselines"
VIEW_INCIDENTS = "my_incidents"
VIEW_SESSIONS = "my_sessions"
VIEW_DEVICE_EVENTS = "my_device_events"
VIEW_BASELINE_SNAPSHOTS = "my_baseline_snapshots"
VIEW_DEVICE_HEALTH = "my_device_health"
VIEW_DEVICES = "my_devices"
VIEW_MEMBERSHIPS = "my_memberships"
