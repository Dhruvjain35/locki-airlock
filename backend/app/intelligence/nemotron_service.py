"""Client for Mustafa's Nemotron investigator service (services/nemotron-investigator, INTEGRATION.md v0.3), reached through the M4's
SSH tunnel at 127.0.0.1:18787.

Only real SpacetimeDB incidents (incident:<32 hex>) are sent, always with evidence_mode "live"; the service never falls back to test
data, and neither does this client. LOCKI speaks the service's own spoken_summary (deterministic, grounded and severity-guarded on the
service side) and nothing else as Nemotron's result, and only after checking that the answer is a live, answered run for exactly the
incident and investigation it asked about. Each run keeps its own record, so overlapping runs never mix. Any error is reported as
unavailable; the alert path never waits on this.

Settings (environment): LOCKI_INVESTIGATOR_URL (default http://127.0.0.1:18787, loopback only), LOCKI_INVESTIGATOR_KEY_FILE (default
~/.locki/investigator.key, the requester key collected once over SSH; owner-only, read once per run without following links, never logged).
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

LIVE_INCIDENT = re.compile(r"incident:[0-9a-f]{32}")
INVESTIGATION_ID = re.compile(r"inv_[A-Za-z0-9_-]{1,80}")
KEY_TEXT = re.compile(r"[\x21-\x7e]{8,512}")
TERMINAL = {"completed", "completed_with_warnings", "degraded", "failed", "timeout", "rejected", "interrupted"}
ANSWERED = {"completed", "completed_with_warnings", "degraded"}  # statuses that carry an assessment
MAX_SPOKEN_CHARS = 1500
POLL_S = 0.75
DEADLINE_S = 45.0  # warm runs take ~9-10 s, cold ~13 s; the service bounds a run itself
_READ_ERRORS = (ValueError, KeyError, TypeError, AttributeError, UnicodeError, httpx.InvalidURL)


class InvestigatorUnavailable(RuntimeError):
    """Nemotron could not produce an assessment for this request; the message says why, in a few words. ``run`` is that run's record."""

    def __init__(self, message: str, run: dict | None = None):
        super().__init__(message)
        self.run = run


