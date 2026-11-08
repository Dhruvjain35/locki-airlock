"""The Mac's connection to the team's SpacetimeDB module (locki-w91bw, Sid's): its reducers over the documented HTTP API, and its per-identity views over
the WebSocket JSON protocol. SpacetimeDB has no official Python SDK, so this speaks the same server API the SDKs use. SpacetimeDB owns arming, incidents
and lock requests; this carries messages to and from it and decides none of them.

Before sending anything it checks the live module's reducer signatures against the ones below, so a change on Sid's side stops sending with a clear
reason rather than writing wrong data. A reducer that refuses has decided, and that is final. Only a real 2xx counts as delivered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from .contracts import AgentStepV1, IncidentUpdateV1, LockRequestV1, SecurityEventV1, SecurityKind, SecuritySource
from .publisher import Delivery

log = logging.getLogger(__name__)

RECONNECT_MIN_S, RECONNECT_MAX_S = 1.0, 15.0
PROTOCOL = "v1.json.spacetimedb"
# Sid's reducers as the live module declares them (GET /v1/database/locki-w91bw/schema?version=9, 2026-11-07), in argument order.
EXPECTED_REDUCERS: dict[str, list[tuple[str, str]]] = {
    "ingest_device_event": [("workstationId", "String"), ("sessionId", "String"), ("deviceId", "String"), ("eventId", "String"), ("kind", "String"), ("observedAtMs", "U64"), ("seq", "U64"), ("confidence", "F64"), ("metadataJson", "String"), ("simulated", "Bool"), ("schemaVersion", "U32")],
    "record_agent_step": [("workstationId", "String"), ("runId", "String"), ("incidentId", "String"), ("stepId", "String"), ("step", "U32"), ("toolName", "String"), ("status", "String"), ("evidenceId", "String"), ("durationMs", "U64"), ("inputType", "String"), ("argsJson", "String"), ("resultSummary", "String"), ("traceId", "String"), ("model", "String")],
    "record_baseline": [("workstationId", "String"), ("deviceId", "String"), ("observedAtMs", "U64"), ("baselineJson", "String"), ("activeAppId", "String")],
    "report_health": [("workstationId", "String"), ("deviceId", "String"), ("component", "String"), ("status", "String"), ("detail", "String")],
    "report_lock_result": [("actionId", "String"), ("status", "String"), ("detail", "String")],
}
_AUTH_REFUSAL = re.compile(r"not an enrolled|not enrolled|not a member|not authori[sz]ed|unauthori[sz]ed|forbidden|permission|not the owner|not allowed|identity", re.I)
_DONE_LOCK = {"executed", "confirmed", "failed", "refused", "expired", "cancelled", "canceled", "done", "completed", "dry_run"}
_SESSION_ENDED = {"DISARMED", "EXPIRED", "ENDED", "RESOLVED", "CANCELLED"}
BASELINE_FORMAT = "locki-usb-v1"
MAX_BASELINE_IDS = 256  # the module refuses more: "Expected at most 256 bounded string identifiers"
# What Sid's module accepts from a "mac" device, learned from its own replies: these components, and "online" (not "started" or "ok").
HEALTH_COMPONENTS = {"usb": "usb", "apps": "apps"}
# The event kinds a "mac" device may publish, and the only metadata each accepts (string values only), learned from the module's replies to calls
# that named a session that does not exist, so nothing was written while learning. Everything else about an event stays in the local record.
EVENT_KINDS = {"usb_device_added": "usb_added", "usb_device_removed": "usb_removed", "app_launched": "app_launched"}
HEALTH_STATUS = {"started": "online", "recovered": "online", "stopped": "offline", "degraded": "degraded"}
# The desk camera enrolls with role "jetson" in Sid's module (spacetime/src/index.ts, feature/airlock-sid), which takes
# person_in_zone, person_cleared, camera_offline and camera_online from it, and correlates an unknown USB device with a
# person_in_zone within 10 s. Face states and zone interactions have no kind there yet, so they stay in the local record.
# "camera_kinds" in data/airlock_vision/spacetime.json overrides this without a code change.
CAMERA_EVENT_KINDS = {"person_in_zone": "person_in_zone", "person_left_zone": "person_cleared", "zone_clear": "person_cleared",
                      "camera_offline": "camera_offline", "camera_unavailable": "camera_offline", "camera_obstructed": "camera_offline", "camera_restored": "camera_online"}


class SpacetimeConfig(BaseModel):
    url: str
    database: str
    workstation_id: str
    device_id: str
    token: str | None = None
    timeout_s: float = 5.0
    incidents_view: str = "my_incidents"
    lock_view: str = "my_lock_requests"
    sessions_view: str = "my_sessions"
    device_events_view: str = "my_device_events"
    arm_grants_view: str = "my_arm_authorizations"  # owner-issued one-use voice ARM grants for this Mac (Sid's live delegation)
    remote_cameras: tuple[str, ...] = ()  # the only other devices whose camera rows this Mac acts on; none unless the owner lists them
    camera_kinds: dict[str, str] | None = None  # our camera kind -> the module's, for the camera device


class BaselineReport(BaseModel):
    """What is connected now, so the owner can arm with it. Fingerprints are salted hashes; no serial leaves the Mac."""

    observed_at: int
    usb: dict[str, int]
    active_app_id: str | None = None

    def baseline_json(self) -> str:
        """A JSON list of fingerprints, one entry per device, so two identical devices appear twice. That is the shape the module accepts."""
        ids = [fp for fp, count in sorted(self.usb.items()) for _ in range(max(0, count))]
        return json.dumps(ids[:MAX_BASELINE_IDS])


class HealthBeat(BaseModel):
    """A monitor saying it is still working. The module marks a component offline after 15 s without one."""

    component: str = Field(min_length=1, max_length=40)
    status: str = Field(min_length=1, max_length=20)
    detail: str = Field("", max_length=200)


class LockResult(BaseModel):
    action_id: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=40)
    detail: str = Field("", max_length=240)


Message = SecurityEventV1 | AgentStepV1 | BaselineReport | HealthBeat | LockResult


def reducer_call(message: Message, config: SpacetimeConfig) -> tuple[str, list]:
    """Which of Sid's reducers a message goes to, with its arguments in his order."""
    ws, device = config.workstation_id, config.device_id
    if isinstance(message, SecurityEventV1):
        meta = message.metadata
        if message.kind == SecurityKind.MONITOR_HEALTH:
            component, status = HEALTH_COMPONENTS.get(str(meta.get("monitor"))), HEALTH_STATUS.get(str(meta.get("status")))
            if component is None or status is None:
                return "", []  # not something the module takes from this device: it stays in the local record
            return "report_health", [ws, device, component, status, str(meta.get("detail") or meta.get("method") or "")[:200]]
        if message.source.value in ("mac_vision", "jetson_vision"):
            kind = (config.camera_kinds or CAMERA_EVENT_KINDS).get(message.kind.value)
            if kind is None:
                return "", []
            # Sid's module takes only label, detail, captureMode and bootId, as strings: no picture, face or name can be in here.
            # captureMode names the real capture path (Sid: "a fallback webcam must identify itself accurately"): camera and inference.
            shared = {"label": str(meta.get("zone") or meta.get("camera") or "camera")[:60],
                      "captureMode": f"{meta.get('camera') or 'camera'}:{meta.get('inference') or 'unknown'}"[:60]}
            if meta.get("detail"):
                shared["detail"] = str(meta["detail"])[:200]
            return "ingest_device_event", [ws, str(meta.get("session_id") or ""), message.device_id, message.event_id, kind, message.observed_at, message.seq,
                                           1.0 if message.confidence is None else float(message.confidence), json.dumps(shared), message.is_fixture, message.schema_version]
        kind = EVENT_KINDS.get(message.kind.value)
        if kind is None:
            return "", []  # no kind the module takes from this device: it stays in the local record
        shared = {"deviceKey": str(meta.get("fingerprint") or "")} if kind.startswith("usb_") else {"appId": str(meta.get("bundle_id") or meta.get("name") or "unknown")[:200]}
        # An OS-reported fact has no detector confidence: 1.0 says "observed".
        return "ingest_device_event", [ws, str(meta.get("session_id") or ""), message.device_id, message.event_id, kind, message.observed_at, message.seq,
                                       1.0 if message.confidence is None else float(message.confidence), json.dumps(shared), message.is_fixture, message.schema_version]
    if isinstance(message, AgentStepV1):
        return "record_agent_step", [ws, message.run_id, message.incident_id or "", f"{message.run_id}:{message.step}", message.step, message.tool_name, message.status.value, message.evidence_id or "",
                                     int(round(message.duration_ms or 0)), message.input_type or "none", message.args_json or "{}", message.detail or "", message.trace_id or "", message.model_id or ""]
    if isinstance(message, BaselineReport):
        return "record_baseline", [ws, device, message.observed_at, message.baseline_json(), message.active_app_id or "unknown"]  # the module asks for "unknown" when there is none
    if isinstance(message, HealthBeat):
        return "report_health", [ws, device, message.component, message.status, message.detail]
    return "report_lock_result", [message.action_id, message.status, message.detail]


