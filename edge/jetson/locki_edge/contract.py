"""SecurityEventV1 and the pinned contract of Sid's deployed SpacetimeDB module `locki-w91bw`.

VERIFIED on 2026-11-07 with a read-only `spacetime describe --json -s maincloud locki-w91bw`
(snapshot: edge/jetson/contract/locki-w91bw.pinned.json):

    ingest_device_event(workstation_id: String, session_id: String, device_id: String,
                        event_id: String, kind: String, observed_at_ms: U64, seq: U64,
                        confidence: F64, metadata_json: String, simulated: Bool,
                        schema_version: U32) -> Result<(), String>
    report_health(workstation_id: String, device_id: String, component: String,
                  status: String, detail: String) -> Result<(), String>

The SpacetimeDB HTTP API takes a JSON array of POSITIONAL args, so order matters and is
pinned here. The deployed reducer has NO `source` arg: the server derives the stored source
itself. Provenance (Mac dev camera vs Jetson) is therefore carried by enrolling a different
device_id for each, plus the `runtime` / `capture_source` metadata keys.

NOT VERIFIED (Sid owns these, they live in the reducer body we cannot read): the allowed
`kind` values, metadata keys, health component/status strings, the confidence value for
"no calibrated score", and what an empty session_id means. Defaults below are the PRD's
illustrative contract and can be overridden from a JSON file without code changes.

Python 3.6 compatible on purpose (original Jetson Nano = JetPack 4.6 = Python 3.6.9).
"""
import datetime
import json
import math
import re
import uuid
from typing import Any, Dict, NamedTuple, Optional

SCHEMA_VERSION = 1
U32_MAX = 2 ** 32 - 1
U64_MAX = 2 ** 64 - 1

INGEST_REDUCER = "ingest_device_event"
INGEST_PARAMS = (
    ("workstation_id", "String"), ("session_id", "String"), ("device_id", "String"),
    ("event_id", "String"), ("kind", "String"), ("observed_at_ms", "U64"), ("seq", "U64"),
    ("confidence", "F64"), ("metadata_json", "String"), ("simulated", "Bool"),
    ("schema_version", "U32"),
)
HEALTH_REDUCER = "report_health"
HEALTH_PARAMS = (
    ("workstation_id", "String"), ("device_id", "String"), ("component", "String"),
    ("status", "String"), ("detail", "String"),
)

# Local provenance label of the frames behind an event. Never "jetson_usb_camera" off a Jetson.
SOURCES = ("jetson_usb_camera", "mac_dev_camera", "fixture")
RUNTIMES = ("mac_dev", "jetson")

# Which provider capability is REQUIRED before a kind may be emitted. A motion detector has only
# "motion", so it can never produce person or face kinds, whatever it claims (PRD E2).
KIND_CAPABILITY = {
    "person_in_zone": "person",
    "person_left_zone": "person",
    "motion_in_zone": "motion",
    "camera_unavailable": "runtime",
    "owner_face_match": "face",
    "unrecognized_face": "face",
    "face_uncertain": "face",
    "keyboard_interaction": "hand_zone",
    "usb_zone_interaction": "hand_zone",
}
# UNCONFIRMED until Sid confirms: PRD 5.1 kinds. motion_in_zone is PRD E2's "only if Sid accepts it".
DEFAULT_KINDS = tuple(sorted(KIND_CAPABILITY))

# Metadata allowlist: key -> value rule. UNCONFIRMED keys beyond zone_id/observation (PRD 5.1).
DEFAULT_METADATA_RULES = {
    "zone_id": "slug",
    "observation": "slug",
    "provider": "slug",
    "provider_version": "text",
    "runtime": "runtime",
    "capture_source": "source",
    "dwell_ms": "uint",
    "frames_confirmed": "uint",
    "capture_to_event_ms": "number",
    "reason": "slug",
    "zone_overlap": "unit",
    "bbox": "bbox",
}
# Privacy: these never leave the device, even if someone adds them to the allowlist (PRD 5.1, 7).
FORBIDDEN_KEY_TOKENS = frozenset((
    "image", "img", "frame", "jpeg", "jpg", "png", "pixel", "pixels", "b64", "base64", "raw", "embedding",
    "embeddings", "vector", "snapshot", "crop", "biometric", "biometrics", "name", "email", "thumbnail",
    "video", "audio", "landmarks"))


