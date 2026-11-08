"""Client for Mustafa's Nemotron investigator service (services/nemotron-investigator, INTEGRATION.md v0.3), reached through the M4's
SSH tunnel at 127.0.0.1:18787.

Only real SpacetimeDB incidents (incident:<32 hex>) are sent, always with evidence_mode "live"; the service never falls back to test
data, and neither does this client. LOCKI speaks the service's own spoken_summary (deterministic, grounded and severity-guarded on the
service side) and nothing else as Nemotron's result. Any error is reported as unavailable; the alert path never waits on this.

Settings (environment): LOCKI_INVESTIGATOR_URL (default http://127.0.0.1:18787), LOCKI_INVESTIGATOR_KEY_FILE (default
~/.locki/investigator.key, the requester key collected once over SSH; read per call, never logged).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

import httpx

LIVE_INCIDENT = re.compile(r"^incident:[0-9a-f]{32}$")
TERMINAL = {"completed", "completed_with_warnings", "degraded", "failed", "timeout", "rejected", "interrupted"}
ANSWERED = {"completed", "completed_with_warnings", "degraded"}  # statuses that carry an assessment
POLL_S = 0.75
DEADLINE_S = 45.0  # warm runs take ~9-10 s, cold ~13 s; the service bounds a run itself


class InvestigatorUnavailable(RuntimeError):
    """Nemotron could not produce an assessment for this request; the message says why, in a few words."""


class NemotronService:
    def __init__(self, url: str | None = None, key_file: str | None = None, client: httpx.AsyncClient | None = None):
        self.url = (url or os.environ.get("LOCKI_INVESTIGATOR_URL") or "http://127.0.0.1:18787").rstrip("/")
        self.key_file = Path(os.path.expanduser(key_file or os.environ.get("LOCKI_INVESTIGATOR_KEY_FILE") or "~/.locki/investigator.key"))
        self._client = client
        self.last: dict | None = None  # the last run: ids, status, badge and timings (no evidence text)

    def configured(self) -> bool:
        try:
            return self.key_file.is_file() and self.key_file.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def accepts(incident_id: str | None) -> bool:
        return bool(incident_id and LIVE_INCIDENT.match(incident_id))

    def _headers(self) -> dict:
        try:
            key = self.key_file.read_text().strip()
        except OSError as exc:
            raise InvestigatorUnavailable("the investigator key file can't be read") from exc
        if not key:
            raise InvestigatorUnavailable("the investigator key file is empty")
        return {"Authorization": f"Bearer {key}"}

    async def investigate(self, incident_id: str, note: str = "owner asked by voice: what happened?") -> dict:
        """Start a live investigation of one real incident and wait for its voice view. Returns the voice view plus timings."""
        if not self.accepts(incident_id):
            raise InvestigatorUnavailable("that incident isn't a live SpacetimeDB incident")
        t0 = time.monotonic()
        client = self._client or httpx.AsyncClient(timeout=8.0)
        try:
            body = {"incident_id": incident_id, "evidence_mode": "live", "requester": {"identity": "dhruv-m4-backend", "display_name": "LOCKI voice"},
                    "context": {"note": note[:500]}}
            r = await client.post(f"{self.url}/investigations", json=body, headers=self._headers())
            if r.status_code != 202:
                raise InvestigatorUnavailable(_refusal(r))
            inv = r.json()
            inv_id = inv["investigation_id"]
            if inv.get("evidence_mode") != "live":
                raise InvestigatorUnavailable("the service did not accept the run as live evidence")
            self.last = {"investigation_id": inv_id, "incident_id": incident_id, "status": inv.get("status"), "started_ms": round((time.monotonic() - t0) * 1000, 1)}
            deadline = t0 + DEADLINE_S
            view: dict = {}
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL_S)
                g = await client.get(f"{self.url}/investigations/{inv_id}", params={"view": "voice"}, headers=self._headers())
                if g.status_code != 200:
                    raise InvestigatorUnavailable(_refusal(g))
                view = g.json()
                if view.get("status") in TERMINAL:
                    break
            else:
                raise InvestigatorUnavailable(f"no result within {DEADLINE_S:.0f} s")
            self.last.update(status=view.get("status"), evidence_badge=view.get("evidence_badge"), total_ms=round((time.monotonic() - t0) * 1000, 1),
                             needs_review=view.get("needs_review"), spoken_summary_source=view.get("spoken_summary_source"))
            if view.get("status") not in ANSWERED or not view.get("spoken_summary"):
                raise InvestigatorUnavailable(f"the investigation ended {view.get('status')} without an assessment")
            if view.get("evidence_mode") != "live" or view.get("evidence_badge") != "LIVE" or view.get("evidence_live") is False:
                raise InvestigatorUnavailable("the result was not marked as live evidence")
            return {**view, "total_ms": self.last["total_ms"]}
        except httpx.HTTPError as exc:
            raise InvestigatorUnavailable(f"the investigator service is unreachable ({type(exc).__name__})") from exc
        finally:
            if self._client is None:
                await client.aclose()


    async def record(self, investigation_id: str) -> dict:
        """One of this requester's investigations, trimmed for display (read-only)."""
        if not INVESTIGATION_ID.match(investigation_id or ""):
            raise InvestigatorUnavailable("that is not an investigation id")
        client = self._client or httpx.AsyncClient(timeout=8.0)
        try:
            r = await client.get(f"{self.url}/investigations/{investigation_id}", headers=self._headers())
            if r.status_code != 200:
                raise InvestigatorUnavailable(_refusal(r))
            return trim_record(r.json())
        except httpx.HTTPError as exc:
            raise InvestigatorUnavailable(f"the investigator service is unreachable ({type(exc).__name__})") from exc
        finally:
            if self._client is None:
                await client.aclose()

INVESTIGATION_ID = re.compile(r"^inv_[A-Za-z0-9_-]{1,80}$")


def trim_record(record: dict) -> dict:
    """The part of a full investigation record the owner console shows: badge, headline, status, the grounded facts with their evidence
    ids, which tools ran and on what data, and the total latency. Never the model conversation or the untrusted text list."""
    result = record.get("result") or {}
    return {
        "investigation_id": record.get("investigation_id"), "incident_id": record.get("incident_id"), "status": record.get("status"),
        "evidence_mode": result.get("evidence_mode") or record.get("evidence_mode"), "evidence_badge": result.get("evidence_badge") or record.get("evidence_badge"),
        "headline": result.get("headline"),
        "observed_facts": [{"fact": str(f.get("fact", ""))[:300], "evidence_ids": [str(i) for i in (f.get("evidence_ids") or [])][:12]}
                           for f in (result.get("observed_facts") or [])[:12] if isinstance(f, dict)],
        "tools": [{"tool": t.get("tool"), "status": t.get("status"), "live_data": t.get("live_data")} for t in (result.get("tool_execution_trace") or [])[:24] if isinstance(t, dict)],
        "total_latency_ms": result.get("total_latency_ms"),
    }


def _refusal(r: httpx.Response) -> str:
    reasons = {401: "the investigator key was refused", 403: "the investigator key lacks permission", 404: "the investigation was not found",
               409: "live evidence isn't enabled on the investigator", 413: "the request was too large", 422: "the investigator can't take this incident live",
               429: "the investigator is busy", 503: "the investigator's evidence backend is unavailable"}
    return reasons.get(r.status_code, f"the investigator answered {r.status_code}")
