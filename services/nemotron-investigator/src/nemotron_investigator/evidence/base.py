"""Evidence source interface, and the rules every adapter shares.

Implementations are read-only by construction. The fixture and live SpacetimeDB
adapters follow one contract, so a fixture run exercises the same behaviour as
the live path:

  * Workstation scope. Every read names its workstation. A session that exists
    on another workstation is returned as None, exactly like a missing one.
  * Server clock. Event and baseline windows select on received_at_ms. The
    server stamps it, on the same clock as incidents.created_at_ms. The device
    supplies observed_at_ms, which can be skewed, so it never decides whether a
    row is in a window.
  * Per-row validation. List reads validate each row on its own and return only
    the valid rows. The adapter records each invalid row instead. It also records
    linked ids that cannot be queried safely, and any read that hit its row cap.
    Callers drain the records with pop_rejected_rows() after every read.
    Single-row reads raise EvidenceContractError for a row that breaks the
    contract.
  * Identifier policy. Every id is checked against SAFE_ID before any lookup. An
    id outside it raises UnsupportedIdentifier, an EvidenceContractError, and
    never reaches a query.
  * Bounded reads. A window read returns at most WINDOW_ROW_LIMIT rows. The
    baseline read looks back at most BASELINE_LOOKBACK_MS and returns at most
    BASELINE_ROW_LIMIT rows.

Errors. EvidenceUnavailable means the store could not be reached or refused the
read: a transport failure, an auth refusal or a server error. EvidenceContractError
means the store answered, but the query or its rows did not match the agreed
contract: schema drift, an oversized response or an unsupported identifier.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from ..contracts import (
    BaselineSnapshotRow,
    DeviceEventRow,
    DeviceHealthRow,
    IncidentRow,
    SessionRow,
)


class EvidenceUnavailable(Exception):
    """The backing store could not be reached or refused the read."""


class EvidenceContractError(Exception):
    """The store answered, but the query or rows did not match the agreed contract (drift)."""


class UnsupportedIdentifier(EvidenceContractError, ValueError):
    """An id outside SAFE_ID. Refused before any lookup.

    It is also a ValueError, so callers written against the old lit() behaviour keep working.
    """


class Provenance(BaseModel):
    backend: str          # "fixture" | "spacetimedb"
    live: bool            # True only for a real authenticated database read
    label: str            # human-readable, shown in every tool result and assessment
    detail: dict = {}


# ------------------------------------------------------------------ shared limits
WINDOW_ROW_LIMIT = 500                 # list_device_events, list_device_health
BASELINE_ROW_LIMIT = 200               # list_baseline_snapshots
BASELINE_LOOKBACK_MS = 86_400_000      # the baseline read never looks back further than 24 h
# The SQL subset has no ORDER BY, so a capped read is an arbitrary subset and may
# miss the newest snapshot. The baseline read then retries narrower windows that
# end at until_ms.
BASELINE_NARROWING_MS = (BASELINE_LOOKBACK_MS, 3_600_000, 300_000, 60_000)
MAX_LOOKUP_IDS = 20                    # get_device_events_by_ids
MAX_REJECTED_ROWS = 200                # itemized entries held between two drains
U64_MAX = 2**64 - 1

# Reason codes in pop_rejected_rows() entries (a reason may add ": <detail>").
REASON_CONTRACT = "contract_violation"          # row failed validation; it was dropped
REASON_UNQUERYABLE_ID = "unqueryable_id"        # id outside SAFE_ID; never looked up
REASON_ID_LIMIT = "id_limit_reached"            # more than MAX_LOOKUP_IDS distinct ids
REASON_ROW_LIMIT = "row_limit_reached"          # read returned its row cap; more rows may exist
REASON_OUT_OF_SCOPE = "out_of_scope_row_ignored"  # backend returned a row outside the query scope
REASON_OVERFLOW = "rejected_rows_overflow"      # more entries than MAX_REJECTED_ROWS

# One strict charset for every id that is looked up. Each of these characters is
# inert inside a single-quoted SQL literal. Quotes, backslash, whitespace,
# semicolons and comment markers are excluded.
SAFE_ID = re.compile(r"[A-Za-z0-9:_\-.@/+=]{1,128}")


def is_queryable_id(value: object) -> bool:
    return isinstance(value, str) and SAFE_ID.fullmatch(value) is not None


def check_id(value: object) -> str:
    if not is_queryable_id(value):
        raise UnsupportedIdentifier("identifier outside supported charset")
    return value  # type: ignore[return-value]


def window_bounds(since_ms: int, until_ms: int) -> tuple[int, int] | None:
    """Clamp a window to the u64 range; None when it is empty."""
    for v in (since_ms, until_ms):
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("numeric bound failed validation")
    lo, hi = max(0, since_ms), min(until_ms, U64_MAX)
    return (lo, hi) if lo <= hi else None


def raw_in_window(v: object, lo: int, hi: int) -> bool:
    """Window test on a raw (not yet validated) value.

    A malformed window column passes, so that validation rejects the row and
    reports it instead of the row silently falling out of the window.
    """
    if isinstance(v, int) and not isinstance(v, bool):
        return lo <= v <= hi
    return True


def row_limit_reason(limit: int) -> str:
    return f"{REASON_ROW_LIMIT}: read returned its {limit}-row cap; more rows may exist"


def cap_rows(rows: list, limit: int) -> tuple[list, bool]:
    """(at most `limit` rows, whether the cap was reached).

    Reaching the cap counts as truncation: a backend LIMIT returns exactly
    `limit` rows when more exist.
    """
    return rows[:limit], len(rows) >= limit


def describe_validation_error(e: ValidationError, limit: int = 3) -> str:
    """Field names and messages only. Input values are untrusted and never echoed."""
    parts = []
    for err in e.errors(include_url=False, include_input=False)[:limit]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "row"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    more = e.error_count() - limit
    if more > 0:
        parts.append(f"+{more} more")
    return "; ".join(parts)[:200]


def raw_row_id(row: object) -> str | None:
    rid = row.get("id") if isinstance(row, dict) else None
    return rid if isinstance(rid, str) else None


def split_valid(model: type[BaseModel], rows: list) -> tuple[list, list[tuple[str | None, str]]]:
    """Validate each row on its own: (valid rows, [(raw id, reason) for each rejected row]).

    Pure function with no shared state, so it is safe to run in a worker thread.
    """
    valid: list = []
    rejected: list[tuple[str | None, str]] = []
    for r in rows:
        try:
            valid.append(model.model_validate(r))
        except ValidationError as e:
            rejected.append((raw_row_id(r), f"{REASON_CONTRACT}: {describe_validation_error(e)}"))
    return valid, rejected


def validate_one(model: type[BaseModel], row: object, view: str):
    try:
        return model.model_validate(row)
    except ValidationError as e:
        raise EvidenceContractError(
            f"{view} row failed contract validation: {describe_validation_error(e)}") from e


class RejectedRows:
    """Buffer of dropped rows and gaps between two pop_rejected_rows() drains.

    Entries: {"view", "id", "reason", "workstation_id"}. `id` is the raw backend
    or linked id: untrusted text that callers must sanitize before display. It is
    None when the row had no string id. `workstation_id` is the scope of the read
    that produced the entry, so callers can drop entries that belong to another
    investigation. The buffer is bounded; once full, further entries are counted
    in a single overflow entry.
    """

    def __init__(self, cap: int = MAX_REJECTED_ROWS):
        self._cap = cap
        self._items: list[dict] = []
        self._overflow = 0

    def add(self, view: str, row_id: object, reason: str, workstation_id: str | None = None) -> None:
        if len(self._items) >= self._cap:
            self._overflow += 1
            return
        self._items.append({"view": view, "id": row_id if isinstance(row_id, str) else None,
                            "reason": reason[:200], "workstation_id": workstation_id})

    def pop(self) -> list[dict]:
        items, self._items = self._items, []
        if self._overflow:
            items.append({"view": "*", "id": None, "workstation_id": None,
                          "reason": f"{REASON_OVERFLOW}: {self._overflow} further entries not itemized"})
            self._overflow = 0
        return items


def plan_id_lookup(event_ids: list, *, view: str, workstation_id: str, rejects: RejectedRows) -> list[str]:
    """Return the distinct, queryable ids to look up, in order.

    Ids outside SAFE_ID are recorded as unqueryable_id. They must not look merely
    missing. Ids past MAX_LOOKUP_IDS are recorded as id_limit_reached.
    """
    out: list[str] = []
    seen: set[str] = set()
    n = 0
    for eid in event_ids or []:
        if isinstance(eid, str):
            if eid in seen:
                continue
            seen.add(eid)
        n += 1
        if n > MAX_LOOKUP_IDS:
            rejects.add(view, eid, REASON_ID_LIMIT, workstation_id)
        elif not is_queryable_id(eid):
            rejects.add(view, eid, REASON_UNQUERYABLE_ID, workstation_id)
        else:
            out.append(eid)
    return out


async def narrowed_baseline_rows(fetch: Callable[[int, int], Awaitable[list]],
                                 until_ms: int) -> tuple[list, bool]:
    """Shared bounded read of the snapshots that end at until_ms. Returns (rows, truncated).

    The read looks back BASELINE_LOOKBACK_MS. `fetch(lo, hi)` returns at most
    BASELINE_ROW_LIMIT rows whose received_at_ms lies in [lo, hi]. A capped
    result is an arbitrary subset (there is no ORDER BY), so narrower windows
    ending at until_ms are tried next. A narrower window that comes back under
    the cap and not empty holds the newest snapshot. If it comes back empty, the
    wider capped result is kept and marked truncated.
    """
    best: list | None = None
    prev_lo: int | None = None
    for lookback in BASELINE_NARROWING_MS:
        lo = max(0, until_ms - lookback)
        if lo == prev_lo:
            continue
        prev_lo = lo
        rows = (await fetch(lo, until_ms))[:BASELINE_ROW_LIMIT]
        if len(rows) < BASELINE_ROW_LIMIT:
            if rows or best is None:
                return rows, False
            return best, True
        best = rows
    return best or [], True


@runtime_checkable
class EvidenceSource(Protocol):
    provenance: Provenance

    async def get_incident(self, incident_id: str) -> IncidentRow | None: ...

    async def get_session(self, workstation_id: str, session_id: str) -> SessionRow | None:
        """The session only if it belongs to `workstation_id`. A session on another workstation is None."""
        ...

    async def get_device_events_by_ids(
        self, workstation_id: str, event_ids: list[str]
    ) -> list[DeviceEventRow]:
        """Events of this workstation with these ids. Unqueryable ids are reported via pop_rejected_rows()."""
        ...

    async def list_device_events(
        self, workstation_id: str, since_ms: int, until_ms: int
    ) -> list[DeviceEventRow]:
        """Events with since_ms <= received_at_ms <= until_ms (server clock), at most WINDOW_ROW_LIMIT."""
        ...

    async def list_baseline_snapshots(
        self, workstation_id: str, until_ms: int
    ) -> list[BaselineSnapshotRow]:
        """Snapshots received within BASELINE_LOOKBACK_MS before until_ms, at most BASELINE_ROW_LIMIT."""
        ...

    async def list_device_health(self, workstation_id: str) -> list[DeviceHealthRow]: ...

    def pop_rejected_rows(self) -> list[dict]:
        """Drain the entries {"view", "id", "reason", "workstation_id"} recorded since the last call."""
        ...

    async def ping(self) -> dict: ...


__all__ = [
    "BASELINE_LOOKBACK_MS", "BASELINE_NARROWING_MS", "BASELINE_ROW_LIMIT", "EvidenceContractError",
    "EvidenceSource", "EvidenceUnavailable", "MAX_LOOKUP_IDS", "MAX_REJECTED_ROWS", "Provenance",
    "REASON_CONTRACT", "REASON_ID_LIMIT", "REASON_OUT_OF_SCOPE", "REASON_OVERFLOW", "REASON_ROW_LIMIT",
    "REASON_UNQUERYABLE_ID", "RejectedRows", "SAFE_ID", "U64_MAX", "UnsupportedIdentifier",
    "WINDOW_ROW_LIMIT", "cap_rows", "check_id", "describe_validation_error", "is_queryable_id",
    "narrowed_baseline_rows", "plan_id_lookup", "raw_in_window", "raw_row_id", "row_limit_reason", "split_valid",
    "validate_one", "window_bounds",
]