def is_forbidden_key(key):
    """Token match: `frame_jpeg`, `raw_frame`, `face_embedding`, `person_name` are blocked; `frames_confirmed` is not."""
    return any(tok in FORBIDDEN_KEY_TOKENS for tok in re.split(r"[^a-z0-9]+", str(key).lower()) if tok)
MAX_METADATA_JSON_BYTES = 1024
MAX_TEXT = 64
SLUG = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


# report_health, as the deployed module answered on 2026-11-08 for a device enrolled with role "jetson":
#   component "edge"   -> "Component not permitted for device role"
#   status "starting" / "streaming" -> "Invalid health status"   (Dhruv's devices report "online")
# So by default only the camera component is reported, in online/degraded/offline terms. The raw edge
# state still goes in `detail` and on the local /health API. Override both via AIRLOCK_CONTRACT_FILE.
DEFAULT_HEALTH_COMPONENTS = ("camera",)
DEFAULT_HEALTH_STATUS_MAP = {
    "streaming": "online", "ok": "online",
    "starting": "degraded", "stalled": "degraded", "degraded": "degraded",
    "disconnected": "offline", "stopped": "offline", "camera_unavailable": "offline",
}


class ContractError(ValueError):
    """An event that must not be sent. Never retried."""


class SecurityEventV1(NamedTuple):
    schema_version: int
    event_id: str
    workstation_id: str
    session_id: str
    device_id: str
    source: str               # local provenance, see SOURCES (not a reducer arg)
    kind: str
    observed_at: str          # ISO-8601 UTC, millisecond precision, "Z"
    observed_at_ms: int       # same instant, unix epoch ms (what the reducer stores)
    seq: int
    confidence: Optional[float]
    metadata: Dict[str, Any]
    simulated: bool


class Contract(object):
    """The mutable half of the contract (kinds, metadata keys, null-confidence convention)."""

    def __init__(self, kinds=DEFAULT_KINDS, metadata_rules=None, null_confidence=0.0,
                 max_metadata_bytes=MAX_METADATA_JSON_BYTES, health_components=None, health_status_map=None,
                 metadata_strings=True, max_metadata_value_chars=64, wire_metadata_keys=None):
        self.kinds = frozenset(kinds)
        self.metadata_rules = dict(DEFAULT_METADATA_RULES if metadata_rules is None else metadata_rules)
        self.null_confidence = float(null_confidence)
        self.max_metadata_bytes = int(max_metadata_bytes)
        self.health_components = frozenset(DEFAULT_HEALTH_COMPONENTS if health_components is None
                                           else health_components)
        self.health_status_map = dict(DEFAULT_HEALTH_STATUS_MAP if health_status_map is None else health_status_map)
        self.metadata_strings = bool(metadata_strings)
        self.max_metadata_value_chars = int(max_metadata_value_chars)
        # None = send every validated key. A list = only these keys go to the DB; the local audit keeps all.
        self.wire_metadata_keys = None if wire_metadata_keys is None else tuple(wire_metadata_keys)
        for key in self.metadata_rules:
            if is_forbidden_key(key):
                raise ContractError("metadata key %r is privacy-forbidden" % key)

    @classmethod
    def from_file(cls, path):
        with open(path) as f:
            raw = json.load(f)
        return cls(kinds=raw.get("kinds", DEFAULT_KINDS), metadata_rules=raw.get("metadata_rules"),
                   null_confidence=raw.get("null_confidence", 0.0),
                   max_metadata_bytes=raw.get("max_metadata_bytes", MAX_METADATA_JSON_BYTES),
                   health_components=raw.get("health_components"),
                   health_status_map=raw.get("health_status_map"),
                   metadata_strings=raw.get("metadata_strings", True),
                   max_metadata_value_chars=raw.get("max_metadata_value_chars", 64),
                   wire_metadata_keys=raw.get("wire_metadata_keys"))

    def wire_metadata(self, metadata):
        """Metadata as the deployed module accepts it. Live answer (2026-11-08, role "jetson") to numbers and
        arrays: "Metadata must contain only bounded redacted string fields". So every value is sent as a
        short string: bbox [x1,y1,x2,y2] -> "x1,y1,x2,y2", numbers -> plain decimal, bools -> true/false."""
        if self.wire_metadata_keys is not None:
            metadata = {k: v for k, v in metadata.items() if k in self.wire_metadata_keys}
        if not self.metadata_strings:
            return dict(metadata)
        out = {}
        for key, value in metadata.items():
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                text = ",".join("%.3f" % v if isinstance(v, float) else str(v) for v in value)
            elif isinstance(value, float):
                text = ("%.3f" % value).rstrip("0").rstrip(".") if math.isfinite(value) else ""
            else:
                text = str(value)
            out[key] = text[:self.max_metadata_value_chars]
        return out

    def health_status(self, edge_state):
        """Edge state name -> the reducer's status vocabulary (unknown names pass through unchanged)."""
        return self.health_status_map.get(edge_state, edge_state)


