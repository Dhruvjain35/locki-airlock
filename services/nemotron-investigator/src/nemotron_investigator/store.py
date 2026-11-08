"""Investigation records, persisted from the moment they are accepted.

Life cycle (one server process per store directory):
  * create()   writes the queued record to `active/<id>.json` before the API
               answers 202, so an accepted investigation is never only in memory;
  * progress() updates the in-memory copy while the run is queued or running;
  * complete() writes `<id>.json`, removes the active copy and drops the record
               from memory. get() then reads it from disk.
Status counts are kept incrementally, so they stay correct after eviction. On
startup, records left in `active/` by a process that stopped mid-run are marked
`interrupted` with error `service_restart`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from collections import Counter
from pathlib import Path

log = logging.getLogger("nemotron_investigator.store")

ACTIVE_STATUSES = frozenset({"queued", "running"})
INVESTIGATION_ID = re.compile(r"^inv_[A-Za-z0-9]{1,36}$")
MAX_PROGRESS_EVENTS = 500
_STALE_TMP_S = 60.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_json(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, default=str)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _mode_labels(mode: str | None) -> dict:
    """Evidence-mode labels for results the store writes itself (interrupted runs)."""
    if mode not in ("live", "fixture"):
        return {}
    return {"evidence_mode": mode, "evidence_badge": {"live": "LIVE", "fixture": "TEST DATA"}[mode]}


def _copy(rec: dict) -> dict:
    return json.loads(json.dumps(rec, default=str))


class InvestigationStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.active_dir = self.dir / "active"
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.active_dir.mkdir(exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}            # queued/running (plus any record that failed to persist)
        self._counts: dict[str, Counter] = {}          # requester_id -> status -> n (this process)
        self._clean_stale_tmp()
        self.recovered = self._recover()

    # ------------------------------------------------------------ paths / disk
    def _path(self, investigation_id: str) -> Path:
        return self.dir / f"{investigation_id}.json"

    def _active_path(self, investigation_id: str) -> Path:
        return self.active_dir / f"{investigation_id}.json"

    def _clean_stale_tmp(self) -> None:
        cutoff = time.time() - _STALE_TMP_S
        for d in (self.dir, self.active_dir):
            for f in list(d.glob(".inv_*.tmp")) + list(d.glob("inv_*.tmp")):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass

    def _recover(self) -> int:
        """Mark records left queued/running by a previous process as interrupted."""
        n = 0
        for f in sorted(self.active_dir.glob("inv_*.json")):
            inv_id = f.stem
            if not INVESTIGATION_ID.fullmatch(inv_id):
                continue
            if self._path(inv_id).exists():  # it finished; only the active copy was left behind
                f.unlink(missing_ok=True)
                continue
            try:
                rec = json.loads(f.read_text())
                if not isinstance(rec, dict) or rec.get("investigation_id") != inv_id:
                    raise ValueError("not a record")
            except (OSError, ValueError) as e:
                log.error("leftover record %s is unreadable (%s); moving it aside", f.name, type(e).__name__)
                try:
                    f.replace(f.with_name(f.name + ".corrupt"))
                except OSError:
                    pass
                continue
            rec["status"] = "interrupted"
            rec["finished_at_ms"] = _now_ms()
            rec["result"] = {**_mode_labels(rec.get("evidence_mode")), "investigation_id": inv_id,
                             "incident_id": rec.get("incident_id"), "requester_id": rec.get("requester_id"),
                             "investigation_status": "interrupted", "errors": ["service_restart"]}
            try:
                _write_json(self._path(inv_id), rec)
            except OSError:
                log.exception("could not mark leftover record %s as interrupted", inv_id)
                continue
            f.unlink(missing_ok=True)
            n += 1
        if n:
            log.warning("marked %d investigation(s) left queued/running by a previous process as interrupted", n)
        return n

    # ------------------------------------------------------------ counters
    def _move(self, rec: dict, new_status: str) -> None:
        c = self._counts.setdefault(rec["requester_id"], Counter())
        old = rec["status"]
        c[old] -= 1
        if c[old] <= 0:
            del c[old]
        c[new_status] += 1
        rec["status"] = new_status

    # ------------------------------------------------------------ lifecycle
    def create(self, investigation_id: str, incident_id: str, requester_id: str, *,
               allowed_workstations: tuple[str, ...] | list[str] | None = None,
               evidence_mode: str | None = None) -> dict:
        if not isinstance(investigation_id, str) or not INVESTIGATION_ID.fullmatch(investigation_id):
            raise ValueError("invalid investigation id")
        rec = {"investigation_id": investigation_id, "incident_id": incident_id, "requester_id": requester_id,
               "evidence_mode": evidence_mode, "status": "queued", "created_at_ms": _now_ms(), "progress": [],
               "result": None}
        if allowed_workstations is not None:
            # Internal: the grants the run executes under (see api._may_read). Never served.
            rec["_authz"] = {"allowed_workstations": list(allowed_workstations)}
        with self._lock:
            if investigation_id in self._records or self._path(investigation_id).exists():
                raise ValueError("duplicate investigation id")
            _write_json(self._active_path(investigation_id), rec)  # durable before the API answers 202
            self._counts.setdefault(requester_id, Counter())["queued"] += 1
            self._records[investigation_id] = rec
            return _copy(rec)

    def mark_running(self, investigation_id: str, *, allowed_workstations: tuple[str, ...] | list[str] | None = None,
                     queue_wait_ms: float | None = None) -> bool:
        with self._lock:
            rec = self._records.get(investigation_id)
            if rec is None or rec["status"] not in ACTIVE_STATUSES:
                return False
            if rec["status"] == "queued":
                self._move(rec, "running")
            rec["started_at_ms"] = _now_ms()
            if queue_wait_ms is not None:
                rec["queue_wait_ms"] = queue_wait_ms
            if allowed_workstations is not None:
                rec["_authz"] = {"allowed_workstations": list(allowed_workstations)}
            return True

    def progress(self, investigation_id: str, event: dict) -> None:
        if not isinstance(event, dict):
            return
        try:
            event = _copy(event)
        except (TypeError, ValueError):
            event = {"kind": str(event.get("kind", "unknown"))[:64], "note": "unserializable progress event"}
        with self._lock:
            rec = self._records.get(investigation_id)
            if rec is None or rec["status"] not in ACTIVE_STATUSES:
                return
            if rec["status"] == "queued":
                self._move(rec, "running")
            # The workstation the run was authorized for, reported once preflight passes;
            # reads of the record are then scoped to it.
            if event.get("kind") == "preflight_ok" and "workstation_id" not in rec:
                ws = event.get("workstation_id")
                if isinstance(ws, str) and ws:
                    rec["workstation_id"] = ws
            if len(rec["progress"]) < MAX_PROGRESS_EVENTS:
                rec["progress"].append(event)
            else:
                rec["progress_truncated"] = True

    def complete(self, investigation_id: str, result: dict) -> bool:
        """Finish an active record. Returns False if it is unknown or already finished."""
        with self._lock:
            rec = self._records.get(investigation_id)
            if rec is None or rec["status"] not in ACTIVE_STATUSES:
                return False
            status = result.get("investigation_status")
            self._move(rec, status if isinstance(status, str) and status else "failed")
            rec["result"] = result
            rec["finished_at_ms"] = _now_ms()
            try:
                _write_json(self._path(investigation_id), rec)
            except (OSError, TypeError, ValueError):
                log.exception("could not persist investigation %s; keeping it in memory", investigation_id)
                return True
            del self._records[investigation_id]
        try:
            self._active_path(investigation_id).unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove active copy of %s", investigation_id)
        return True

    def interrupt_active(self, error: str) -> int:
        """Finish every queued/running record as interrupted (used at shutdown)."""
        with self._lock:
            active = [(i, r.get("incident_id"), r.get("requester_id"), r.get("evidence_mode"))
                      for i, r in self._records.items() if r["status"] in ACTIVE_STATUSES]
        n = 0
        for inv_id, incident_id, requester_id, mode in active:
            n += self.complete(inv_id, {**_mode_labels(mode), "investigation_id": inv_id, "incident_id": incident_id,
                                        "requester_id": requester_id, "investigation_status": "interrupted",
                                        "errors": [error]})
        return n

    # ------------------------------------------------------------ reads
    def get(self, investigation_id: str) -> dict | None:
        if not isinstance(investigation_id, str) or not INVESTIGATION_ID.fullmatch(investigation_id):
            return None
        with self._lock:
            rec = self._records.get(investigation_id)
            if rec is not None:
                return _copy(rec)
        try:
            data = json.loads(self._path(investigation_id).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("investigation record %s is unreadable", investigation_id)
            return None
        return data if isinstance(data, dict) else None

    def active_for(self, requester_id: str) -> int:
        with self._lock:
            return sum(1 for r in self._records.values()
                       if r["requester_id"] == requester_id and r["status"] in ACTIVE_STATUSES)

    def active_total(self) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r["status"] in ACTIVE_STATUSES)

    def in_memory(self) -> int:
        with self._lock:
            return len(self._records)

    def counts(self, requester_id: str | None = None) -> dict:
        """Investigations created by this process, by status (one requester, or all)."""
        with self._lock:
            if requester_id is not None:
                total = Counter(self._counts.get(requester_id, {}))
            else:
                total = Counter()
                for c in self._counts.values():
                    total.update(c)
            return {k: v for k, v in sorted(total.items()) if v > 0}
