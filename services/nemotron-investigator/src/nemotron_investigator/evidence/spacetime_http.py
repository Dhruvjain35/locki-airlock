"""LIVE read-only adapter for Sid's SpacetimeDB module (`locki-w91bw`, Maincloud).

Authorization model (no owner privileges, no permission changes):
  * The investigator uses its OWN SpacetimeDB identity (token file, mode 0600),
    never the module owner's CLI token.
  * Production investigations use only owner-approved `my_investigator_*`
    evidence views, with no enabled workstation membership of any role.
  * It never calls a reducer. There is no code path here that writes.

Query safety: SQL statements are fixed templates. The only interpolated values
are ids validated against a strict character set (base.SAFE_ID) and integers
produced by our own code, so model-generated text can never reach the SQL
endpoint. Every workstation-bound read filters on workstation_id in the SQL, and
filters again on the rows that come back.

Bounds:
  * Window reads carry a LIMIT clause.
  * The baseline read has a 24 h lower bound.
  * Each response is streamed under a 2 MB cap.
  * Each query has a total deadline.
  * JSON decoding and row validation run off the event loop.

Error mapping:
  * EvidenceUnavailable: transport errors, HTTP 401/403 and 5xx.
  * EvidenceContractError: HTTP 400/404, carrying the bounded server message.
    SpacetimeDB answers 400 for an unknown or private table and for SQL it
    cannot parse, i.e. schema drift.
  * EvidenceContractError: an oversized or malformed response.

VERIFY LIVE: LIMIT is assumed to be in the SpacetimeDB SQL subset for one-off
queries. It could not be tested against Maincloud from here. If the server
rejects it, every window read fails with HTTP 400 → contract_error. In that case
set SQL_LIMIT_CLAUSES = False. The same row caps are then enforced client-side,
after the response (still 2 MB bounded) arrives.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import BaseModel

from ..contracts import (
    VIEW_BASELINE_SNAPSHOTS,
    VIEW_DEVICE_EVENTS,
    VIEW_DEVICE_HEALTH,
    VIEW_INCIDENTS,
    VIEW_INV_BASELINES,
    VIEW_INV_EVENTS,
    VIEW_INV_INCIDENTS,
    VIEW_MEMBERSHIPS,
    VIEW_SESSIONS,
    BaselineSnapshotRow,
    DeviceEventRow,
    DeviceHealthRow,
    IncidentRow,
    InvestigatorBaselineRow,
    InvestigatorIncidentRow,
    SessionRow,
    try_normalize_identity,
)
from .base import (
    BASELINE_LOOKBACK_MS,
    BASELINE_ROW_LIMIT,
    REASON_OUT_OF_SCOPE,
    SAFE_ID,
    U64_MAX,
    WINDOW_ROW_LIMIT,
    EvidenceContractError,
    EvidenceUnavailable,
    Provenance,
    RejectedRows,
    cap_rows,
    check_id,
    narrowed_baseline_rows,
    plan_id_lookup,
    raw_in_window,
    row_limit_reason,
    split_valid,
    validate_one,
    window_bounds,
)

SQL_LIMIT_CLAUSES = True              # see VERIFY LIVE in the module docstring
INCIDENT_LIST_LIMIT = 200             # operator listing only (list_recent_incidents)
# Which views the adapter reads. "investigator" (default) is Sid's minimum authorization:
# the owner grants one incident's evidence with grant_incident_evidence and the identity reads
# only my_investigator_incidents/_events/_baselines, holding no membership at all.
# "membership" reads the broad my_* views of a workstation member (older deployments, tests).
VIEW_MODES = ("investigator", "membership")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # streamed; a larger response is a contract error
EXPECTED_INVESTIGATOR_IDENTITY = "c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883"
MAX_TOKEN_FILE_BYTES = 65536
_ERROR_BODY_BYTES = 4096              # how much of an error response is read at all
_SERVER_MESSAGE_CHARS = 200           # how much of it is carried in the exception

_SAFE_ID = SAFE_ID  # backward-compatible alias


def lit(value: str) -> str:
    """Single-quoted SQL literal for an id.

    Raises UnsupportedIdentifier (an EvidenceContractError, also a ValueError)
    for anything outside SAFE_ID.
    """
    return f"'{check_id(value)}'"


def num(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= U64_MAX:
        raise ValueError("numeric bound failed validation")
    return str(value)


def _limit(n: int) -> str:
    return f" LIMIT {num(n)}" if SQL_LIMIT_CLAUSES else ""


def _name(el: object) -> str:
    if not isinstance(el, dict):
        return ""
    n = el.get("name")
    if isinstance(n, dict):
        n = n.get("some")
    return n if isinstance(n, str) else ""


def decode_sql_response(payload) -> list[dict]:
    """Convert the HTTP SQL response (list of {schema, rows}) into row dicts."""
    if not isinstance(payload, list) or len(payload) != 1:
        raise EvidenceContractError("unexpected SQL response shape")
    out: list[dict] = []
    for stmt in payload:
        if not isinstance(stmt, dict):
            raise EvidenceContractError("unexpected SQL statement shape")
        schema = stmt.get("schema")
        elements = schema.get("elements", []) if isinstance(schema, dict) else None
        rows = stmt.get("rows")
        if not isinstance(elements, list) or not isinstance(rows, list):
            raise EvidenceContractError("unexpected SQL statement shape")
        cols = [_name(e) for e in elements]
        if not cols or any(not c for c in cols) or len(set(cols)) != len(cols):
            raise EvidenceContractError("invalid SQL column names")
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            elif isinstance(row, list) and len(row) == len(cols):
                out.append(dict(zip(cols, row)))
            else:
                raise EvidenceContractError("row/column count mismatch in SQL response")
    return out


def _parse_body(body: bytes) -> list[dict]:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as e:
        raise EvidenceContractError("SpacetimeDB returned non-JSON SQL response") from e
    return decode_sql_response(payload)


def _server_message(body: bytes) -> str:
    """Printable, single-line, bounded excerpt of a server error body."""
    text = body.decode("utf-8", errors="replace")
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    if len(text) > _SERVER_MESSAGE_CHARS:
        text = text[: _SERVER_MESSAGE_CHARS - 3] + "..."
    return text or "(no message)"


def _oversize_error() -> EvidenceContractError:
    return EvidenceContractError(f"SpacetimeDB response exceeded the {MAX_RESPONSE_BYTES // (1024 * 1024)} MB cap")


async def _read_capped(resp: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read at most `cap` decoded bytes: (body, whether more was on offer)."""
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        room = cap - len(buf)
        if len(chunk) > room:
            buf += chunk[:room]
            return bytes(buf), True
        buf += chunk
    return bytes(buf), False


