"""FIXTURE evidence adapter. NOT a live database read.

Used only while Sid's device-scoped `my_*` view permissions are being fixed.
Every result produced through this adapter is stamped `live: false` with the
dataset id, and that label propagates into tool results, the assessment and
the API response, so a fixture run can never be mistaken for a live fetch.

Scenario datasets live in fixtures/scenarios/*.json and use the exact row
shapes of the live module (see contracts.py). Ground-truth expectations used
by the benchmark live separately in bench/scenarios/ and are never loaded here.

This adapter follows the same read contract as the live adapter (see base.py).
It uses the same id charset, the same workstation-bound session read and the
same windows on received_at_ms. It applies the same row caps, and validates rows
one at a time, reporting rejected rows through pop_rejected_rows(). A malformed
row in a dataset is therefore reported when it is read; it no longer fails the
whole dataset at load.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from ..contracts import (
    VIEW_BASELINE_SNAPSHOTS,
    VIEW_DEVICE_EVENTS,
    VIEW_DEVICE_HEALTH,
    VIEW_INCIDENTS,
    VIEW_SESSIONS,
    BaselineSnapshotRow,
    DeviceEventRow,
    DeviceHealthRow,
    IncidentRow,
    SessionRow,
)
from .base import (
    BASELINE_ROW_LIMIT,
    REASON_CONTRACT,
    WINDOW_ROW_LIMIT,
    EvidenceContractError,
    EvidenceUnavailable,
    Provenance,
    RejectedRows,
    cap_rows,
    check_id,
    describe_validation_error,
    narrowed_baseline_rows,
    plan_id_lookup,
    raw_in_window,
    raw_row_id,
    row_limit_reason,
    window_bounds,
)

log = logging.getLogger("nemotron_investigator.evidence.fixture")

_TABLES: dict[str, tuple[str, type[BaseModel]]] = {
    "incidents": (VIEW_INCIDENTS, IncidentRow),
    "sessions": (VIEW_SESSIONS, SessionRow),
    "device_events": (VIEW_DEVICE_EVENTS, DeviceEventRow),
    "baseline_snapshots": (VIEW_BASELINE_SNAPSHOTS, BaselineSnapshotRow),
    "device_health": (VIEW_DEVICE_HEALTH, DeviceHealthRow),
}


class _Entry:
    """One dataset row: the raw dict, plus either its validated row or the reason it failed."""

    __slots__ = ("raw", "row", "error")

    def __init__(self, raw: dict, row: BaseModel | None, error: str | None):
        self.raw, self.row, self.error = raw, row, error


class FixtureEvidenceSource:
    def __init__(self, path: str | Path, *, fail_mode: str | None = None, delay_s: float = 0.0):
        self.path = Path(path)
        files = sorted(self.path.glob("*.json")) if self.path.is_dir() else [self.path]
        datasets = [json.loads(f.read_text()) for f in files]
        if not datasets:
            raise ValueError(f"no fixture datasets found at {self.path}")
        ids = [d["dataset_id"] for d in datasets]
        self.dataset_id: str = ids[0] if len(ids) == 1 else f"{len(ids)} scenario datasets"
        merged: dict[str, list] = {}
        for d in datasets:
            for name, rows in d["tables"].items():
                merged.setdefault(name, []).extend(rows)
        self._tables: dict[str, list[_Entry]] = {}
        for name, (_view, model) in _TABLES.items():
            entries = []
            for raw in merged.get(name, []):
                if not isinstance(raw, dict):
                    log.warning("fixture %s: non-object row in %s ignored", self.path.name, name)
                    continue
                try:
                    entries.append(_Entry(raw, model.model_validate(raw), None))
                except ValidationError as e:
                    entries.append(_Entry(raw, None, describe_validation_error(e)))
            self._tables[name] = entries
        self.fail_mode = fail_mode      # None | "unavailable" | "slow"
        self.delay_s = delay_s
        self.provenance = Provenance(
            backend="fixture",
            live=False,
            label=f"FIXTURE {self.dataset_id} (not a live database read)",
            detail={"datasets": ids, "path": self.path.name,
                    "derived_from": [d.get("derived_from", "") for d in datasets]},
        )
        self.reads = 0  # count of backend reads, for tests
        self._rejects = RejectedRows()

    async def _gate(self):
        self.reads += 1
        if self.fail_mode == "unavailable":
            raise EvidenceUnavailable("fixture configured as unavailable (simulated outage)")
        if self.fail_mode == "slow" or self.delay_s:
            await asyncio.sleep(self.delay_s or 30.0)

    # Backward-compatible read-only views of the valid rows (tests subclass and peek at these).
    def _valid(self, table: str) -> list:
        return [e.row for e in self._tables[table] if e.row is not None]

    @property
    def _incidents(self) -> list[IncidentRow]:
        return self._valid("incidents")

    @property
    def _sessions(self) -> list[SessionRow]:
        return self._valid("sessions")

    @property
    def _events(self) -> list[DeviceEventRow]:
        return self._valid("device_events")

    @property
    def _baselines(self) -> list[BaselineSnapshotRow]:
        return self._valid("baseline_snapshots")

    @property
    def _health(self) -> list[DeviceHealthRow]:
        return self._valid("device_health")

    # ------------------------------------------------------------- helpers
    def _select(self, table: str, pred: Callable[[dict], bool]) -> list[_Entry]:
        return [e for e in self._tables[table] if pred(e.raw)]

    def _accept(self, table: str, entries: list[_Entry], workstation_id: str | None) -> list:
        """Valid rows only. Each invalid row is recorded for pop_rejected_rows()."""
        view = _TABLES[table][0]
        out = []
        for e in entries:
            if e.row is not None:
                out.append(e.row)
            else:
                self._rejects.add(view, raw_row_id(e.raw), f"{REASON_CONTRACT}: {e.error}", workstation_id)
        return out

    def _one(self, table: str, entries: list[_Entry]):
        if not entries:
            return None
        e = entries[0]
        if e.row is None:
            raise EvidenceContractError(f"{_TABLES[table][0]} row failed contract validation: {e.error}")
        return e.row

    # --------------------------------------------------------------- reads
    async def get_incident(self, incident_id: str) -> IncidentRow | None:
        await self._gate()
        check_id(incident_id)
        return self._one("incidents", self._select("incidents", lambda r: r.get("id") == incident_id))

    async def get_session(self, workstation_id: str, session_id: str) -> SessionRow | None:
        await self._gate()
        if not workstation_id or not session_id:
            return None
        check_id(workstation_id)
        check_id(session_id)
        return self._one("sessions", self._select(
            "sessions", lambda r: r.get("id") == session_id and r.get("workstation_id") == workstation_id))

    async def get_device_events_by_ids(self, workstation_id: str, event_ids: list[str]) -> list[DeviceEventRow]:
        await self._gate()
        check_id(workstation_id)
        wanted = set(plan_id_lookup(event_ids, view=VIEW_DEVICE_EVENTS, workstation_id=workstation_id,
                                    rejects=self._rejects))
        if not wanted:
            return []
        hits = self._select("device_events", lambda r: r.get("workstation_id") == workstation_id
                            and isinstance(r.get("id"), str) and r["id"] in wanted)
        return self._accept("device_events", hits, workstation_id)

    async def list_device_events(self, workstation_id: str, since_ms: int, until_ms: int) -> list[DeviceEventRow]:
        await self._gate()
        check_id(workstation_id)
        bounds = window_bounds(since_ms, until_ms)
        if bounds is None:
            return []
        lo, hi = bounds
        hits = self._select("device_events", lambda r: r.get("workstation_id") == workstation_id
                            and raw_in_window(r.get("received_at_ms"), lo, hi))
        hits, truncated = cap_rows(hits, WINDOW_ROW_LIMIT)
        if truncated:
            self._rejects.add(VIEW_DEVICE_EVENTS, None, row_limit_reason(WINDOW_ROW_LIMIT), workstation_id)
        return self._accept("device_events", hits, workstation_id)

    async def list_baseline_snapshots(self, workstation_id: str, until_ms: int) -> list[BaselineSnapshotRow]:
        await self._gate()
        check_id(workstation_id)
        bounds = window_bounds(0, until_ms)
        if bounds is None:
            return []
        until = bounds[1]

        async def fetch(lo: int, hi: int) -> list[_Entry]:
            return self._select("baseline_snapshots", lambda r: r.get("workstation_id") == workstation_id
                                and raw_in_window(r.get("received_at_ms"), lo, hi))[:BASELINE_ROW_LIMIT]

        hits, truncated = await narrowed_baseline_rows(fetch, until)
        if truncated:
            self._rejects.add(VIEW_BASELINE_SNAPSHOTS, None, row_limit_reason(BASELINE_ROW_LIMIT), workstation_id)
        return self._accept("baseline_snapshots", hits, workstation_id)

    async def list_device_health(self, workstation_id: str) -> list[DeviceHealthRow]:
        await self._gate()
        check_id(workstation_id)
        hits = self._select("device_health", lambda r: r.get("workstation_id") == workstation_id)
        hits, truncated = cap_rows(hits, WINDOW_ROW_LIMIT)
        if truncated:
            self._rejects.add(VIEW_DEVICE_HEALTH, None, row_limit_reason(WINDOW_ROW_LIMIT), workstation_id)
        return self._accept("device_health", hits, workstation_id)

    def pop_rejected_rows(self) -> list[dict]:
        return self._rejects.pop()

    async def ping(self) -> dict:
        if self.fail_mode == "unavailable":
            return {"reachable": False, "error": "fixture configured as unavailable"}
        return {"reachable": True, "live": False, "dataset_id": self.dataset_id}