class NemotronService:
    def __init__(self, url: str | None = None, key_file: str | None = None, client: httpx.AsyncClient | None = None):
        self.url = (url or os.environ.get("LOCKI_INVESTIGATOR_URL") or "http://127.0.0.1:18787").rstrip("/")
        self.key_file = Path(os.path.expanduser(key_file or os.environ.get("LOCKI_INVESTIGATOR_KEY_FILE") or "~/.locki/investigator.key"))
        self._client = client
        self.last: dict | None = None  # the most recently started run's record (a run's own dict is what its turn reports)

    def loopback(self) -> bool:
        """The key only ever goes to the local end of the SSH tunnel: plain http, a loopback host, no user or password in the URL."""
        try:
            parts = urlsplit(self.url)
            host = parts.hostname or ""
            return parts.scheme == "http" and host in ("127.0.0.1", "localhost", "::1") and parts.username is None and parts.password is None
        except ValueError:
            return False

    def configured(self) -> bool:
        """A loopback URL and an owner-only, regular, non-empty key file (a symlink or a group/world-readable file is refused)."""
        if not self.loopback():
            return False
        try:
            st = self.key_file.lstat()
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_size > 0 and (st.st_mode & 0o077) == 0

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=8.0, trust_env=False, follow_redirects=False)  # never through a proxy, never redirected

    @staticmethod
    def accepts(incident_id: str | None) -> bool:
        return isinstance(incident_id, str) and LIVE_INCIDENT.fullmatch(incident_id) is not None

    def _read_key(self) -> dict:
        """Read the key once (no symlink, owner-only, printable ASCII) and return the auth header. Its text never reaches an error."""
        try:
            fd = os.open(self.key_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            raise InvestigatorUnavailable("the investigator key file can't be read") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_size > 4096:
                raise InvestigatorUnavailable("the investigator key file is not an owner-only key file")
            raw = os.read(fd, 4096)
        finally:
            os.close(fd)
        try:
            key = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise InvestigatorUnavailable("the investigator key file is malformed") from None
        if not KEY_TEXT.fullmatch(key):
            raise InvestigatorUnavailable("the investigator key file is malformed") from None
        return {"Authorization": f"Bearer {key}"}

    async def investigate(self, incident_id: str, note: str = "owner asked by voice: what happened?") -> dict:
        """Start a live investigation of one real incident and wait for its voice view. Returns the voice view plus timings; the run's own
        record is under "run". Raises InvestigatorUnavailable (with .run) for anything that is not a live, answered result."""
        run = {"incident_id": incident_id, "status": "starting", "evidence_badge": None}
        self.last = run
        try:
            return await self._investigate(run, incident_id, note)
        except InvestigatorUnavailable as exc:
            run.update(status="unavailable", error=str(exc), evidence_badge=None)
            exc.run = dict(run)
            raise
        except _READ_ERRORS:
            run.update(status="unavailable", error="the investigator's reply could not be read", evidence_badge=None)
            raise InvestigatorUnavailable("the investigator's reply could not be read", dict(run)) from None
        except httpx.HTTPError as exc:
            reason = f"the investigator service is unreachable ({type(exc).__name__})"
            run.update(status="unavailable", error=reason, evidence_badge=None)
            raise InvestigatorUnavailable(reason, dict(run)) from None

    async def _investigate(self, run: dict, incident_id: str, note: str) -> dict:
        if not self.accepts(incident_id):
            raise InvestigatorUnavailable("that incident isn't a live SpacetimeDB incident")
        if not self.loopback():
            raise InvestigatorUnavailable("the investigator URL is not on this Mac's loopback")
        headers = self._read_key()
        t0 = time.monotonic()
        client = self._client or self._new_client()
        try:
            body = {"incident_id": incident_id, "evidence_mode": "live", "requester": {"identity": "dhruv-m4-backend", "display_name": "LOCKI voice"},
                    "context": {"note": note[:500]}}
            r = await client.post(f"{self.url}/investigations", json=body, headers=headers)
            if r.status_code != 202:
                raise InvestigatorUnavailable(_refusal(r))
            inv = r.json()
            if not isinstance(inv, dict):
                raise InvestigatorUnavailable("the investigator's reply could not be read")
            inv_id = inv.get("investigation_id")
            if not isinstance(inv_id, str) or INVESTIGATION_ID.fullmatch(inv_id) is None:
                raise InvestigatorUnavailable("the investigator returned an invalid investigation id")
            if inv.get("evidence_mode") != "live":
                raise InvestigatorUnavailable("the service did not accept the run as live evidence")
            run.update(investigation_id=inv_id, status=inv.get("status") if isinstance(inv.get("status"), str) else "queued",
                       started_ms=round((time.monotonic() - t0) * 1000, 1))
            deadline = t0 + DEADLINE_S
            view: dict = {}
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL_S)
                g = await client.get(f"{self.url}/investigations/{inv_id}", params={"view": "voice"}, headers=headers)
                if g.status_code != 200:
                    raise InvestigatorUnavailable(_refusal(g))
                view = g.json()
                if not isinstance(view, dict):
                    raise InvestigatorUnavailable("the investigator's reply could not be read")
                if view.get("status") in TERMINAL:
                    break
            else:
                raise InvestigatorUnavailable(f"no result within {DEADLINE_S:.0f} s")
            status = view.get("status")
            run.update(status=status, total_ms=round((time.monotonic() - t0) * 1000, 1), needs_review=view.get("needs_review") is True,
                       spoken_summary_source=view.get("spoken_summary_source") if isinstance(view.get("spoken_summary_source"), str) else None)
            summary = view.get("spoken_summary")
            if status not in ANSWERED or not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SPOKEN_CHARS:
                raise InvestigatorUnavailable(f"the investigation ended {status} without a usable assessment")
            if view.get("incident_id") != incident_id or view.get("investigation_id") != inv_id:
                raise InvestigatorUnavailable("the answer was for a different incident or investigation")
            if view.get("evidence_mode") != "live" or view.get("evidence_badge") != "LIVE" or view.get("evidence_live") is not True:
                raise InvestigatorUnavailable("the result was not marked as live evidence")
            run["evidence_badge"] = "LIVE"  # only now: an answered run on live, authorized evidence for exactly this incident
            return {**view, "spoken_summary": summary.strip(), "total_ms": run["total_ms"], "run": dict(run)}
        finally:
            if self._client is None:
                await client.aclose()

    async def record(self, investigation_id: str) -> dict:
        """One of this requester's investigations, trimmed for display (read-only)."""
        if not isinstance(investigation_id, str) or INVESTIGATION_ID.fullmatch(investigation_id) is None:
            raise InvestigatorUnavailable("that is not an investigation id")
        if not self.loopback():
            raise InvestigatorUnavailable("the investigator URL is not on this Mac's loopback")
        headers = self._read_key()
        client = self._client or self._new_client()
        try:
            r = await client.get(f"{self.url}/investigations/{investigation_id}", headers=headers)
            if r.status_code != 200:
                raise InvestigatorUnavailable(_refusal(r))
            body = r.json()
            if not isinstance(body, dict) or body.get("investigation_id") != investigation_id:
                raise InvestigatorUnavailable("the investigator's reply could not be read")
            return trim_record(body)
        except httpx.HTTPError as exc:
            raise InvestigatorUnavailable(f"the investigator service is unreachable ({type(exc).__name__})") from None
        except _READ_ERRORS:
            raise InvestigatorUnavailable("the investigator's reply could not be read") from None
        finally:
            if self._client is None:
                await client.aclose()


