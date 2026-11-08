"""LIVE read-only adapter for Sid's SpacetimeDB module (`locki-w91bw`, Maincloud).

Authorization model (no owner privileges, no permission changes):
  * The investigator uses its OWN SpacetimeDB identity (token file, mode 0600),
    never the module owner's CLI token.
  * It reads ONLY the identity-scoped `my_*` views. Rows appear only after the
    workstation owner grants this identity read-only membership
    (e.g. `grant_reviewer(workstation_id, <identity hex>, can_approve=false)`).
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
from pathlib import Path
from typing import Callable

import httpx
from pydantic import BaseModel

from ..contracts import (
    VIEW_BASELINE_SNAPSHOTS,
    VIEW_DEVICE_EVENTS,
    VIEW_DEVICE_HEALTH,
    VIEW_INCIDENTS,
    VIEW_MEMBERSHIPS,
    VIEW_SESSIONS,
    BaselineSnapshotRow,
    DeviceEventRow,
    DeviceHealthRow,
    IncidentRow,
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
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # streamed; a larger response is a contract error
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
    if not isinstance(payload, list):
        raise EvidenceContractError("unexpected SQL response shape")
    out: list[dict] = []
    for stmt in payload:
        if not isinstance(stmt, dict):
            raise EvidenceContractError("unexpected SQL statement shape")
        schema = stmt.get("schema") or {}
        elements = schema.get("elements", []) if isinstance(schema, dict) else None
        rows = stmt.get("rows") or []
        if not isinstance(elements, list) or not isinstance(rows, list):
            raise EvidenceContractError("unexpected SQL statement shape")
        cols = [_name(e) for e in elements]
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
                 timeout_s: float = 5.0, transport: httpx.AsyncBaseTransport | None = None):
        self.host = host.rstrip("/")
        self.database = database
        self._token = token
        self.timeout_s = timeout_s
        self._transport = transport
        self._rejects = RejectedRows()
        canonical = try_normalize_identity(identity) if identity else None
        self.provenance = Provenance(
            backend="spacetimedb", live=True,
            label=f"SpacetimeDB {database} (identity-scoped my_* views)",
            detail={"host": self.host, "database": database, "identity": canonical or "unknown"})

    @classmethod
    def from_token_file(cls, host: str, database: str, token_file: str | Path, **kw):
        p = Path(token_file)
        if not p.is_file():
            raise EvidenceUnavailable(f"investigator identity token not found at {p.name}")
        raw = p.read_text().strip()
        identity = None
        if raw.startswith("{"):
            d = json.loads(raw)
            raw, identity = d["token"], d.get("identity")
        return cls(host, database, raw, identity=identity, **kw)

    # ------------------------------------------------------------- transport
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=min(3.0, self.timeout_s)),
                                 transport=self._transport, follow_redirects=False)

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
    async def get_incident(self, incident_id: str) -> IncidentRow | None:
        rows = await self._sql(f"SELECT * FROM {VIEW_INCIDENTS} WHERE id = {lit(incident_id)}")
        return self._single(VIEW_INCIDENTS, IncidentRow, rows, lambda r: r.get("id") == incident_id)

    async def get_session(self, workstation_id: str, session_id: str) -> SessionRow | None:
        if not workstation_id or not session_id:
            return None
        rows = await self._sql(f"SELECT * FROM {VIEW_SESSIONS} WHERE id = {lit(session_id)} "
                               f"AND workstation_id = {lit(workstation_id)}")
        return self._single(VIEW_SESSIONS, SessionRow, rows,
                            lambda r: r.get("id") == session_id and r.get("workstation_id") == workstation_id,
                            workstation_id)

    async def get_device_events_by_ids(self, workstation_id: str, event_ids: list[str]) -> list[DeviceEventRow]:
        ws = lit(workstation_id)
        ids = plan_id_lookup(event_ids, view=VIEW_DEVICE_EVENTS, workstation_id=workstation_id,
                             rejects=self._rejects)
        out: list[DeviceEventRow] = []
        if not ids:
            return out
        async with self._client() as client:  # one connection for the whole loop
            for eid in ids:
                rows = await self._sql(f"SELECT * FROM {VIEW_DEVICE_EVENTS} WHERE id = {lit(eid)} "
                                       f"AND workstation_id = {ws}", client=client)
                out.extend(await self._accept(
                    VIEW_DEVICE_EVENTS, DeviceEventRow, rows, workstation_id,
                    lambda r, eid=eid: r.get("id") == eid and r.get("workstation_id") == workstation_id))
        return out

    async def list_device_events(self, workstation_id: str, since_ms: int, until_ms: int) -> list[DeviceEventRow]:
        ws = lit(workstation_id)
        bounds = window_bounds(since_ms, until_ms)
        if bounds is None:
            return []
        lo, hi = bounds
        query = (f"SELECT * FROM {VIEW_DEVICE_EVENTS} WHERE workstation_id = {ws} "
                 f"AND received_at_ms >= {num(lo)} AND received_at_ms <= {num(hi)}{_limit(WINDOW_ROW_LIMIT)}")
        return await self._rows(VIEW_DEVICE_EVENTS, DeviceEventRow, query, workstation_id=workstation_id,
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
                    f"SELECT * FROM {VIEW_BASELINE_SNAPSHOTS} WHERE workstation_id = {ws} "
                    f"AND received_at_ms >= {num(lo)} AND received_at_ms <= {num(hi)}{_limit(BASELINE_ROW_LIMIT)}",
                    client=client)

            rows, truncated = await narrowed_baseline_rows(fetch, until)
        if truncated:
            self._rejects.add(VIEW_BASELINE_SNAPSHOTS, None, row_limit_reason(BASELINE_ROW_LIMIT), workstation_id)
        return await self._accept(VIEW_BASELINE_SNAPSHOTS, BaselineSnapshotRow, rows, workstation_id,
                                  _window_scope(workstation_id, max(0, until - BASELINE_LOOKBACK_MS), until))

    async def list_device_health(self, workstation_id: str) -> list[DeviceHealthRow]:
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
        query = f"SELECT * FROM {VIEW_INCIDENTS}{_limit(INCIDENT_LIST_LIMIT)}"
        raw, truncated = cap_rows(await self._sql(query), INCIDENT_LIST_LIMIT)
        if truncated:
            self._rejects.add(VIEW_INCIDENTS, None, row_limit_reason(INCIDENT_LIST_LIMIT), None)
        valid, rejected = await asyncio.to_thread(split_valid, IncidentRow, raw)
        for rid, reason in rejected:
            self._rejects.add(VIEW_INCIDENTS, rid, reason, None)
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
        return {"reachable": True, "live": True, "memberships": len(counted),
                "workstations": sorted({str(m.get("workstation_id")) for m in counted}),
                "authorized_for_any_workstation": bool(counted),
                "least_privilege_ok": not over, "over_privileged_workstations": over,
                "identity_match": identity_match}
