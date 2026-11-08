"""Authenticated HTTP API for Dhruv's Mac backend.

  POST /investigations          -> 202 {investigation_id, status}
  GET  /investigations/{id}     -> status, progress, tool trace, structured assessment
  GET  /investigations/{id}?view=voice -> concise fields for the Mac voice system
  GET  /health                  -> Ollama/model/evidence-backend status and degraded deps

Every endpoint requires `Authorization: Bearer <api key>`. A pure ASGI guard in
front of routing checks the key from the headers alone, so an unauthenticated
request gets 401 before any body byte is read; bodies are capped at 8 KiB (413).
The server binds to 127.0.0.1 by default and refuses a non-loopback bind unless
explicitly allowed.

Investigations run one at a time against the shared local model. Admission is
bounded (429): at most 4 queued or running per requester, 8 overall, and 30
created per requester per minute. A queued run waits at most
min(120 s, total_timeout_s) for the model, then finishes as `timeout`
(`queue_timeout`); a run whose API key was revoked while it waited finishes as
`rejected` (`api_key_revoked`). Accepted runs are on disk from the start, and a
shutdown finishes them as `interrupted` (`service_shutdown`).

A record is readable only by its requester, and only while the reading key may
access the workstation the run was authorized for.

Evidence mode. Each investigation runs on exactly one evidence source, chosen at
creation: "live" (LOCKI's SpacetimeDB through the read-only investigator
identity) or "fixture" (test data). A request may name `evidence_mode`; without
it the server's default applies (NI_EVIDENCE_BACKEND). A mode the server does not
offer is refused (409), never substituted. Live runs only take `incident:<32 hex>`
ids and fixture runs only `incident:fixture-<slug>` ids (422 otherwise), so a real
incident can never be investigated on fixtures. Every response about a run carries
the `X-Evidence-Mode` header and `evidence_mode` / `evidence_badge` fields.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import re
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .agent import Requester, evidence_mode_fields, investigate
from .assessment import overclaims, understates_severity
from .auth import ApiPrincipal, KeyStore
from .config import Settings
from .evidence.authorization import incident_scope_digest
from .evidence.base import EvidenceSource, EvidenceUnavailable
from .ollama_client import OllamaClient
from .store import ACTIVE_STATUSES, INVESTIGATION_ID, InvestigationStore
from .tools import INCIDENT_ID_PATTERN, incident_id_mode

log = logging.getLogger("nemotron_investigator.api")

MAX_ACTIVE_PER_REQUESTER = 4      # queued + running, per requester
MAX_ACTIVE_TOTAL = 8              # queued + running, all requesters
CREATE_RATE_LIMIT = 30            # investigations created per requester ...
CREATE_RATE_WINDOW_S = 60.0       # ... per sliding window
MAX_QUEUE_WAIT_S = 120.0          # longest wait for the model slot (never above total_timeout_s)
RUN_GRACE_S = 15.0                # outer bound on one run: total_timeout_s + this
MAX_BODY_BYTES = 8192
BODY_READ_TIMEOUT_S = 10.0
SHUTDOWN_GRACE_S = 10.0
HEALTH_CACHE_S = 5.0              # /health reuses dependency checks for this long
UVICORN_LIMIT_CONCURRENCY = 32

_PRINCIPAL_KEY = "nemotron_investigator.principal"  # ASGI scope key set by the guard
_CONTENT_LENGTH = re.compile(rb"^\s*[0-9]{1,15}\s*$")


class RequesterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: str = Field(min_length=1, max_length=128, description="caller's own identity label (informational)")
    display_name: str | None = Field(default=None, max_length=80)


class ContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=500, description="untrusted free text shown to the model as data")


class InvestigationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_id: str = Field(pattern=INCIDENT_ID_PATTERN)
    evidence_mode: Literal["live", "fixture"] | None = Field(
        default=None, description="live = LOCKI SpacetimeDB; fixture = test data. Omitted: the server default.")
    requester: RequesterIn | None = None
    context: ContextIn | None = None


EVIDENCE_MODES = ("live", "fixture")


def default_mode(settings: Settings) -> str:
    return "live" if settings.evidence_backend == "spacetimedb" else "fixture"


def build_source(settings: Settings, mode: str | None = None) -> EvidenceSource:
    mode = mode or default_mode(settings)
    if mode == "live":
        from .evidence.spacetime_http import SpacetimeHttpEvidenceSource
        return SpacetimeHttpEvidenceSource.from_token_file(settings.stdb_host, settings.stdb_database,
                                                           settings.stdb_token_file,
                                                           timeout_s=settings.tool_timeout_s,
                                                           views=settings.stdb_views)
    from .evidence.fixture import FixtureEvidenceSource
    return FixtureEvidenceSource(settings.fixture_path)


def _mode_of(src: EvidenceSource) -> str:
    return "live" if src.provenance.live else "fixture"


# --------------------------------------------------------------------- request guard
_INVALID, _TOO_LARGE, _DISCONNECTED = object(), object(), object()


def _header_values(scope: dict, name: bytes) -> list[bytes]:
    return [v for k, v in scope.get("headers") or [] if k.lower() == name]


def _bearer_token(scope: dict) -> str | None:
    values = _header_values(scope, b"authorization")
    if len(values) != 1:  # missing, or ambiguous
        return None
    value = values[0].decode("latin-1")
    if value[:7].lower() != "bearer ":
        return None
    return value[7:].strip() or None


def _declared_length(scope: dict):
    values = _header_values(scope, b"content-length")
    if not values:
        return None
    if len(set(values)) != 1 or not _CONTENT_LENGTH.fullmatch(values[0]):
        return _INVALID
    return int(values[0])


async def _read_body(receive, limit: int):
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return _DISCONNECTED
        if message["type"] != "http.request":
            continue
        chunk = message.get("body") or b""
        size += len(chunk)
        if size > limit:
            return _TOO_LARGE
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _error(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail}, headers=headers)


class RequestGuard:
    """Pure ASGI middleware in front of routing.

    1. Authenticates the Bearer key from the headers. A missing or invalid key
       gets 401 before a single body byte is read.
    2. Rejects a declared Content-Length above MAX_BODY_BYTES with 413, then reads
       the body itself and stops (413) as soon as more than MAX_BODY_BYTES arrive.
    3. Replays the bounded body to the app and passes the principal in the scope.
    """

    def __init__(self, app, *, keys):
        self.app = app
        self._keys = keys  # callable returning the live KeyStore (None before startup)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        keys: KeyStore | None = self._keys()
        if keys is None:
            await _error(503, "service is starting")(scope, receive, send)
            return
        principal = keys.authenticate(_bearer_token(scope))
        if principal is None:
            await _error(401, "missing or invalid API key", {"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is _INVALID:
            await _error(400, "invalid Content-Length")(scope, receive, send)
            return
        if declared is not None and declared > MAX_BODY_BYTES:
            await _error(413, f"request body exceeds {MAX_BODY_BYTES} bytes")(scope, receive, send)
            return
        try:
            body = await asyncio.wait_for(_read_body(receive, MAX_BODY_BYTES), timeout=BODY_READ_TIMEOUT_S)
        except TimeoutError:
            await _error(408, "request body not received in time")(scope, receive, send)
            return
        if body is _DISCONNECTED:
            return
        if body is _TOO_LARGE:
            await _error(413, f"request body exceeds {MAX_BODY_BYTES} bytes")(scope, receive, send)
            return
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app({**scope, _PRINCIPAL_KEY: principal}, replay, send)


# --------------------------------------------------------------------- admission
class _RateLimiter:
    """Sliding-window limit on investigations created per requester (in memory)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, deque] = {}

    def _window(self, key: str, now: float) -> deque | None:
        q = self._hits.get(key)
        if q is None:
            return None
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if not q:
            del self._hits[key]
            return None
        return q

    def retry_after(self, key: str) -> int | None:
        now = time.monotonic()
        q = self._window(key, now)
        if q is None or len(q) < self.limit:
            return None
        return max(1, math.ceil(self.window_s - (now - q[0])))

    def record(self, key: str) -> None:
        now = time.monotonic()
        self._window(key, now)
        self._hits.setdefault(key, deque()).append(now)