def utc_iso_ms(epoch_ms):
    dt = datetime.datetime.fromtimestamp(epoch_ms / 1000.0, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(epoch_ms) % 1000)


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_value(key, rule, value):
    if isinstance(value, bool) and rule not in ("bool",):
        raise ContractError("metadata %s: bool not allowed" % key)
    if rule == "slug":
        ok = isinstance(value, str) and SLUG.match(value)
    elif rule == "text":
        ok = isinstance(value, str) and 0 < len(value) <= MAX_TEXT and "\n" not in value
    elif rule == "runtime":
        ok = value in RUNTIMES
    elif rule == "source":
        ok = value in SOURCES
    elif rule == "uint":
        ok = isinstance(value, int) and 0 <= value <= U32_MAX
    elif rule == "number":
        ok = isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    elif rule == "unit":
        ok = isinstance(value, (int, float)) and math.isfinite(value) and 0.0 <= value <= 1.0
    elif rule == "bbox":
        ok = (isinstance(value, (list, tuple)) and len(value) == 4
              and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0 for v in value)
              and value[0] < value[2] and value[1] < value[3])
    elif rule == "bool":
        ok = isinstance(value, bool)
    else:
        raise ContractError("metadata %s: unknown rule %r" % (key, rule))
    if not ok:
        raise ContractError("metadata %s=%r violates rule %s" % (key, value, rule))


def validate_metadata(metadata, contract):
    if not isinstance(metadata, dict):
        raise ContractError("metadata must be an object")
    for key, value in metadata.items():
        if not isinstance(key, str) or is_forbidden_key(key):
            raise ContractError("metadata key %r is privacy-forbidden" % (key,))
        if key not in contract.metadata_rules:
            raise ContractError("metadata key %r is not in the allowlist" % key)
        _check_value(key, contract.metadata_rules[key], value)
    if len(canonical_json(metadata).encode("utf-8")) > contract.max_metadata_bytes:
        raise ContractError("metadata_json larger than %d bytes" % contract.max_metadata_bytes)