def contract_problems(schema: dict) -> list[str]:
    """Differences between the live module's reducers and the ones this adapter calls."""
    live = {r.get("name"): [((p.get("name") or {}).get("some"), next(iter(p.get("algebraic_type") or {"?": 0}))) for p in (r.get("params") or {}).get("elements", [])] for r in schema.get("reducers", [])}
    problems = []
    for name, params in EXPECTED_REDUCERS.items():
        if name not in live:
            problems.append(f"{name} is missing")
        elif live[name] != params:
            problems.append(f"{name} takes {live[name]}, not {params}")
    return problems


def load_or_create_token(config_url: str, directory: Path, timeout_s: float = 5.0) -> str:
    """This Mac agent's own identity on the server, kept in an owner-only file in the git-ignored data folder. Never logged or printed."""
    path = directory / f"spacetime-{urlsplit(config_url).hostname}.json"
    try:
        return json.loads(path.read_text())["token"]
    except (OSError, ValueError, KeyError):
        pass
    response = httpx.post(f"{config_url}/v1/identity", timeout=timeout_s)
    response.raise_for_status()
    body = response.json()
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"identity": body.get("identity"), "token": body["token"]}, f)
    return body["token"]


def agent_identity(config_url: str, directory: Path) -> str | None:
    """The agent's public identity, to enroll it. Not a secret."""
    try:
        return json.loads((directory / f"spacetime-{urlsplit(config_url).hostname}.json").read_text()).get("identity")
    except (OSError, ValueError):
        return None