def trim_record(record: dict) -> dict:
    """The part of a full investigation record the owner console shows: badge, headline, status, the grounded facts with their evidence
    ids, which tools ran and on what data, and the total latency. Never the model conversation or the untrusted text list."""
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    text = lambda v, n: v[:n] if isinstance(v, str) else None  # noqa: E731
    facts = result.get("observed_facts") if isinstance(result.get("observed_facts"), list) else []
    trace = result.get("tool_execution_trace") if isinstance(result.get("tool_execution_trace"), list) else []
    latency = result.get("total_latency_ms")
    return {
        "investigation_id": text(record.get("investigation_id"), 90), "incident_id": text(record.get("incident_id"), 60), "status": text(record.get("status"), 40),
        "evidence_mode": text(result.get("evidence_mode") or record.get("evidence_mode"), 20), "evidence_badge": text(result.get("evidence_badge") or record.get("evidence_badge"), 20),
        "headline": text(result.get("headline"), 400),
        "observed_facts": [{"fact": text(f.get("fact"), 300) or "", "evidence_ids": [str(i)[:80] for i in (f.get("evidence_ids") if isinstance(f.get("evidence_ids"), list) else [])][:12]}
                           for f in facts[:12] if isinstance(f, dict)],
        "tools": [{"tool": text(t.get("tool"), 60), "status": text(t.get("status"), 40), "live_data": t.get("live_data") if isinstance(t.get("live_data"), bool) else None}
                  for t in trace[:24] if isinstance(t, dict)],
        "total_latency_ms": latency if isinstance(latency, (int, float)) and not isinstance(latency, bool) else None,
    }


def _refusal(r: httpx.Response) -> str:
    reasons = {401: "the investigator key was refused", 403: "the investigator key lacks permission", 404: "the investigation was not found",
               409: "live evidence isn't enabled on the investigator", 413: "the request was too large", 422: "the investigator can't take this incident live",
               429: "the investigator is busy", 503: "the investigator's evidence backend is unavailable"}
    return reasons.get(r.status_code, f"the investigator answered {r.status_code}")