# --------------------------------------------------------------------- read scope
_PAST_PREFLIGHT_RESULT_KEYS = ("tool_execution_trace", "model_calls", "conversation", "timeline",
                               "observed_facts", "risk_assessment", "untrusted_content_flags",
                               "official_severity", "effective_severity", "headline", "summary",
                               "spoken_summary", "model_drafts", "evidence_gaps", "evidence_scope_sha256")


def _past_preflight(rec: dict) -> bool:
    """True when the run got far enough to hold incident data."""
    if rec.get("progress"):
        return True
    r = rec.get("result")
    return isinstance(r, dict) and any(r.get(k) for k in _PAST_PREFLIGHT_RESULT_KEYS)


def _may_read(rec: dict, p: ApiPrincipal) -> bool:
    if rec.get("requester_id") != p.requester_id:
        return False
    reader = Requester(p.requester_id, p.allowed_workstations)
    ws = rec.get("workstation_id")
    if isinstance(ws, str) and ws:
        return reader.may_access(ws)
    if not _past_preflight(rec):
        return True  # rejected or stopped before preflight: no incident data in the record
    # Preflight passed but no workstation was recorded (a record from before this check,
    # or an agent that did not report it). Allow only a reader whose grants cover every
    # workstation the run could have reached; otherwise fail closed.
    if "*" in p.allowed_workstations:
        return True
    grants = (rec.get("_authz") or {}).get("allowed_workstations")
    return (isinstance(grants, list) and bool(grants) and "*" not in grants
            and all(isinstance(w, str) and reader.may_access(w) for w in grants))


