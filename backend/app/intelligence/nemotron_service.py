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
import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

LIVE_INCIDENT = re.compile(r"^incident:[0-9a-f]{32}$")
TERMINAL = {"completed", "completed_with_warnings", "degraded", "failed", "timeout", "rejected", "interrupted"}
ANSWERED = {"completed", "completed_with_warnings", "degraded"}  # statuses that carry an assessment
POLL_S = 0.75
DEADLINE_S = 45.0  # warm runs take ~9-10 s, cold ~13 s; the service bounds a run itself
MAX_RESPONSE_BYTES = 65_536
MAX_KEY_BYTES = 4096
MAX_SUMMARY_CHARS = 2000
RUN_ID = re.compile(r"inv_[A-Za-z0-9]{1,36}")
SEVERITIES = ("low", "medium", "high", "critical")


class InvestigatorUnavailable(RuntimeError):
    """Nemotron could not produce an assessment for this request; the message says why, in a few words."""


class NemotronService:
    def __init__(self, url: str | None = None, key_file: str | None = None, client: httpx.AsyncClient | None = None):
        self.url = (url or os.environ.get("LOCKI_INVESTIGATOR_URL") or "http://127.0.0.1:18787").rstrip("/")
        self.key_file = Path(os.path.expanduser(key_file or os.environ.get("LOCKI_INVESTIGATOR_KEY_FILE") or "~/.locki/investigator.key"))
        self._client = client
        self._call_id = 0
        self.last: dict | None = None  # the last run: ids, status, badge and timings (no evidence text)

    def configured(self) -> bool:
        try:
            return self.key_file.is_file() and self.key_file.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def accepts(incident_id: str | None) -> bool:
        return isinstance(incident_id, str) and LIVE_INCIDENT.fullmatch(incident_id) is not None

    def _endpoint(self) -> str:
        try:
            parsed = urlsplit(self.url)
            if (any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.url)
                    or parsed.scheme not in {"http", "https"}
                    or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.port is None or parsed.port <= 0
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise ValueError("not a loopback origin")
        except ValueError:
            raise InvestigatorUnavailable("the investigator URL must be an exact loopback origin with a port") from None
        return self.url.rstrip("/")

    def _headers(self) -> dict:
        fd = None
        try:
            fd = os.open(self.key_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("not a private owned regular file")
            if not 0 < info.st_size <= MAX_KEY_BYTES:
                raise ValueError("invalid key size")
            raw = os.read(fd, MAX_KEY_BYTES + 1)
            key = raw.decode("ascii").strip()
            if len(raw) > MAX_KEY_BYTES or not key or any(not 33 <= ord(c) <= 126 for c in key):
                raise ValueError("invalid key")
        except (OSError, ValueError):
            raise InvestigatorUnavailable("the investigator key must be a readable, private owned regular file with a bounded valid token") from None
        finally:
            if fd is not None:
                os.close(fd)
        return {"Authorization": f"Bearer {key}"}

    async def _request(self, client, method, url, headers, *, body=None, params=None, expected=200):
        async with client.stream(method, url, json=body, params=params, headers=headers,
                                 timeout=8.0, follow_redirects=False) as response:
            if response.status_code != expected:
                raise InvestigatorUnavailable(_refusal(response))
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise InvestigatorUnavailable("the investigator response exceeded its size limit")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise InvestigatorUnavailable("the investigator returned malformed JSON") from None
        if not isinstance(value, dict):
            raise InvestigatorUnavailable("the investigator returned an invalid response object")
        return value

    async def investigate(self, incident_id: str, note: str = "owner asked by voice: what happened?") -> dict:
        """Start a live investigation of one real incident and wait for its voice view. Returns the voice view plus timings."""
        self._call_id += 1
        call_id = self._call_id
        self.last = None
        if not self.accepts(incident_id):
            raise InvestigatorUnavailable("that incident isn't a live SpacetimeDB incident")
        base, headers = self._endpoint(), self._headers()
        t0 = time.monotonic()
        client = self._client or httpx.AsyncClient(timeout=8.0, trust_env=False, follow_redirects=False)
        try:
            async with asyncio.timeout(DEADLINE_S):
                body = {"incident_id": incident_id, "evidence_mode": "live", "requester": {"identity": "dhruv-m4-backend", "display_name": "LOCKI voice"},
                        "context": {"note": note[:500] if isinstance(note, str) else ""}}
                inv = await self._request(client, "POST", f"{base}/investigations", headers, body=body, expected=202)
                inv_id = inv.get("investigation_id")
                if not isinstance(inv_id, str) or not RUN_ID.fullmatch(inv_id) or inv.get("status") != "queued":
                    raise InvestigatorUnavailable("the service returned an invalid investigation ID or status")
                if inv.get("evidence_mode") != "live" or inv.get("evidence_badge") != "LIVE":
                    raise InvestigatorUnavailable("the service did not accept the run as live evidence")
                record = {"investigation_id": inv_id, "incident_id": incident_id, "status": inv["status"], "started_ms": round((time.monotonic() - t0) * 1000, 1)}
                if call_id == self._call_id:
                    self.last = record
                while True:
                    await asyncio.sleep(POLL_S)
                    view = await self._request(client, "GET", f"{base}/investigations/{inv_id}", headers, params={"view": "voice"})
                    if view.get("investigation_id") != inv_id or view.get("incident_id") != incident_id:
                        raise InvestigatorUnavailable("the result does not match the requested incident and investigation")
                    if (view.get("evidence_mode") != "live" or view.get("evidence_badge") != "LIVE"
                            or view.get("evidence_live") is not True or view.get("incident_simulated") is not False):
                        raise InvestigatorUnavailable("the result was not marked as non-simulated live evidence")
                    status = view.get("status")
                    if not isinstance(status, str) or status not in TERMINAL | {"queued", "running"}:
                        raise InvestigatorUnavailable("the investigator returned an invalid status")
                    if status in TERMINAL:
                        break
                if status not in ANSWERED:
                    raise InvestigatorUnavailable(f"the investigation ended {status} without an assessment")
                official, effective = view.get("official_severity"), view.get("effective_severity")
                summary = view.get("spoken_summary")
                if (official not in SEVERITIES or effective not in SEVERITIES
                        or SEVERITIES.index(effective) < SEVERITIES.index(official)
                        or not isinstance(summary, str) or not 0 < len(summary) <= MAX_SUMMARY_CHARS
                        or any(ord(c) < 32 or ord(c) == 127 for c in summary)
                        or not summary.startswith(f"LOCKI rated this incident {official} severity.")
                        or not isinstance(view.get("spoken_summary_source"), str)
                        or view["spoken_summary_source"] in ("", "status_template")):
                    raise InvestigatorUnavailable("the investigator returned no valid severity-bound assessment")
                record.update(status=status, evidence_badge="LIVE", total_ms=round((time.monotonic() - t0) * 1000, 1),
                              needs_review=view.get("needs_review"), spoken_summary_source=view.get("spoken_summary_source"))
                return {**view, "total_ms": record["total_ms"]}
        except TimeoutError:
            raise InvestigatorUnavailable(f"no result within {DEADLINE_S:.0f} s") from None
        except httpx.HTTPError as exc:
            raise InvestigatorUnavailable(f"the investigator service is unreachable ({type(exc).__name__})") from exc
        finally:
            if self._client is None:
                await client.aclose()


def _refusal(r: httpx.Response) -> str:
    reasons = {401: "the investigator key was refused", 403: "the investigator key lacks permission", 404: "the investigation was not found",
               409: "live evidence isn't enabled on the investigator", 413: "the request was too large", 422: "the investigator can't take this incident live",
               429: "the investigator is busy", 503: "the investigator's evidence backend is unavailable"}
    return reasons.get(r.status_code, f"the investigator answered {r.status_code}")