def _window_scope(workstation_id: str, lo: int, hi: int) -> Callable[[dict], bool]:
    return lambda r: r.get("workstation_id") == workstation_id and raw_in_window(r.get("received_at_ms"), lo, hi)


def _over_privileged(m: dict) -> bool:
    return bool(m.get("can_approve")) or str(m.get("role", "")).lower() in ("owner", "admin")


def attribute_memberships(members: list, me: str | None) -> tuple[list[dict], list[dict], str]:
    """Split my_memberships rows into (counted, judged, label).

    `counted` holds the investigator's own enabled memberships; they make up the
    reported count and workstations. `judged` holds the rows the least-privilege
    check examines. Rows whose identity normalizes to a different identity
    belong to other members and are ignored. That only happens once at least one
    row matches the investigator, which proves the encodings agree. If rows
    carry identities but none match, or the investigator identity is unknown,
    every row is judged, so an encoding mismatch fails closed rather than open.
    A row without a recognizable identity is assumed to be the investigator's
    own: the view is identity-scoped.
    """
    enabled = [m for m in members if isinstance(m, dict) and m.get("enabled", True)]
    if not enabled:
        return [], [], "no_memberships"
    own, other, unattributed = [], [], []
    for m in enabled:
        rid = try_normalize_identity(m.get("identity")) if "identity" in m else None
        if rid is None:
            unattributed.append(m)
        elif me is not None and rid == me:
            own.append(m)
        else:
            other.append(m)
    if me is None:
        return enabled, enabled, "investigator_identity_unknown"
    if other and not own:
        return unattributed, enabled, "no_row_matched_investigator_identity"
    mine = own + unattributed
    return mine, mine, "matched" if own else "rows_without_identity"