# --------------------------------------------------------------------- voice view
_RISK_LEVELS = ("low", "medium", "high", "critical")
_SEV_ORDER = {lvl: i for i, lvl in enumerate(_RISK_LEVELS)}
_CONFIDENCE = ("low", "medium", "high")
_SUMMARY_FIELDS = ("summary", "spoken_summary")
_CODE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_ASSESSED = ("completed", "completed_with_warnings", "degraded")
_BADGE = {"live": "LIVE", "fixture": "TEST DATA"}
# Review reasons under which no model-written or data-derived sentence is read aloud.
_NO_FREE_TEXT = ("untrusted_content", "executed_action_claimed", "validation_missing")
_STATUS_TEXT = {
    "failed": "The investigation could not finish, so there is no Nemotron assessment.",
    "timeout": "The investigation ran out of time, so there is no Nemotron assessment.",
    "interrupted": "The investigation was interrupted, so there is no Nemotron assessment.",
    "rejected": "The investigation request was rejected.",
}
_MAX_SENTENCE = 180
_INTERROGATIVE = re.compile(r"^\s*(who|what|when|where|why|how|whether|which|if)\b", re.I)
_STATES_UNKNOWN = re.compile(r"\b(unknown|unclear|missing|absent|unavailable|not (?:been )?(?:established|known|"
                             r"confirmed|determined|available|retrieved|checked|identified|recorded)|cannot|"
                             r"could not)\b", re.I)
_CATEGORY_NOUN = {"usb": ("USB event", "USB events"), "vision": ("camera event", "camera events"),
                  "application": ("application event", "application events"),
                  "system": ("system event", "system events")}


def _codes(items: Any) -> list[str]:
    out: list[str] = []
    for a in items if isinstance(items, list) else []:
        code = a.get("code") if isinstance(a, dict) else None
        if isinstance(code, str) and _CODE.fullmatch(code) and code not in out:
            out.append(code)
    return out


def _sentence(text: str) -> str:
    t = " ".join(str(text).split()).strip()
    if len(t) > _MAX_SENTENCE:
        t = t[:_MAX_SENTENCE].rsplit(" ", 1)[0].rstrip(",;: ")
    if t and t[-1] not in ".!?":
        t += "."
    return t


def _sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?])\s+", " ".join(str(text).split())) if x]


def mentions_lower_level(text: str, official_level: str | None) -> bool:
    """Kept for callers of the earlier name; see assessment.understates_severity."""
    return understates_severity(text, official_level)


def record_mode(rec: dict) -> str | None:
    """The evidence mode of a stored record, including records written before evidence_mode existed."""
    r = rec.get("result") if isinstance(rec.get("result"), dict) else {}
    m = r.get("evidence_mode") or rec.get("evidence_mode")
    if m in EVIDENCE_MODES:
        return m
    prov = r.get("evidence_provenance")
    live = prov.get("live") if isinstance(prov, dict) else None
    return {True: "live", False: "fixture"}.get(live) if isinstance(live, bool) else None


def _contains_untrusted(text: str, untrusted: list[str]) -> bool:
    low = text.lower()
    return any(len(u) >= 4 and u.lower() in low for u in untrusted)


def _readable(text: Any, official_level: str | None, untrusted: list[str]) -> bool:
    return (isinstance(text, str) and bool(text.strip()) and not text.lstrip().startswith("[system]")
            and not overclaims(text, observed=False) and not understates_severity(text, official_level)
            and not _contains_untrusted(text, untrusted))


def _category_sentence(r: dict) -> str | None:
    counts: dict[str, int] = {}
    for e in r.get("timeline") or []:
        if isinstance(e, dict) and e.get("type") == "device_event" and e.get("category") in _CATEGORY_NOUN:
            counts[e["category"]] = counts.get(e["category"], 0) + 1
    if not counts:
        return None
    parts = [f"{n} {_CATEGORY_NOUN[c][0] if n == 1 else _CATEGORY_NOUN[c][1]}" for c, n in sorted(counts.items())]
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"The retrieved evidence includes {listed}."


def _gap_sentence(r: dict) -> str | None:
    from .agent import EVIDENCE_CLASSES
    labels = []
    for g in r.get("evidence_gaps") or []:
        label = EVIDENCE_CLASSES.get(g.get("tool")) if isinstance(g, dict) else None
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return None
    return _sentence("Not available to the investigation: " + ", ".join(labels[:3]))