class SpacetimeTransport:
    name = "spacetimedb"

    def __init__(self, config: SpacetimeConfig, client: httpx.Client | None = None):
        self.config = config
        self._client = client or httpx.Client(timeout=config.timeout_s)
        self.connected = False
        self.authorized: bool | None = None  # False once the module has said this identity may not do something
        self.contract: list[str] | None = None  # None until checked
        self.last_refusal: str | None = None
        self.last_ok_at: float | None = None

    def _headers(self, content_type: str = "application/json") -> dict:
        headers = {"Content-Type": content_type}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        return headers

    def check(self) -> list[str]:
        """Compare the live module with this adapter. Any difference stops sending until it is resolved with Sid."""
        try:
            response = self._client.get(f"{self.config.url}/v1/database/{self.config.database}/schema", params={"version": 9}, headers=self._headers())
            response.raise_for_status()
            self.contract = contract_problems(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            self.contract = [f"could not read the module's schema: {type(exc).__name__}: {exc}"[:200]]
        return self.contract

    def send(self, message: Message) -> Delivery:
        if self.contract is None:
            self.check()
        if self.contract:
            return Delivery("refused", self.name, "the database's reducers no longer match this adapter: " + "; ".join(self.contract)[:200])
        reducer, args = reducer_call(message, self.config)
        if not reducer:
            return Delivery("local_only", self.name, "kept in the local record: the module takes no such message from this device")
        return self.call_reducer(reducer, args)

    def call_reducer(self, reducer: str, args: list) -> Delivery:
        """One reducer call with this Mac's own identity, judged like every message: delivered, refused (final) or failed (retry later)."""
        try:
            response = self._client.post(f"{self.config.url}/v1/database/{self.config.database}/call/{reducer}", headers=self._headers(), content=json.dumps(args))
        except httpx.HTTPError as exc:
            self.connected = False
            return Delivery("failed", self.name, f"{type(exc).__name__}: {exc}"[:200])
        code, text = response.status_code, response.text[:200]
        if 200 <= code < 300:
            self.connected, self.last_ok_at = True, time.time()
            if self.authorized is None:
                self.authorized = True
            return Delivery("delivered", self.name, f"{reducer}: {code}")
        if code in (401, 403) or (code == 530 and _AUTH_REFUSAL.search(text)):
            self.connected, self.authorized, self.last_refusal = True, False, f"{reducer}: {text}"
            return Delivery("refused", self.name, f"{reducer}: not authorized: {text}")
        if code == 530 or 400 <= code < 500:  # the module refused this message, or the request can never be accepted
            self.connected, self.last_refusal = code == 530 or self.connected, f"{reducer}: {text}"
            return Delivery("refused", self.name, f"{reducer}: {code} {text}")
        self.connected = False
        return Delivery("failed", self.name, f"{reducer}: {code} {text}")

    def close(self) -> None:
        self._client.close()


# ------------------------------------------------------------------ rows from Sid's views
def columns_from_schema(schema: dict) -> tuple[list[str], list[bool]]:
    """Column names, and which columns are options, from an SQL result's schema."""
    names, options = [], []
    for element in schema.get("elements", []):
        names.append((element.get("name") or {}).get("some") or "")
        variants = ((element.get("algebraic_type") or {}).get("Sum") or {}).get("variants") or []
        options.append(sorted((v.get("name") or {}).get("some") or "" for v in variants) == ["none", "some"])
    return names, options


def decode_row(row: str | list | dict, names: list[str], options: list[bool]) -> dict:
    """As the real server sends them: rows in the first subscription reply are JSON objects, rows in later updates are positional arrays, and an option is [0, value] or [1, []]."""
    values = json.loads(row) if isinstance(row, str) else row
    pairs = values if isinstance(values, dict) else dict(zip(names, values, strict=False))
    optional = {name for name, is_option in zip(names, options, strict=False) if is_option}
    return {name: ((value[1] if value[0] == 0 else None) if name in optional and isinstance(value, list) and len(value) == 2 else value) for name, value in pairs.items()}


def _ids(text: object) -> list[str]:
    try:
        values = json.loads(text) if isinstance(text, str) and text else []
    except ValueError:
        return []
    return [str(v) for v in values if isinstance(v, str | int)][:200] if isinstance(values, list) else []


# Sid's incident states, as his module writes them ("open" on the first real incident), and what they mean in the contract.
INCIDENT_STATES = {"open": "SUSPECTED_INTRUSION", "suspected_intrusion": "SUSPECTED_INTRUSION", "lock_requested": "LOCK_REQUESTED", "locked": "SCREEN_LOCKED", "screen_locked": "SCREEN_LOCKED",
                   "acknowledged": "REVIEWING", "reviewing": "REVIEWING", "resolved": "RESOLVED", "closed": "RESOLVED", "degraded": "DEGRADED", "observing": "OBSERVING"}


def incident_from_row(row: dict) -> IncidentUpdateV1:
    state = str(row.get("state", ""))
    return IncidentUpdateV1(
        incident_id=row["id"], session_id=row.get("session_id") or None, severity=str(row.get("severity", "")).lower(), state=INCIDENT_STATES.get(state.lower(), state.upper()),
        evidence_ids=_ids(row.get("event_ids_json")), action_required="review" if row.get("action_required") else "none", updated_at=row.get("updated_at_ms") or None,
        digital_event_id=row.get("digital_event_id") or None, physical_event_id=row.get("physical_event_id") or None, summary=(row.get("summary") or "")[:240] or None, rule_id=(row.get("rule_id") or "")[:80] or None,
        is_fixture=bool(row.get("simulated")),
    )


def lock_request_from_row(row: dict) -> LockRequestV1:
    return LockRequestV1(session_id=row["session_id"], action_id=row["id"], expiry=int(row["expires_at_ms"]), reason=(row.get("reason") or "requested by the security system")[:240], owner_policy_hash=row.get("owner_policy_hash") or "", incident_id=row.get("incident_id") or None, is_fixture=bool(row.get("simulated")))


@dataclass(frozen=True)
class RemoteSession:
    session_id: str
    state: str
    armed_at_ms: int
    expires_at_ms: int
    disarmed_at_ms: int
    baseline_json: str
    approved_apps_json: str
    auto_lock_enabled: bool
    owner_policy_hash: str
    simulated: bool

    def active(self, now_ms: int | None = None) -> bool:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return not self.disarmed_at_ms and self.state.upper() not in _SESSION_ENDED and self.expires_at_ms > now

    def baseline(self) -> dict[str, int] | None:
        """The USB baseline the owner armed with: a list of this Mac's fingerprints. None if it cannot be read, and then every device counts as new."""
        try:
            data = json.loads(self.baseline_json or "")
        except ValueError:
            return None
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            counts: dict[str, int] = {}
            for fp in data:
                counts[fp] = counts.get(fp, 0) + 1
            return counts
        if isinstance(data, dict) and data.get("format") == BASELINE_FORMAT and isinstance(data.get("usb"), dict):
            return {str(k): int(v) for k, v in data["usb"].items() if isinstance(v, int)}
        return None

    def approved_apps(self) -> list[str]:
        try:
            data = json.loads(self.approved_apps_json or "[]")
        except ValueError:
            return []
        return [str(a) for a in data if isinstance(a, str)][:100] if isinstance(data, list) else []


def session_from_row(row: dict) -> RemoteSession:
    return RemoteSession(str(row["id"]), str(row.get("state", "")), int(row.get("armed_at_ms") or 0), int(row.get("expires_at_ms") or 0), int(row.get("disarmed_at_ms") or 0), str(row.get("baseline_json") or ""),
                         str(row.get("approved_apps_json") or ""), bool(row.get("auto_lock_enabled")), str(row.get("owner_policy_hash") or ""), bool(row.get("simulated")))


# A camera enrolled at this workstation (Areeb's jetson-01) publishes these to the module. A person alone is a soft observation there and
# opens no incident, so the Mac follows my_device_events itself: these four kinds, from another device, never simulated, become local
# camera observations. They drive LOCKI's mode and spoken warning exactly as the Mac's own camera does, and are never published again.
REMOTE_CAMERA_KINDS = {"person_in_zone": "person_in_zone", "person_cleared": "person_left_zone", "camera_offline": "camera_offline", "camera_online": "camera_restored"}


def remote_camera_event(row: dict, own_device_id: str, allowed: tuple[str, ...] | frozenset[str]) -> tuple[SecurityEventV1, str, int] | None:
    """A camera row from my_device_events as (event, session id, the server's received_at_ms), or None unless it is a real, non-simulated
    observation published as role jetson by a device the owner listed in remote_cameras. Every other camera enrolled at the workstation
    (Areeb's mac-dev-cam-01, wherever it points) is ignored."""
    kind = REMOTE_CAMERA_KINDS.get(str(row.get("kind") or ""))
    device = str(row.get("device_id") or "")
    if kind is None or not device or device == own_device_id or device not in allowed or row.get("simulated") or str(row.get("source") or "") != "jetson":
        return None
    try:
        meta = json.loads(row.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    label = str(meta.get("label") or "")
    capture = str(meta.get("captureMode") or "")
    session = str(row.get("session_id") or "")
    metadata = {"camera": (capture.split(":")[0] or device)[:60], "inference": "unreported", "armed": bool(session), "session_id": session[:128] or None,
                "zone": label if label in ("desk", "keyboard_zone", "laptop_zone", "usb_port_zone", "protected_device_zone") else ("desk" if kind == "person_in_zone" else None),
                "detail": (f"{device}: {meta.get('detail') or capture or 'remote camera'}")[:240]}
    confidence = row.get("confidence")
    event = SecurityEventV1(event_id=str(row["id"]), device_id=device, source=SecuritySource.JETSON_VISION, kind=SecurityKind(kind), observed_at=int(row["observed_at_ms"]),
                            seq=int(row.get("seq") or 0), confidence=float(confidence) if isinstance(confidence, (int, float)) and 0 <= confidence <= 1 else None,
                            metadata={k: v for k, v in metadata.items() if v is not None})
    return event, session, int(row.get("received_at_ms") or 0)


class SpacetimeSubscriber:
    """Follows this workstation's sessions, incidents and lock requests through Sid's views. If the server goes away it keeps trying, and local warnings
    carry on regardless. A row that does not fit the contract, or is for another workstation or device, is counted and never acted on."""

    def __init__(self, config: SpacetimeConfig, *, on_incident: Callable[[IncidentUpdateV1], None], on_lock_request: Callable[[LockRequestV1], object], on_session: Callable[[RemoteSession], None],
                 on_camera_event: Callable[[SecurityEventV1, str, int], None] | None = None, on_arm_grant: Callable[[object], None] | None = None):
        self.config = config
        self.on_incident, self.on_lock_request, self.on_session, self.on_camera_event = on_incident, on_lock_request, on_session, on_camera_event
        self.on_arm_grant = on_arm_grant
        self.camera_rows = 0
        self.connected = False
        self.connections = 0
        self.reconnects = 0
        self.rows_received = 0
        self.rows_rejected = 0
        self.rows_ignored = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._columns: dict[str, tuple[list[str], list[bool]]] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="spacetime-subscription")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.connected = False

    def _views(self) -> list[str]:
        views = [self.config.sessions_view, self.config.incidents_view, self.config.lock_view]
        if self.on_camera_event is not None and self.config.remote_cameras:
            views = views + [self.config.device_events_view]
        return views + [self.config.arm_grants_view] if self.on_arm_grant is not None else views

    def _ws_url(self) -> str:
        parts = urlsplit(self.config.url)
        return f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}/v1/database/{self.config.database}/subscribe"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.token}"} if self.config.token else {}

    async def _load_columns(self) -> None:
        async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
            for view in self._views():
                response = await client.post(f"{self.config.url}/v1/database/{self.config.database}/sql", headers={**self._headers(), "Content-Type": "text/plain"}, content=f"SELECT * FROM {view}")
                response.raise_for_status()
                self._columns[view] = columns_from_schema(response.json()[0]["schema"])

    async def _run(self) -> None:
        import websockets

        delay = RECONNECT_MIN_S
        while True:
            try:
                await self._load_columns()
                async with websockets.connect(self._ws_url(), subprotocols=[PROTOCOL], additional_headers=self._headers(), open_timeout=self.config.timeout_s) as ws:
                    await ws.send(json.dumps({"Subscribe": {"query_strings": [f"SELECT * FROM {v}" for v in self._views()], "request_id": 1}}))
                    self.connected, self.last_error, delay = True, None, RECONNECT_MIN_S
                    self.connections += 1
                    async for raw in ws:
                        await self._message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.debug("SpacetimeDB subscription: %s", self.last_error)
            self.connected = False
            self.reconnects += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _message(self, raw: str | bytes) -> None:
        message = json.loads(raw)
        if "InitialSubscription" in message:
            tables = message["InitialSubscription"]["database_update"]["tables"]
        elif "TransactionUpdateLight" in message:
            tables = message["TransactionUpdateLight"]["update"]["tables"]
        elif "TransactionUpdate" in message:
            tables = ((message["TransactionUpdate"].get("status") or {}).get("Committed") or {}).get("tables") or []
        else:
            return
        for table in tables:
            name = table.get("table_name")
            if name not in self._columns:
                continue
            names, options = self._columns[name]
            for update in table.get("updates", []):
                for row in update.get("inserts", []):
                    await self._row(name, decode_row(row, names, options))

    async def _row(self, view: str, row: dict) -> None:
        self.rows_received += 1
        if row.get("workstation_id") not in (None, self.config.workstation_id):
            self.rows_ignored += 1
            return
        camera_event = None
        try:
            if view == self.config.sessions_view:
                self.on_session(session_from_row(row))
            elif view == self.config.incidents_view:
                self.on_incident(incident_from_row(row))
            elif view == self.config.arm_grants_view:
                from .arm_delegation import grant_from_row

                grant = grant_from_row(row)
                if self.on_arm_grant is not None:
                    self.on_arm_grant(grant)
            elif view == self.config.device_events_view:
                found = remote_camera_event(row, self.config.device_id, self.config.remote_cameras) if self.on_camera_event is not None else None
                if found is None:
                    self.rows_ignored += 1
                    return
                self.camera_rows += 1
                camera_event = found
            else:
                if (row.get("target_device_id") or "") not in ("", self.config.device_id) or str(row.get("status") or "").lower() in _DONE_LOCK:
                    self.rows_ignored += 1
                    return
                result = self.on_lock_request(lock_request_from_row(row))
                if asyncio.iscoroutine(result):
                    await result
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            self.rows_rejected += 1
            self.last_error = f"a {view} row did not fit the contract: {str(exc)[:120]}"
            log.warning(self.last_error)
            return
        if camera_event is not None:
            try:  # called outside the parse: a bug in the service is not a malformed row
                self.on_camera_event(*camera_event)
            except Exception as exc:
                self.last_error = f"the camera handler failed: {type(exc).__name__}: {str(exc)[:120]}"
                log.exception("remote camera row")

    def snapshot(self) -> dict:
        return {"connected": self.connected, "connections": self.connections, "reconnects": self.reconnects, "rows_received": self.rows_received, "rows_rejected": self.rows_rejected, "rows_ignored": self.rows_ignored,
                "camera_rows": self.camera_rows, "last_error": self.last_error, "url": self.config.url, "database": self.config.database, "workstation_id": self.config.workstation_id, "device_id": self.config.device_id}