class SpacetimeHttpEvidenceSource:
    def __init__(self, host: str, database: str, token: str, *, identity: str | None = None,
                 timeout_s: float = 5.0, transport: httpx.AsyncBaseTransport | None = None,
                 views: str = "investigator", expected_identity: str = EXPECTED_INVESTIGATOR_IDENTITY):
        if views not in VIEW_MODES:
            raise ValueError(f"views must be one of {VIEW_MODES}")
        self.host = host.rstrip("/")
        self.database = database
        self._token = token
        self.timeout_s = timeout_s
        self._transport = transport
        self._rejects = RejectedRows()
        self.views = views
        if not isinstance(expected_identity, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_identity):
            raise ValueError("expected investigator identity must be 64 hex characters")
        self.expected_identity = expected_identity.lower()
        inv = views == "investigator"
        self._v_incidents, self._incident_row = ((VIEW_INV_INCIDENTS, InvestigatorIncidentRow) if inv
                                                 else (VIEW_INCIDENTS, IncidentRow))
        self._v_events = VIEW_INV_EVENTS if inv else VIEW_DEVICE_EVENTS
        self._v_baselines, self._baseline_row = ((VIEW_INV_BASELINES, InvestigatorBaselineRow) if inv
                                                 else (VIEW_BASELINE_SNAPSHOTS, BaselineSnapshotRow))
        canonical = try_normalize_identity(identity) if identity else None
        self.provenance = Provenance(
            backend="spacetimedb", live=True,
            label=(f"SpacetimeDB {database} (owner-granted incident evidence, my_investigator_* views)" if inv
                   else f"SpacetimeDB {database} (identity-scoped my_* views)"),
            detail={"host": self.host, "database": database, "identity": canonical or "unknown",
                    "scope": "incident_evidence_grant" if inv else "workstation_membership"})

    @classmethod
    def from_token_file(cls, host: str, database: str, token_file: str | Path, **kw):
        p = Path(token_file)
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                info = os.fstat(f.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > MAX_TOKEN_FILE_BYTES):
                    raise EvidenceUnavailable("investigator token must be a private operator-owned regular file")
                data = f.read(MAX_TOKEN_FILE_BYTES + 1)
            if len(data) > MAX_TOKEN_FILE_BYTES:
                raise EvidenceUnavailable("investigator token file exceeds size limit")
            raw = data.decode("utf-8").strip()
            identity = None
            if raw.startswith("{"):
                d = json.loads(raw)
                raw, identity = d["token"], d.get("identity")
            if not isinstance(raw, str) or not raw or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
                raise EvidenceUnavailable("invalid investigator token file")
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as e:
            raise EvidenceUnavailable("cannot read a valid private investigator token file") from e
        return cls(host, database, raw, identity=identity, **kw)

    # ------------------------------------------------------------- transport
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=min(3.0, self.timeout_s)),
                                 transport=self._transport, follow_redirects=False, trust_env=False)

    async def assert_investigator_authorized(self) -> None:
        """Fresh authenticated principal check. Call before inference and final evidence read.

        File identity metadata is never authority. This gate authenticates the
        fixed investigator using the server header and requires zero enabled
        memberships, including reviewers without action approval permission.
        """
        if self.views != "investigator":
            raise EvidenceContractError("live investigations require incident-evidence views")
        async with self._client() as client:
            try:
                async with asyncio.timeout(self.timeout_s):
                    async with client.stream("GET", f"{self.host}/v1/database/{self.database}/schema?version=9",
                                             headers={"Authorization": f"Bearer {self._token}"}) as response:
                        if response.status_code != 200:
                            raise EvidenceUnavailable("investigator identity authentication failed")
                        authenticated = response.headers.get("spacetime-identity", "")
                        if not re.fullmatch(r"(?:0x)?[a-fA-F0-9]{64}", authenticated):
                            raise EvidenceUnavailable("investigator identity authentication failed")
                        if authenticated.removeprefix("0x").lower() != self.expected_identity:
                            raise EvidenceUnavailable("authenticated investigator identity mismatch")
                        body, oversized = await _read_capped(response, MAX_RESPONSE_BYTES)
                        if oversized:
                            raise _oversize_error()
                        try:
                            schema = json.loads(body)
                        except (ValueError, RecursionError) as e:
                            raise EvidenceContractError("invalid investigator schema response") from e
                        if not isinstance(schema, dict) or not isinstance(schema.get("misc_exports"), list):
                            raise EvidenceContractError("invalid investigator schema response")
                        views = {entry["View"].get("name") for entry in schema["misc_exports"]
                                 if isinstance(entry, dict) and isinstance(entry.get("View"), dict)
                                 and entry["View"].get("is_public") is True
                                 and entry["View"].get("is_anonymous") is False}
                        if not {VIEW_INV_INCIDENTS, VIEW_INV_EVENTS, VIEW_INV_BASELINES} <= views:
                            raise EvidenceContractError("investigator evidence views unavailable")
                members = await self._sql(f"SELECT * FROM {VIEW_MEMBERSHIPS}", client=client)
            except (httpx.HTTPError, TimeoutError) as e:
                raise EvidenceUnavailable("investigator authorization check unavailable") from e
        if any(m.get("enabled") is not False for m in members):
            raise EvidenceUnavailable("investigator must have no enabled memberships")
        self.provenance.detail["identity"] = self.expected_identity

    async def _post(self, client: httpx.AsyncClient, query: str) -> tuple[int, bytes]:
        async with client.stream("POST", f"{self.host}/v1/database/{self.database}/sql", content=query,
                                 headers={"Authorization": f"Bearer {self._token}",
                                          "Content-Type": "text/plain"}) as resp:
            if resp.status_code != 200:
                body, _ = await _read_capped(resp, _ERROR_BODY_BYTES)
                return resp.status_code, body
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise _oversize_error()
            body, over = await _read_capped(resp, MAX_RESPONSE_BYTES)
            if over:
                raise _oversize_error()
            return 200, body

    async def _sql(self, query: str, *, client: httpx.AsyncClient | None = None) -> list[dict]:
        if client is None:
            async with self._client() as own:
                return await self._sql(query, client=own)
        try:
            async with asyncio.timeout(self.timeout_s):
                status, body = await self._post(client, query)
        except httpx.HTTPError as e:
            raise EvidenceUnavailable(f"SpacetimeDB unreachable: {type(e).__name__}") from e
        except TimeoutError as e:
            raise EvidenceUnavailable(f"SpacetimeDB query exceeded its {self.timeout_s:g}s deadline") from e
        if status in (401, 403):
            raise EvidenceUnavailable(f"SpacetimeDB refused the read (HTTP {status}); "
                                      "investigator identity is not authorized")
        if status >= 500:
            raise EvidenceUnavailable(f"SpacetimeDB server error (HTTP {status})")
        if status in (400, 404):
            raise EvidenceContractError(f"SpacetimeDB rejected the query (HTTP {status}): {_server_message(body)}")
        if status != 200:
            raise EvidenceUnavailable(f"SpacetimeDB rejected the query (HTTP {status}): {_server_message(body)}")
        return await asyncio.to_thread(_parse_body, body)

    # --------------------------------------------------------------- rows
    def _drop_out_of_scope(self, view: str, raw: list[dict], scope: Callable[[dict], bool],
                           workstation_id: str | None) -> list[dict]:
        kept = [r for r in raw if scope(r)]
        if len(kept) < len(raw):
            self._rejects.add(view, None, f"{REASON_OUT_OF_SCOPE}: {len(raw) - len(kept)} row(s)", workstation_id)
        return kept

    async def _accept(self, view: str, model: type[BaseModel], raw: list[dict], workstation_id: str,
                      scope: Callable[[dict], bool]) -> list:
        """Valid in-scope rows only. Each invalid row is recorded for pop_rejected_rows()."""
        raw = self._drop_out_of_scope(view, raw, scope, workstation_id)
        if not raw:
            return []
        valid, rejected = await asyncio.to_thread(split_valid, model, raw)
        for rid, reason in rejected:
            self._rejects.add(view, rid, reason, workstation_id)
        return valid

    async def _rows(self, view: str, model: type[BaseModel], query: str, *, workstation_id: str,
                    limit: int, scope: Callable[[dict], bool]) -> list:
        raw, truncated = cap_rows(await self._sql(query), limit)
        if truncated:
            self._rejects.add(view, None, row_limit_reason(limit), workstation_id)
        return await self._accept(view, model, raw, workstation_id, scope)

    def _single(self, view: str, model: type[BaseModel], raw: list[dict], scope: Callable[[dict], bool],
                workstation_id: str | None = None):
        kept = self._drop_out_of_scope(view, raw, scope, workstation_id)
        return validate_one(model, kept[0], view) if kept else None

    # -------------------------------------------------------------- reads
    async def get_incident(self, incident_id: str) -> IncidentRow | InvestigatorIncidentRow | None:
        rows = await self._sql(f"SELECT * FROM {self._v_incidents} WHERE id = {lit(incident_id)}")
        return self._single(self._v_incidents, self._incident_row, rows, lambda r: r.get("id") == incident_id)

    async def get_session(self, workstation_id: str, session_id: str) -> SessionRow | None:
        if not workstation_id or not session_id or self.views == "investigator":
            return None  # sessions are outside an incident evidence grant (my_sessions is empty for it)
        rows = await self._sql(f"SELECT * FROM {VIEW_SESSIONS} WHERE id = {lit(session_id)} "
                               f"AND workstation_id = {lit(workstation_id)}")
        return self._single(VIEW_SESSIONS, SessionRow, rows,
                            lambda r: r.get("id") == session_id and r.get("workstation_id") == workstation_id,
                            workstation_id)

    async def get_device_events_by_ids(self, workstation_id: str, event_ids: list[str]) -> list[DeviceEventRow]:
        ws = lit(workstation_id)
        ids = plan_id_lookup(event_ids, view=self._v_events, workstation_id=workstation_id,
                             rejects=self._rejects)
        out: list[DeviceEventRow] = []
        if not ids:
            return out
        async with self._client() as client:  # one connection for the whole loop
            for eid in ids:
                rows = await self._sql(f"SELECT * FROM {self._v_events} WHERE id = {lit(eid)} "
                                       f"AND workstation_id = {ws}", client=client)
                out.extend(await self._accept(
                    self._v_events, DeviceEventRow, rows, workstation_id,
                    lambda r, eid=eid: r.get("id") == eid and r.get("workstation_id") == workstation_id))
        return out

    async def list_device_events(self, workstation_id: str, since_ms: int, until_ms: int) -> list[DeviceEventRow]:
        ws = lit(workstation_id)
        bounds = window_bounds(since_ms, until_ms)
        if bounds is None:
            return []
        lo, hi = bounds
        query = (f"SELECT * FROM {self._v_events} WHERE workstation_id = {ws} "
                 f"AND received_at_ms >= {num(lo)} AND received_at_ms <= {num(hi)}{_limit(WINDOW_ROW_LIMIT)}")
        return await self._rows(self._v_events, DeviceEventRow, query, workstation_id=workstation_id,
                                limit=WINDOW_ROW_LIMIT, scope=_window_scope(workstation_id, lo, hi))

    async def list_baseline_snapshots(self, workstation_id: str, until_ms: int) -> list[BaselineSnapshotRow]:
        ws = lit(workstation_id)
        bounds = window_bounds(0, until_ms)
        if bounds is None:
            return []
        until = bounds[1]
        async with self._client() as client:
            async def fetch(lo: int, hi: int) -> list[dict]:
                return await self._sql(
                    f"SELECT * FROM {self._v_baselines} WHERE workstation_id = {ws} "
                    f"AND received_at_ms >= {num(lo)} AND received_at_ms <= {num(hi)}{_limit(BASELINE_ROW_LIMIT)}",
                    client=client)

            rows, truncated = await narrowed_baseline_rows(fetch, until)
        if truncated:
            self._rejects.add(self._v_baselines, None, row_limit_reason(BASELINE_ROW_LIMIT), workstation_id)
        return await self._accept(self._v_baselines, self._baseline_row, rows, workstation_id,
                                  _window_scope(workstation_id, max(0, until - BASELINE_LOOKBACK_MS), until))

    async def list_device_health(self, workstation_id: str) -> list[DeviceHealthRow]:
        if self.views == "investigator":
            return []  # device health is outside an incident evidence grant
        ws = lit(workstation_id)
        return await self._rows(VIEW_DEVICE_HEALTH, DeviceHealthRow,
                                f"SELECT * FROM {VIEW_DEVICE_HEALTH} WHERE workstation_id = {ws}"
                                f"{_limit(WINDOW_ROW_LIMIT)}",
                                workstation_id=workstation_id, limit=WINDOW_ROW_LIMIT,
                                scope=lambda r: r.get("workstation_id") == workstation_id)

    def pop_rejected_rows(self) -> list[dict]:
        return self._rejects.pop()

    async def list_recent_incidents(self, limit: int = 10) -> list[IncidentRow]:
        """Operator tooling only (bench/live_e2e.py, bench/stdb_probe.py): the newest incidents
        this identity can see. One fixed query on the identity-scoped view. Not an agent tool:
        no model-generated value ever reaches it."""
        query = f"SELECT * FROM {self._v_incidents}{_limit(INCIDENT_LIST_LIMIT)}"
        raw, truncated = cap_rows(await self._sql(query), INCIDENT_LIST_LIMIT)
        if truncated:
            self._rejects.add(self._v_incidents, None, row_limit_reason(INCIDENT_LIST_LIMIT), None)
        valid, rejected = await asyncio.to_thread(split_valid, self._incident_row, raw)
        for rid, reason in rejected:
            self._rejects.add(self._v_incidents, rid, reason, None)
        valid.sort(key=lambda r: r.created_at_ms, reverse=True)
        return valid[:max(1, min(int(limit), 50))]

    async def memberships(self) -> list[dict]:
        # No LIMIT here: the least-privilege check must see every row. The 2 MB cap still bounds it.
        return await self._sql(f"SELECT * FROM {VIEW_MEMBERSHIPS}")

    async def ping(self) -> dict:
        """Reachability plus a least-privilege check of the investigator identity.

        The investigator must never hold approval or owner rights. If it does,
        report it so the operator can revoke them (we do not self-modify rights).
        Only the investigator's own membership rows are counted: rows are matched
        on the canonical identity hex. See attribute_memberships for the
        fail-closed cases.
        """
        try:
            members = await self.memberships()
        except EvidenceUnavailable as e:
            return {"reachable": False, "live": True, "error_code": "evidence_unavailable", "error": str(e)}
        except EvidenceContractError as e:
            return {"reachable": False, "live": True, "error_code": "contract_error", "error": str(e)}
        me = try_normalize_identity(self.provenance.detail.get("identity"))
        counted, judged, identity_match = attribute_memberships(members, me)
        over = [m.get("workstation_id") for m in judged if _over_privileged(m)]
        out = {"reachable": True, "live": True, "memberships": len(counted),
               "workstations": sorted({str(m.get("workstation_id")) for m in counted}),
               "authorized_for_any_workstation": bool(counted),
               "least_privilege_ok": not over, "over_privileged_workstations": over,
               "identity_match": identity_match, "evidence_scope": self.provenance.detail.get("scope")}
        if self.views == "investigator":
            try:
                grants = await self._sql(f"SELECT * FROM {VIEW_INV_INCIDENTS}{_limit(INCIDENT_LIST_LIMIT)}")
            except (EvidenceUnavailable, EvidenceContractError) as e:
                return {"reachable": False, "live": True, "error_code": "evidence_unavailable", "error": str(e)}
            out["authorized_incidents"] = len(grants)
            out["authorized_incident_ids"] = sorted(str(g.get("id")) for g in grants if isinstance(g, dict))[:20]
            out["authorized_for_any_workstation"] = bool(grants)
            # Sid's rule: an evidence-only investigator holds no enabled membership anywhere.
            if counted:
                out["least_privilege_ok"] = False
                out["over_privileged_workstations"] = sorted({str(m.get("workstation_id")) for m in counted} | set(over))
        return out