def _findings(r: dict, reasons: list[str], official_level: str | None,
              model_level: str | None) -> tuple[list[str], str]:
    """Up to three short sentences about what was actually found, and their source."""
    untrusted = [u for u in r.get("untrusted_text_values") or [] if isinstance(u, str)]
    if any(x in reasons for x in _NO_FREE_TEXT):
        validation = r.get("validation") if isinstance(r.get("validation"), dict) else {}
        metrics = r.get("metrics") if isinstance(r.get("metrics"), dict) else {}
        n_ev = len(validation.get("retrieved_evidence_ids") or [])
        n_src = len(metrics.get("distinct_tools_ok") or [])
        return ([f"Nemotron reviewed {n_ev} evidence records from {n_src} sources; their text is withheld "
                 "until a human reviews it."], "counts_only")
    # Nemotron's own narrative is never read when Nemotron rated the risk below the official level:
    # its view is spoken separately, labeled as its view.
    below = (official_level in _SEV_ORDER and model_level in _SEV_ORDER
             and _SEV_ORDER[model_level] < _SEV_ORDER[official_level])
    out: list[str] = []
    source = "grounded_facts"
    text = r.get("spoken_summary")
    if (not below and str(r.get("summary_source") or "").startswith("model") and "summary_flagged" not in reasons
            and _readable(text, official_level, untrusted)):
        out = [_sentence(x) for x in _sentences(text)[:2]]
        source = "model"
    if not out:
        facts = [f for f in r.get("observed_facts") or []
                 if isinstance(f, dict) and _readable(f.get("fact"), official_level, untrusted)]
        facts.sort(key=lambda f: 0 if f.get("basis") == "observed" else 1)
        out = [_sentence(f["fact"]) for f in facts[:2]]
    if not out:
        cat_sentence = _category_sentence(r)
        out, source = ([cat_sentence], "evidence_counts") if cat_sentence else (
            ["No finding could be confirmed from the retrieved evidence."], "evidence_counts")
    unknown = None
    if not below and "summary_flagged" not in reasons:
        unknown = next((m for m in r.get("missing_information") or []
                        if _readable(m, official_level, untrusted)
                        and (_STATES_UNKNOWN.search(m) or _INTERROGATIVE.match(m))), None)
    if unknown:
        reads_as_is = not _INTERROGATIVE.match(unknown)
        out.append(_sentence(unknown if reads_as_is else f"Not established: {unknown[0].lower() + unknown[1:]}"))
    else:
        gap = _gap_sentence(r)
        if gap:
            out.append(gap)
    return out, source


def voice_view(rec: dict) -> dict:
    """Short spoken text plus the fields a voice or UI client needs.

    The official severity (incident record) is always spoken first and is never replaced by
    Nemotron's level; Nemotron's view follows as its own labeled sentence, and its narrative is
    not read when it rated the risk below the official level. Fixture runs open with "Test data
    only". Attacker-controllable strings (device and app names, summaries) are never spoken."""
    status = rec.get("status")
    r = rec.get("result") if isinstance(rec.get("result"), dict) else {}
    mode = record_mode(rec)
    official = r.get("official_severity") if isinstance(r.get("official_severity"), dict) else {}
    off_level = official.get("level") if official.get("level") in _RISK_LEVELS else None
    eff = r.get("effective_severity") if isinstance(r.get("effective_severity"), dict) else {}
    eff_level = eff.get("level") if eff.get("level") in _RISK_LEVELS else None
    ra = r.get("risk_assessment") if isinstance(r.get("risk_assessment"), dict) else {}
    level = ra.get("level") if ra.get("level") in _RISK_LEVELS else None
    confidence = ra.get("confidence") if ra.get("confidence") in _CONFIDENCE else None
    simulated = r.get("incident_simulated") is True
    advisory_codes = _codes(r.get("advisories"))
    validation = r.get("validation")
    flags = [f for f in (validation.get("flags") or []) if isinstance(f, dict)] \
        if isinstance(validation, dict) and isinstance(validation.get("flags"), list) else []
    summary_flagged = any(f.get("field") in _SUMMARY_FIELDS for f in flags)
    action_claimed = any(f.get("code") == "claims_executed_action" for f in flags)
    finished = status not in ACTIVE_STATUSES

    reasons: list[str] = []
    if not finished:
        reasons.append("investigation_not_finished")
    elif status != "completed":
        reasons.append("status_not_completed")
    if advisory_codes:
        reasons.append("advisories")
    if summary_flagged:
        reasons.append("summary_flagged")
    if action_claimed:
        reasons.append("executed_action_claimed")
    if r.get("untrusted_content_flags"):
        reasons.append("untrusted_content")
    if status == "completed" and not isinstance(validation, dict):
        reasons.append("validation_missing")
    if simulated:
        reasons.append("incident_simulated")
    if mode is None and finished:
        reasons.append("evidence_mode_unknown")
    needs_review = bool(reasons)

    spoken, source = None, None
    if finished:
        parts: list[str] = []
        if mode == "fixture":
            parts.append("Test data only, not the live LOCKI database.")
        elif mode is None:
            parts.append("The evidence source of this record is not recorded.")
        if simulated:
            parts.append("LOCKI marked this incident as simulated.")
        if off_level:
            parts.append(f"LOCKI rated this incident {off_level} severity.")
        elif official:
            parts.append("LOCKI's severity for this incident is not a standard level.")
        elif status in _ASSESSED:
            parts.append("LOCKI's official severity is not available for this record.")
        if status in _ASSESSED and level is not None:
            body, source = _findings(r, reasons, off_level, level)
            parts.extend(body)
            parts.append(f"Nemotron's evidence review: {level} risk"
                         + (f", {confidence} confidence." if confidence else "."))
            if off_level and _SEV_ORDER[level] < _SEV_ORDER[off_level]:
                parts.append(f"The official {off_level} severity stands.")
            elif off_level and _SEV_ORDER[level] > _SEV_ORDER[off_level]:
                parts.append("Nemotron rates it above the official severity.")
            if status == "degraded":
                parts.append("Some evidence could not be retrieved.")
        else:
            parts.append(_STATUS_TEXT.get(status, "No Nemotron assessment is available."))
            source = "status_template"
        if needs_review:
            parts.append("This needs human review.")
        spoken = " ".join(parts)

    prov = r.get("evidence_provenance") if isinstance(r.get("evidence_provenance"), dict) else {}
    return {"investigation_id": rec.get("investigation_id"), "status": status, "incident_id": rec.get("incident_id"),
            "evidence_mode": mode, "evidence_badge": _BADGE.get(mode, "UNVERIFIED"),
            "evidence_banner": r.get("evidence_banner"), "incident_simulated": simulated,
            "spoken_summary": spoken, "spoken_summary_source": source, "spoken_summary_flagged": summary_flagged,
            "headline": r.get("headline"),
            "official_severity": off_level, "effective_severity": eff_level,
            "nemotron_risk_level": level, "nemotron_risk_confidence": confidence,
            "needs_review": needs_review, "review_reasons": reasons, "advisory_codes": advisory_codes,
            "requires_human_approval": True,
            "evidence_live": prov.get("live") if prov else (mode == "live" if mode else None),
            "evidence_label": prov.get("label")}