def validate_event(event, contract):
    """Raises ContractError. Same rules on every path, so a bad event can't be retried into the DB."""
    if not isinstance(event, SecurityEventV1):
        raise ContractError("not a SecurityEventV1")
    if event.schema_version != SCHEMA_VERSION:
        raise ContractError("schema_version must be %d" % SCHEMA_VERSION)
    try:
        if str(uuid.UUID(event.event_id)) != event.event_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ContractError("event_id must be a canonical lowercase UUID")
    for name in ("workstation_id", "device_id"):
        value = getattr(event, name)
        if not isinstance(value, str) or not DEVICE_ID.match(value):
            raise ContractError("%s must match %s" % (name, DEVICE_ID.pattern))
    if not isinstance(event.session_id, str) or len(event.session_id) > 128:
        raise ContractError("session_id must be a string (empty allowed)")
    if event.source not in SOURCES:
        raise ContractError("source must be one of %s" % (SOURCES,))
    if event.kind not in contract.kinds:
        raise ContractError("kind %r is not in the contract's allowed kinds" % (event.kind,))
    if event.kind not in KIND_CAPABILITY:
        raise ContractError("kind %r has no evidence rule" % (event.kind,))
    for name, hi in (("observed_at_ms", U64_MAX), ("seq", U64_MAX)):
        value = getattr(event, name)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= hi:
            raise ContractError("%s must be an integer in 1..%d" % (name, hi))
    if event.observed_at != utc_iso_ms(event.observed_at_ms):
        raise ContractError("observed_at must equal observed_at_ms rendered as UTC")
    c = event.confidence
    if c is not None and (isinstance(c, bool) or not isinstance(c, float) or not math.isfinite(c)
                          or not 0.0 <= c <= 1.0):
        raise ContractError("confidence must be null or a float in 0..1")
    if not isinstance(event.simulated, bool):
        raise ContractError("simulated must be a bool")
    if event.source == "fixture" and not event.simulated:
        raise ContractError("fixture events must be simulated=true")
    validate_metadata(event.metadata, contract)
    return True


def make_event(contract, seq, workstation_id, session_id, device_id, source, kind, observed_at_ms,
               confidence, metadata, simulated):
    """Build + validate. `seq` is a callable (SequenceStore.next) so a rejected build burns no seq."""
    md = dict(metadata or {})
    probe = SecurityEventV1(SCHEMA_VERSION, str(uuid.uuid4()), workstation_id, session_id, device_id, source,
                            kind, utc_iso_ms(int(observed_at_ms)), int(observed_at_ms), 1,
                            None if confidence is None else round(float(confidence), 4), md, bool(simulated))
    validate_event(probe, contract)
    event = probe._replace(seq=int(seq()))
    validate_event(event, contract)
    return event


def ingest_args(event, contract):
    """Positional args for the deployed ingest_device_event, in pinned order."""
    validate_event(event, contract)
    confidence = contract.null_confidence if event.confidence is None else event.confidence
    args = [event.workstation_id, event.session_id, event.device_id, event.event_id, event.kind,
            event.observed_at_ms, event.seq, float(confidence), canonical_json(contract.wire_metadata(event.metadata)),
            event.simulated, event.schema_version]
    assert len(args) == len(INGEST_PARAMS)
    return args


def health_args(workstation_id, device_id, component, status, detail):
    for name, value in (("workstation_id", workstation_id), ("device_id", device_id)):
        if not DEVICE_ID.match(value or ""):
            raise ContractError("%s must match %s" % (name, DEVICE_ID.pattern))
    for name, value in (("component", component), ("status", status)):
        if not SLUG.match(value or ""):
            raise ContractError("%s must be a slug" % name)
    if not isinstance(detail, str) or len(detail) > 512:
        raise ContractError("detail too long")
    return [workstation_id, device_id, component, status, detail]


def event_to_dict(event):
    d = event._asdict()
    return dict(d)


def load_pinned(path):
    with open(path) as f:
        return json.load(f)


def check_pinned_signature(pinned):
    """Compare the code's pinned positional order with a describe snapshot. Returns a list of diffs."""
    diffs = []
    for reducer, params in ((INGEST_REDUCER, INGEST_PARAMS), (HEALTH_REDUCER, HEALTH_PARAMS)):
        got = [tuple(p) for p in pinned.get("reducers", {}).get(reducer, [])]
        if got != list(params):
            diffs.append({"reducer": reducer, "code": list(params), "deployed": got})
    return diffs


__all__ = ["SecurityEventV1", "Contract", "ContractError", "make_event", "validate_event", "ingest_args",
           "health_args", "utc_iso_ms", "canonical_json", "check_pinned_signature", "load_pinned",
           "INGEST_PARAMS", "HEALTH_PARAMS", "INGEST_REDUCER", "HEALTH_REDUCER", "KIND_CAPABILITY",
           "SOURCES", "RUNTIMES"]