# --------------------------------------------------------------------- health
_PING_PUBLIC = ("memberships", "least_privilege_ok", "authorized_incidents", "evidence_scope")
_PING_ADMIN = _PING_PUBLIC + ("authorized_for_any_workstation", "workstations", "over_privileged_workstations",
                              "dataset_id", "authorized_incident_ids")


def _backend_view(settings: Settings, src: EvidenceSource | None, source_error: str | None,
                  ping: dict | None, *, admin: bool, mode: str | None = None) -> dict:
    mode = mode or default_mode(settings)
    out: dict[str, Any] = {"configured": "spacetimedb" if mode == "live" else "fixture", "mode": mode}
    if src is None:
        out.update({"reachable": False, "error": source_error or "evidence_backend_unavailable"})
        return out
    prov = src.provenance
    out.update({"backend": prov.backend, "live": prov.live, "label": prov.label})
    ping = ping or {}
    out["reachable"] = bool(ping.get("reachable"))
    out.update({k: ping[k] for k in (_PING_ADMIN if admin else _PING_PUBLIC) if k in ping})
    if not out["reachable"]:
        out["error"] = ping.get("error") or "evidence_backend_unreachable"
    if admin:
        out["detail"] = prov.detail
    return out


def _short(x: object) -> str:
    return repr(str(x)[:300])  # repr keeps backend text on one log line


# --------------------------------------------------------------------- app
def create_app(settings: Settings | None = None, *, source: EvidenceSource | None = None,
               client: OllamaClient | None = None, fixture_source: EvidenceSource | None = None) -> FastAPI:
    """`source` is the default evidence source (its mode follows its provenance); a
    second, fixture source is added when NI_FIXTURE_ENABLED=1 or `fixture_source` is given."""
    settings = settings or Settings.from_env()
    state: dict[str, Any] = {}

    async def _drain_on_shutdown() -> None:
        state["closing"] = True
        tasks = list(state["tasks"])
        for t in tasks:
            t.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    log.error("investigation task ended with %s during shutdown", type(t.exception()).__name__)
            if pending:
                log.error("%d investigation task(s) did not stop within %.0f s", len(pending), SHUTDOWN_GRACE_S)
        # Runs whose task never got to its handler (e.g. cancelled before it started).
        n = state["store"].interrupt_active("service_shutdown")
        if n:
            log.warning("finished %d queued investigation(s) as interrupted at shutdown", n)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["settings"] = settings
        state["store"] = InvestigationStore(settings.store_dir)
        state["client"] = client or OllamaClient(settings.ollama_url, settings.model)
        state["sources"], state["source_errors"] = {}, {}
        state["default_mode"] = _mode_of(source) if source is not None else default_mode(settings)
        wanted = {state["default_mode"]}
        if settings.fixture_enabled or fixture_source is not None:
            wanted.add("fixture")
        for mode in sorted(wanted):
            try:
                if mode == state["default_mode"] and source is not None:
                    src = source
                elif mode == "fixture" and fixture_source is not None:
                    src = fixture_source
                else:
                    src = build_source(settings, mode)
                if _mode_of(src) != mode:  # a source must be exactly what its mode says
                    raise ValueError(f"source provenance does not match evidence mode {mode}")
                state["sources"][mode] = src
            except (EvidenceUnavailable, OSError, ValueError, KeyError, TypeError) as e:
                log.error("%s evidence backend unavailable at startup: %s %s", mode, type(e).__name__, _short(e))
                state["source_errors"][mode] = "evidence_backend_init_failed"
        state["source"] = state["sources"].get(state["default_mode"])  # default source
        state["source_error"] = state["source_errors"].get(state["default_mode"])
        state["sem"] = asyncio.Semaphore(1)
        state["tasks"] = set()
        state["limiter"] = _RateLimiter(CREATE_RATE_LIMIT, CREATE_RATE_WINDOW_S)
        state["health_cache"] = {}
        state["health_lock"] = asyncio.Lock()
        state["closing"] = False
        state["keys"] = KeyStore(settings.api_keys_file)  # last: the guard answers 503 until ready
        try:
            yield
        finally:
            await _drain_on_shutdown()

    app = FastAPI(title="LOCKI AIRLOCK Nemotron Investigator", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestGuard, keys=lambda: state.get("keys"))

    async def principal(request: Request) -> ApiPrincipal:
        p = request.scope.get(_PRINCIPAL_KEY)
        if not isinstance(p, ApiPrincipal):  # the guard always sets it; fail closed if it did not run
            raise HTTPException(status_code=401, detail="missing or invalid API key",
                                headers={"WWW-Authenticate": "Bearer"})
        return p

    def need(p: ApiPrincipal, scope: str):
        if not p.has(scope):
            raise HTTPException(status_code=403, detail=f"API key lacks scope {scope}")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # never leak internals
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    async def assert_live_principal(src: EvidenceSource) -> None:
        if not src.provenance.live:
            return
        check = getattr(src, "assert_investigator_authorized", None)
        if not callable(check):
            raise EvidenceUnavailable("live principal verification is unavailable")
        await asyncio.wait_for(check(), timeout=settings.tool_timeout_s)

    async def _run(inv_id: str, body: InvestigationIn, p: ApiPrincipal, mode: str) -> None:
        store: InvestigationStore = state["store"]
        s: Settings = state["settings"]
        sem: asyncio.Semaphore = state["sem"]
        src: EvidenceSource = state["sources"][mode]
        t_accepted = time.perf_counter()
        queue_wait_ms: float | None = None
        acquired = False

        def terminal(status: str, error: str) -> dict:
            return {**evidence_mode_fields(src.provenance), "investigation_id": inv_id,
                    "incident_id": body.incident_id, "requester_id": p.requester_id,
                    "model_id": s.model, "investigation_status": status, "errors": [error],
                    "tool_execution_trace": [], "model_calls": []}

        def finish(result: dict) -> None:
            result["queue_wait_ms"] = (queue_wait_ms if queue_wait_ms is not None
                                       else round((time.perf_counter() - t_accepted) * 1000, 1))
            result["requester_label"] = body.requester.model_dump() if body.requester else None
            store.complete(inv_id, result)

        try:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=min(MAX_QUEUE_WAIT_S, s.total_timeout_s))
                acquired = True
            except TimeoutError:
                finish(terminal("timeout", "queue_timeout"))
                return
            queue_wait_ms = round((time.perf_counter() - t_accepted) * 1000, 1)
            # The key may have been revoked or changed while the run was queued.
            current = state["keys"].resolve_digest(p.key_digest)
            if current is None or current.requester_id != p.requester_id or not current.has("investigations:create"):
                finish(terminal("rejected", "api_key_revoked"))
                return
            try:
                await assert_live_principal(src)
            except Exception:  # no evidence or inference before the live principal is verified
                finish(terminal("failed", "live_principal_not_verified"))
                return
            store.mark_running(inv_id, allowed_workstations=current.allowed_workstations,
                               queue_wait_ms=queue_wait_ms)
            req = Requester(current.requester_id, current.allowed_workstations)
            try:
                result = await asyncio.wait_for(
                    investigate(body.incident_id, req, src, state["client"], s,
                                investigation_id=inv_id, context_note=(body.context.note if body.context else None),
                                progress=lambda ev: store.progress(inv_id, ev)),
                    timeout=s.total_timeout_s + RUN_GRACE_S)
            except TimeoutError:
                log.error("investigation %s overran its deadline; stopped", inv_id)
                result = terminal("timeout", "run_deadline_exceeded")
            except Exception as e:  # noqa: BLE001
                log.exception("investigation %s crashed", inv_id)
                result = terminal("failed", f"internal: {type(e).__name__}")
            finish(result if isinstance(result, dict) else terminal("failed", "internal: invalid result"))
        except asyncio.CancelledError:
            finish(terminal("interrupted", "service_shutdown"))
            raise
        finally:
            if acquired:
                sem.release()

    @app.post("/investigations", status_code=202)
    async def create(body: InvestigationIn, response: Response, p: ApiPrincipal = Depends(principal)):
        need(p, "investigations:create")
        if state["closing"]:
            raise HTTPException(status_code=503, detail="service is shutting down")
        mode = body.evidence_mode or state["default_mode"]
        if mode not in state["sources"]:
            if mode in state["source_errors"]:
                raise HTTPException(status_code=503, detail=f"{mode} evidence backend unavailable "
                                                            f"({state['source_errors'][mode]})")
            raise HTTPException(status_code=409, detail=f"evidence_mode '{mode}' is not enabled on this server "
                                                        f"(available: {', '.join(sorted(state['sources'])) or 'none'})")
        if incident_id_mode(body.incident_id) != mode:
            raise HTTPException(status_code=422, detail=(
                "incident id does not match evidence_mode: live runs take incident:<32 hex> ids from LOCKI, "
                "fixture runs take incident:fixture-<slug> ids"))
        store: InvestigationStore = state["store"]
        if store.active_for(p.requester_id) >= MAX_ACTIVE_PER_REQUESTER:
            raise HTTPException(status_code=429, detail="too many active investigations for this requester",
                                headers={"Retry-After": "5"})
        wait_s = state["limiter"].retry_after(p.requester_id)
        if wait_s is not None:
            raise HTTPException(status_code=429, detail="investigation creation rate limit exceeded",
                                headers={"Retry-After": str(wait_s)})
        if store.active_total() >= MAX_ACTIVE_TOTAL:
            raise HTTPException(status_code=429, detail="investigation queue is full", headers={"Retry-After": "5"})
        inv_id = f"inv_{uuid.uuid4().hex[:20]}"
        store.create(inv_id, body.incident_id, p.requester_id, allowed_workstations=p.allowed_workstations,
                     evidence_mode=mode)
        state["limiter"].record(p.requester_id)
        task = asyncio.create_task(_run(inv_id, body, p, mode), name=f"investigation {inv_id}")
        state["tasks"].add(task)
        task.add_done_callback(state["tasks"].discard)
        response.headers["X-Evidence-Mode"] = mode
        return {"investigation_id": inv_id, "status": "queued", "evidence_mode": mode,
                "evidence_badge": {"live": "LIVE", "fixture": "TEST DATA"}[mode],
                "links": {"self": f"/investigations/{inv_id}", "voice": f"/investigations/{inv_id}?view=voice"}}

    @app.get("/investigations/{investigation_id}")
    async def read(investigation_id: str, response: Response,
                   view: str = Query(default="full", pattern="^(full|voice)$"),
                   p: ApiPrincipal = Depends(principal)):
        need(p, "investigations:read")
        if not INVESTIGATION_ID.fullmatch(investigation_id):
            raise HTTPException(status_code=404, detail="not found")
        rec = state["store"].get(investigation_id)
        if rec is None or not _may_read(rec, p):
            raise HTTPException(status_code=404, detail="not found")
        rec.pop("_authz", None)
        mode = record_mode(rec)  # also covers records written before evidence_mode existed
        if mode == "live":
            result = rec.get("result") or {}
            withheld = result.get("live_report_withheld") or "live_report_withheld" in (result.get("errors") or [])
            if rec.get("status") not in _ASSESSED or withheld:
                # Polling status does not publish cached live evidence or progress.
                rec = {k: v for k, v in rec.items() if k in (
                    "investigation_id", "incident_id", "requester_id", "status", "created_at_ms",
                    "started_at_ms", "finished_at_ms", "evidence_mode",
                )}
                rec["progress"] = []
                if result:
                    safe_errors = {"live_principal_not_verified", "api_key_revoked", "queue_timeout",
                                   "run_deadline_exceeded", "service_shutdown", "service_restart"}
                    errors = [e for e in result.get("errors", []) if isinstance(e, str) and e in safe_errors]
                    rec["result"] = {"investigation_status": rec.get("status"), "live_report_withheld": True,
                                     "errors": errors or ["live_report_withheld"]}
                else:
                    rec["result"] = None
            else:
                src = state["sources"].get("live")
                if src is None or not result.get("evidence_scope_sha256"):
                    raise HTTPException(status_code=404, detail="not found")
                try:
                    await assert_live_principal(src)
                    incident = await asyncio.wait_for(src.get_incident(rec["incident_id"]),
                                                      timeout=settings.tool_timeout_s)
                except Exception:
                    raise HTTPException(status_code=503, detail="live evidence authorization unavailable") from None
                if (incident is None or incident.id != rec["incident_id"]
                        or incident.workstation_id != rec.get("workstation_id")
                        or not Requester(p.requester_id, p.allowed_workstations).may_access(incident.workstation_id)
                        or incident_scope_digest(incident) != result["evidence_scope_sha256"]):
                    raise HTTPException(status_code=404, detail="not found")
                result.pop("evidence_scope_sha256", None)
        response.headers["X-Evidence-Mode"] = mode or "unknown"
        if view == "voice":
            return voice_view(rec)
        # Labels on the full record too, while queued/running and for interrupted or older records.
        rec["evidence_mode"] = mode
        rec["evidence_badge"] = _BADGE.get(mode, "UNVERIFIED")
        return rec

    async def _cached(name: str, fetch) -> dict:
        cache = state["health_cache"]
        hit = cache.get(name)
        if hit and time.monotonic() - hit[0] < HEALTH_CACHE_S:
            return hit[1]
        async with state["health_lock"]:
            hit = cache.get(name)
            if hit and time.monotonic() - hit[0] < HEALTH_CACHE_S:
                return hit[1]
            value = await fetch()
            cache[name] = (time.monotonic(), value)
            return value

    async def _ping_backend(src: EvidenceSource) -> dict:
        try:
            out = await asyncio.wait_for(src.ping(), timeout=state["settings"].tool_timeout_s)
        except TimeoutError:
            log.warning("evidence backend ping timed out")
            return {"reachable": False, "error": "evidence_backend_timeout"}
        except Exception as e:  # noqa: BLE001
            log.warning("evidence backend ping failed: %s %s", type(e).__name__, _short(e))
            return {"reachable": False, "error": "evidence_backend_ping_failed"}
        if not isinstance(out, dict):
            return {"reachable": False, "error": "evidence_backend_bad_ping"}
        if not out.get("reachable"):
            if out.get("error"):
                log.warning("evidence backend unreachable: %s", _short(out["error"]))
            out = {**out, "error": "evidence_backend_unreachable"}
        return out

    async def _ollama_health() -> dict:
        try:
            return await state["client"].health()
        except Exception as e:  # noqa: BLE001
            log.warning("ollama health check failed: %s %s", type(e).__name__, _short(e))
            return {"reachable": False, "model_installed": False, "error": "ollama_health_failed"}

    @app.get("/health")
    async def health(p: ApiPrincipal = Depends(principal)):
        admin = p.has("health:admin")
        if not (admin or p.has("health:read")):
            raise HTTPException(status_code=403, detail="API key lacks scope health:read")
        s: Settings = state["settings"]
        store: InvestigationStore = state["store"]
        oll = await _cached("ollama", _ollama_health)
        backends: dict[str, dict] = {}
        for mode in sorted(set(state["sources"]) | set(state["source_errors"])):
            src = state["sources"].get(mode)
            ping = await _cached(f"evidence:{mode}", lambda src=src: _ping_backend(src)) if src is not None else None
            backends[mode] = _backend_view(s, src, state["source_errors"].get(mode), ping, admin=admin, mode=mode)
        backend = backends.get(state["default_mode"]) or _backend_view(s, None, state["source_error"], None,
                                                                       admin=admin, mode=state["default_mode"])
        degraded = []
        if not oll.get("reachable"):
            degraded.append("ollama_unreachable")
        elif not oll.get("model_installed"):
            degraded.append("model_not_installed")
        if not backend.get("reachable"):
            degraded.append("evidence_backend_unreachable")
        if state["default_mode"] == "fixture":
            degraded.append("evidence_backend_is_fixture_not_live")
        gate: dict[str, Any] = {"enabled": False}
        if s.relay_gate:
            try:
                from .relay_gate import get_gate
                g = get_gate(s.relay_max_usb_lookback_s)
                gate = {"enabled": True, "label": g.label}
                if admin:
                    gate.update({"decisions": len(g.decisions),
                                 "blocked": sum(1 for d in g.decisions if not d["allowed"])})
            except Exception as e:  # noqa: BLE001
                log.warning("policy gate unavailable: %s %s", type(e).__name__, _short(e))
                gate = {"enabled": True, "error": "policy_gate_unavailable"}
                degraded.append("policy_gate_unavailable")
        if "live" not in state["sources"]:
            degraded.append("live_evidence_not_configured" if "live" not in state["source_errors"]
                            else "live_evidence_backend_init_failed")
        elif backends["live"].get("reachable") and "authorized_incidents" in backends["live"]:
            if not backends["live"]["authorized_incidents"]:
                degraded.append("live_evidence_has_no_incident_grant")   # the owner has not granted any incident yet
        elif backends["live"].get("reachable") and not backends["live"].get("memberships"):
            degraded.append("live_evidence_has_no_workstation_membership")
        if "live" in backends and backends["live"].get("least_privilege_ok") is False:
            # The investigator identity holds approval/owner rights: it must be read-only. Not informational.
            degraded.append("live_evidence_identity_over_privileged")
        informational = {"evidence_backend_is_fixture_not_live", "live_evidence_not_configured"}
        out = {"policy_gate": gate,
               "service": "ok" if not [d for d in degraded if d not in informational]
               else "degraded", "version": __version__, "model_id": s.model, "ollama": oll,
               "evidence_modes": {"default": state["default_mode"], "available": sorted(state["sources"])},
               "evidence_backend": backend, "evidence_backends": backends, "degraded": degraded,
               "investigations": store.counts(p.requester_id),
               "bind": f"{s.bind_host}:{s.bind_port}"}
        if admin:
            out["investigations_all"] = store.counts()
            out["queue"] = {"active": store.active_total(), "max_active": MAX_ACTIVE_TOTAL,
                            "max_active_per_requester": MAX_ACTIVE_PER_REQUESTER}
        return out

    return app


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def serve() -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    s = Settings.from_env()
    if s.evidence_backend not in ("spacetimedb", "fixture"):
        raise SystemExit(f"NI_EVIDENCE_BACKEND must be 'spacetimedb' (live) or 'fixture', not {s.evidence_backend!r}")
    if not _is_loopback(s.bind_host) and os.environ.get("NI_ALLOW_NON_LOOPBACK") != "1":
        raise SystemExit(f"refusing to bind {s.bind_host}: set NI_ALLOW_NON_LOOPBACK=1 to expose beyond loopback "
                         "(coordinate with Dhruv; prefer an SSH local forward instead)")
    uvicorn.run(create_app(s), host=s.bind_host, port=s.bind_port, log_level="info", access_log=True,
                limit_concurrency=UVICORN_LIMIT_CONCURRENCY)
    return 0
