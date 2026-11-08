"""Approved READ-ONLY investigation tools that Nemotron may call.

Hard guarantees enforced here in code (never delegated to the prompt):
  * Only the tools in REGISTRY exist. Anything else the model names is rejected.
  * Arguments are validated by strict pydantic models (extra fields forbidden,
    bounded integers, incident ids must match `incident:<32 hex>`).
  * Every tool is scoped to the ONE incident authorized for this investigation;
    a call naming any other incident is denied without touching the backend.
    Session reads are additionally bound to the incident's workstation.
  * Tools only read through the EvidenceSource interface, which exposes no
    write, shell, filesystem, URL or raw-query capability.
  * Results are size-bounded; every forwarded string is cleaned, bounded and
    scanned; externally chosen ids that do not fit a strict charset are shown
    as opaque placeholders.
  * Evidence becomes citable only if a call SUCCEEDED and only for ids that were
    actually present in the content shown to the model (transactional registry).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import categories as cat
from . import relay_gate as _relay
from .contracts import DeviceEventRow, DeviceHealthRow, IncidentRow, SessionRow
from .evidence.base import EvidenceContractError, EvidenceSource, EvidenceUnavailable
from .sanitize import clean_text, looks_like_injection, primitive, safe_id

log = logging.getLogger("nemotron_investigator.tools")

# Two disjoint incident-id spaces, so a fixture-backed run can never be mistaken
# for (or run against) a real incident:
#   live    incident:<32 lowercase hex>     ids minted by the LOCKI SpacetimeDB module
#   fixture incident:fixture-<slug>         ids that exist only in fixtures/scenarios
LIVE_INCIDENT_ID_PATTERN = r"^incident:[0-9a-f]{32}$"
FIXTURE_INCIDENT_ID_PATTERN = r"^incident:fixture-[a-z0-9][a-z0-9-]{0,30}$"
INCIDENT_ID_PATTERN = r"^incident:(?:[0-9a-f]{32}|fixture-[a-z0-9][a-z0-9-]{0,30})$"  # either space (syntax only)
AFTER_INCIDENT_MS = 60_000      # windowed tools also look 60 s past the incident
CLOCK_SKEW_FLAG_S = 30.0        # |device clock - server receive time| above this is flagged
CAMERA_STALE_S = 600            # a baseline snapshot older than this cannot vouch for the camera
_BARE_HEX = re.compile(r"^[0-9a-f]{32}$")
_BARE_FIXTURE = re.compile(r"^fixture-[a-z0-9][a-z0-9-]{0,30}$")
_MAX_ISO_MS = 253402300799999   # 9999-12-31T23:59:59.999Z


def incident_id_mode(incident_id: str) -> str | None:
    """'live' or 'fixture' for a well-formed incident id, else None."""
    if not isinstance(incident_id, str):
        return None
    if re.fullmatch(LIVE_INCIDENT_ID_PATTERN, incident_id):
        return "live"
    if re.fullmatch(FIXTURE_INCIDENT_ID_PATTERN, incident_id):
        return "fixture"
    return None


def iso(ms: int | None) -> str | None:
    """Total ISO-8601 formatter: None for 0/None/out-of-range values (never raises)."""
    if not ms or not isinstance(ms, (int, float)) or ms < 0 or ms > _MAX_ISO_MS:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (ValueError, OverflowError, OSError):
        return None


# --------------------------------------------------------------------------- args
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IncidentArgs(_Args):
    # Optional: every tool is already scoped to the authorized incident. If given,
    # it must equal that incident (canonicalized first, see canonicalize_args).
    incident_id: str | None = Field(default=None, pattern=INCIDENT_ID_PATTERN, max_length=48)


class WindowArgs(IncidentArgs):
    window_s: int = Field(default=300, ge=1, le=1800)
    limit: int = Field(default=10, ge=1, le=20)


# ------------------------------------------------------------------------ context
@dataclass
class ToolContext:
    investigation_id: str
    incident: IncidentRow                 # authorized during preflight
    source: EvidenceSource
    max_result_chars: int = 2200
    evidence_index: dict[str, dict] = field(default_factory=dict)   # COMMITTED evidence_id -> record meta
    timeline: dict[tuple, dict] = field(default_factory=dict)       # COMMITTED dedup key -> timeline entry
    injection_flags: list[dict] = field(default_factory=list)       # COMMITTED
    untrusted_values: set[str] = field(default_factory=set)         # COMMITTED attacker-controllable strings shown
    signals: dict[str, Any] = field(default_factory=dict)           # deterministic facts derived by tools
    relay_gate: Any = None                                          # optional NeMo Relay policy gate
    _p_index: dict[str, dict] = field(default_factory=dict)
    _p_timeline: dict[tuple, dict] = field(default_factory=dict)
    _p_flags: list[dict] = field(default_factory=list)
    _p_signals: dict[str, Any] = field(default_factory=dict)
    _p_untrusted: set[str] = field(default_factory=set)

    @property
    def t0(self) -> int:
        return self.incident.created_at_ms

    def rel_s(self, ms: int) -> float:
        return round((ms - self.t0) / 1000, 1)

    # -- transactional registry ------------------------------------------------
    def begin(self) -> None:
        self._p_index, self._p_timeline, self._p_flags, self._p_signals = {}, {}, [], {}
        self._p_untrusted = set()

    def register(self, evidence_id: str, etype: str, t_ms: int | None, label: str, **extra) -> None:
        self._p_index.setdefault(evidence_id, {"type": etype, "t_ms": t_ms, "label": label, **extra})
        if t_ms and iso(t_ms):
            self._p_timeline.setdefault((evidence_id, etype, t_ms), {
                "t_ms": t_ms, "time": iso(t_ms), "t_rel_s": self.rel_s(t_ms),
                "evidence_id": evidence_id, "type": etype, "label": label, **extra})

    def timeline_extra(self, key: tuple, entry: dict) -> None:
        self._p_timeline.setdefault(key, entry)

    def flag(self, evidence_id: str, field_name: str, text: str) -> None:
        self._p_flags.append({"evidence_id": evidence_id, "field": field_name, "text": text})

    def signal(self, key: str, value: Any) -> None:
        self._p_signals[key] = value

    def untrusted(self, text: Any) -> None:
        """Record an attacker-controllable string (device/app name, summary, detail) shown to the model."""
        if isinstance(text, str) and len(text.strip()) >= 4:
            self._p_untrusted.add(text.strip())

    def commit(self, visible: str) -> list[str]:
        """Commit pending evidence whose id is present in the content shown to the model."""
        shown = [eid for eid in self._p_index if f'"{eid}"' in visible]
        for eid in shown:
            self.evidence_index.setdefault(eid, self._p_index[eid])
        keep = set(shown)
        for k, entry in self._p_timeline.items():
            if entry["evidence_id"] in keep:
                self.timeline.setdefault(k, entry)
        # A flag is committed when the flagged text itself reached the model.
        self.injection_flags.extend(f for f in self._p_flags
                                    if json.dumps(f["text"], ensure_ascii=False)[1:-1] in visible)
        self.untrusted_values.update(t for t in self._p_untrusted
                                     if json.dumps(t, ensure_ascii=False)[1:-1] in visible)
        for k, v in self._p_signals.items():
            if k.endswith("_events_in_window"):
                self.signals[k] = max(v, self.signals.get(k, 0))
            else:
                self.signals[k] = v
        self.begin()
        return shown

    def rollback(self) -> None:
        self.begin()


class ToolFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------------- views
def _scan(ctx: ToolContext, evidence_id: str, field_name: str, text: str, flags: list[str]) -> None:
    if text and looks_like_injection(text):
        flags.append(f"instruction_like_text_in_{field_name}")
        ctx.flag(evidence_id, field_name, text)


def _sid(ctx: ToolContext, raw: str, flags: list[str] | None = None) -> str:
    shown, ok = safe_id(raw)
    if not ok and flags is not None:
        flags.append("unsafe_identifier_replaced")
    return shown


def _norm_hexid(v: Any) -> str | None:
    """Normalize USB vendor/product ids: int or hex string, any case, with or without 0x."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return f"0x{v:04x}" if 0 <= v <= 0xFFFF else None
    if isinstance(v, str):
        s = v.strip().lower()
        s = s[2:] if s.startswith("0x") else s
        if re.fullmatch(r"[0-9a-f]{1,4}", s):
            return f"0x{int(s, 16):04x}"
    return None


def _event_time(ev: DeviceEventRow) -> tuple[int, float, bool]:
    """(time_ms used for display/order, skew_s, skew_flag).

    Windows are selected on the server clock (received_at_ms). The device clock
    (observed_at_ms) is the better physical time when the two agree; when they
    disagree by more than CLOCK_SKEW_FLAG_S we fall back to the server clock and flag it.
    """
    skew = (ev.observed_at_ms - ev.received_at_ms) / 1000 if ev.received_at_ms else 0.0
    if ev.received_at_ms and abs(skew) > CLOCK_SKEW_FLAG_S:
        return ev.received_at_ms, round(skew, 1), True
    return ev.observed_at_ms or ev.received_at_ms, round(skew, 1), False


def event_view(ctx: ToolContext, ev: DeviceEventRow) -> dict:
    category = cat.categorize(ev)
    meta = ev.metadata()
    flags: list[str] = []
    eid = _sid(ctx, ev.id, flags)
    details: dict[str, Any] = {}
    for k in cat.STRUCTURED_KEYS.get(category, ()):
        if k in meta:
            v = primitive(meta[k])
            if v is not None:
                details[k] = v
                if isinstance(v, str):
                    _scan(ctx, eid, k, v, flags)
    untrusted: dict[str, str] = {}
    for k in cat.UNTRUSTED_TEXT_KEYS.get(category, ()):
        if k in meta and isinstance(meta[k], (str, int, float)) and not isinstance(meta[k], bool):
            t = clean_text(meta[k], 100)
            untrusted[k] = t
            ctx.untrusted(t)
            _scan(ctx, eid, k, t, flags)
    for k, v in meta.items():  # scan keys we do not forward, too
        if k not in details and k not in untrusted and isinstance(v, str):
            _scan(ctx, eid, clean_text(k, 24), clean_text(v, 160), flags)
    source, kind = clean_text(ev.source, 32), clean_text(ev.kind, 48)
    for name, val in (("source", source), ("kind", kind), ("classification", clean_text(ev.classification, 80))):
        _scan(ctx, eid, name, val, flags)
    t_ms, skew, skewed = _event_time(ev)
    if skewed:
        flags.append("clock_skew_detected")
    if iso(t_ms) is None:
        flags.append("timestamp_out_of_range")
    view: dict[str, Any] = {
        "evidence_id": eid,
        "t_rel_s": ctx.rel_s(t_ms) if iso(t_ms) else None,
        "time": iso(t_ms),
        "category": category,
        "source": source,
        "kind": kind,
        "confidence": round(float(ev.confidence), 3) if ev.confidence == ev.confidence else None,
    }
    if abs(skew) > 2:
        view["clock_skew_s"] = skew
    if ev.classification:
        view["classification"] = clean_text(ev.classification, 48)
    if details:
        view["details"] = details
    if untrusted:
        view["untrusted_text"] = untrusted
    if ev.simulated:
        view["simulated"] = True
        ctx.signal("simulated_rows_seen", True)
    if flags:
        view["flags"] = sorted(set(flags))
    label = f"{category}: {kind}"
    name = untrusted.get("product_name") or untrusted.get("app_name")
    if name:
        label += f" ({name})"
    ctx.register(eid, "device_event", t_ms, label, category=category, event_kind=kind)
    return view


def _json_list(raw: str) -> list | None:
    try:
        v = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, list) else None


def _json_obj(raw: str) -> dict | None:
    try:
        v = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def _armed_at(sess: SessionRow, t_ms: int) -> bool:
    if not sess.armed_at_ms or sess.armed_at_ms > t_ms:
        return False
    if sess.disarmed_at_ms and sess.disarmed_at_ms <= t_ms:
        return False
    if sess.expires_at_ms and sess.expires_at_ms <= t_ms:
        return False
    return True


def _known_usb_devices(baseline: dict | None) -> tuple[list[dict], str]:
    """(devices, parse_state) with parse_state in recognized | unrecognized | absent."""
    if baseline is None:
        return [], "unrecognized"
    if not baseline:
        return [], "absent"
    for key in ("usb_devices", "known_usb_devices", "usb", "known_usb"):
        v = baseline.get(key)
        if isinstance(v, list):
            return [d for d in v if isinstance(d, dict)], "recognized"
    return [], "unrecognized"


def _usb_match(meta: dict, known: list[dict]) -> str | None:
    """serial_hash | vendor_product_only | serial_mismatch | None.

    VID/PID are attacker-chosen descriptor fields, so a VID/PID match only counts
    when the event carries no serial. A different serial on the same VID/PID as a
    baseline device is a contradiction, not a match.
    """
    serial = meta.get("serial_hash")
    if serial and any(d.get("serial_hash") == serial for d in known):
        return "serial_hash"
    vid, pid = _norm_hexid(meta.get("vendor_id")), _norm_hexid(meta.get("product_id"))
    same = [d for d in known if vid and pid and _norm_hexid(d.get("vendor_id")) == vid
            and _norm_hexid(d.get("product_id")) == pid]
    if not same:
        return None
    if serial and any(d.get("serial_hash") for d in same):
        return "serial_mismatch"
    return "vendor_product_only"


def _approved_apps(sess: SessionRow | None) -> tuple[list[str] | None, str]:
    """(raw approved bundle ids or None when unknown, source)."""
    if sess is None:
        return None, "session_not_found"
    items = _json_list(sess.approved_apps_json)
    if items is None:
        return None, "unrecognized"
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("bundle_id"), str):
            out.append(item["bundle_id"])
        else:
            return None, "unrecognized"
    return out, "session"


async def _session(ctx: ToolContext) -> SessionRow | None:
    """Session lookup bound to the incident's workstation (fails closed on mismatch)."""
    if not ctx.incident.session_id:
        return None
    sess = await ctx.source.get_session(ctx.incident.workstation_id, ctx.incident.session_id)
    if sess is not None and sess.workstation_id != ctx.incident.workstation_id:
        ctx.signal("session_workstation_mismatch", True)
        return None
    return sess


# -------------------------------------------------------------------------- tools
async def tool_get_incident(ctx: ToolContext, args: IncidentArgs):
    inc = await ctx.source.get_incident(ctx.incident.id)
    if inc is None:
        raise ToolFailure("not_found", "incident not found")
    flags: list[str] = []
    summary = clean_text(inc.summary, 200)
    ctx.untrusted(summary)
    _scan(ctx, inc.id, "summary", summary, flags)
    linked = [_sid(ctx, i, flags) for i in inc.linked_event_ids()[:12]]
    data = {
        "evidence_id": inc.id,
        "workstation_id": _sid(ctx, inc.workstation_id, flags),
        "session_id": _sid(ctx, inc.session_id, flags) if inc.session_id else None,
        "rule_id": clean_text(inc.rule_id, 48),
        "severity": clean_text(inc.severity, 16),
        "state": clean_text(inc.state, 24),
        "created_at": iso(inc.created_at_ms),
        "action_required": inc.action_required,
        "acknowledged": bool(inc.acknowledged_by),
        "resolved": bool(inc.resolved_by),
        "simulated": inc.simulated,
        "linked_event_ids": linked,
        "summary_untrusted": summary,
    }
    for name in ("rule_id", "severity", "state"):
        _scan(ctx, inc.id, name, data[name], flags)
    if flags:
        data["flags"] = sorted(set(flags))
    if inc.simulated:
        ctx.signal("simulated_rows_seen", True)
    ctx.register(inc.id, "incident", inc.created_at_ms,
                 f"incident raised: rule={data['rule_id']} severity={data['severity']}")
    return data, [inc.id], f"incident {data['rule_id']}/{data['severity']}, {len(linked)} linked events"


async def tool_get_session_status(ctx: ToolContext, args: IncidentArgs):
    sess = await _session(ctx)
    if sess is None:
        raise ToolFailure("not_found", "no session record found for this incident's session on its workstation")
    armed = _armed_at(sess, ctx.t0)
    notes = []
    if sess.armed_at_ms:
        r = ctx.rel_s(sess.armed_at_ms)
        notes.append(f"armed {abs(r):g} s {'before' if r <= 0 else 'after'} the incident")
    notes.append("ARMED at incident time" if armed else "NOT armed at incident time")
    if sess.disarmed_at_ms:
        r = ctx.rel_s(sess.disarmed_at_ms)
        notes.append(f"disarmed {abs(r):g} s {'after' if r > 0 else 'before'} the incident")
    apps, apps_source = _approved_apps(sess)
    sid = _sid(ctx, sess.id)
    data = {
        "evidence_id": sid,
        "armed_at_incident_time": armed,
        "timing_note": "; ".join(notes),
        "state_now": clean_text(sess.state, 24),
        "armed_at": iso(sess.armed_at_ms),
        "armed_t_rel_s": ctx.rel_s(sess.armed_at_ms) if iso(sess.armed_at_ms) else None,
        "disarmed_at": iso(sess.disarmed_at_ms),
        "disarmed_t_rel_s": ctx.rel_s(sess.disarmed_at_ms) if iso(sess.disarmed_at_ms) else None,
        "expires_at": iso(sess.expires_at_ms),
        "auto_lock_enabled": sess.auto_lock_enabled,
        "approved_app_count": len(apps) if apps is not None else None,
        "approved_apps_source": apps_source,
        "simulated": sess.simulated,
    }
    ctx.signal("armed_at_incident_time", armed)
    if sess.simulated:
        ctx.signal("simulated_rows_seen", True)
    ctx.register(sid, "session", sess.armed_at_ms or None, "session armed")
    if iso(sess.disarmed_at_ms):
        ctx.timeline_extra((sid, "session_disarmed", sess.disarmed_at_ms), {
            "t_ms": sess.disarmed_at_ms, "time": iso(sess.disarmed_at_ms),
            "t_rel_s": ctx.rel_s(sess.disarmed_at_ms), "evidence_id": sid,
            "type": "session", "label": "session disarmed"})
    return data, [sid], f"session {'ARMED' if armed else 'NOT armed'} at incident time"


async def tool_get_incident_evidence(ctx: ToolContext, args: IncidentArgs):
    raw_ids = ctx.incident.linked_event_ids()[:20]
    if not raw_ids:
        ctx.signal("linked_events_missing", 0)
        return ({"linked_event_ids": [], "events": [], "missing_event_ids": [],
                 "note": "incident record links no event ids"}, [], "incident links no events")
    rows = await ctx.source.get_device_events_by_ids(ctx.incident.workstation_id, raw_ids)
    rows = sorted(rows, key=lambda e: (_event_time(e)[0], e.seq))
    found = {r.id for r in rows}
    missing_raw = [i for i in raw_ids if i not in found]
    flags: list[str] = []
    events = [event_view(ctx, r) for r in rows]
    data = {"linked_event_ids": [_sid(ctx, i, flags) for i in raw_ids], "events": events,
            "missing_event_ids": [_sid(ctx, i, flags) for i in missing_raw]}
    ctx.signal("linked_events_missing", len(missing_raw))
    if missing_raw:
        data["note"] = "some linked events could not be retrieved (deleted, expired, not visible or unqueryable)"
    if flags:
        data["flags"] = sorted(set(flags))
    return data, [e["evidence_id"] for e in events], f"{len(rows)}/{len(raw_ids)} linked events retrieved"


def _window_tool(category: str, label: str):
    async def handler(ctx: ToolContext, args: WindowArgs):
        since = max(0, ctx.t0 - args.window_s * 1000)
        until = ctx.t0 + AFTER_INCIDENT_MS
        rows = await ctx.source.list_device_events(ctx.incident.workstation_id, since, until)
        rows = [r for r in rows if cat.categorize(r) == category]
        total = len(rows)
        closest = sorted(rows, key=lambda e: abs(_event_time(e)[0] - ctx.t0))[: args.limit]
        closest.sort(key=lambda e: (_event_time(e)[0], e.seq))
        extra: dict[str, Any] = {}
        events = [event_view(ctx, r) for r in closest]
        if category == cat.APPLICATION:
            sess = await _session(ctx)
            approved, src = _approved_apps(sess)
            extra["approved_apps_source"] = src
            extra["approved_apps"] = [clean_text(a, 64) for a in approved[:15]] if approved is not None else None
            if approved is None:
                extra["approved_apps_note"] = ("the session's approved-app list is unavailable, so approval "
                                               "of these apps is unknown")
            raw_by_id = {r.id: r.metadata().get("bundle_id") for r in closest}
            for ev, r in zip(events, closest):
                raw_bid = raw_by_id.get(r.id)
                if not isinstance(raw_bid, str):
                    continue
                if approved is None:
                    ev["in_session_approved_apps"] = None
                elif clean_text(raw_bid, 64) != raw_bid:
                    # Approval is an EXACT match on the raw bundle id; text that changes
                    # under normalization (zero-width, fullwidth, overlong) never matches.
                    ev["in_session_approved_apps"] = False
                    ev.setdefault("flags", []).append("bundle_id_altered_by_normalization")
                else:
                    ev["in_session_approved_apps"] = raw_bid in approved
        data = {
            "window": {"from_t_rel_s": -float(args.window_s), "to_t_rel_s": AFTER_INCIDENT_MS / 1000},
            "total_in_window": total,
            "returned": len(events),
            "truncated": total > len(events),
            "events": events,
            **extra,
        }
        ctx.signal(f"{category}_events_in_window", total)
        if total == 0:
            data["note"] = f"no {label} events recorded in this window; cite this result's result_id for the absence"
        return data, [e["evidence_id"] for e in events], f"{len(events)} {label} events (of {total})"
    return handler


def _camera_status(ctx: ToolContext, health: list[DeviceHealthRow], latest) -> tuple[str, str, float | None]:
    """(status, source, age_s). Device health at incident time beats a stale snapshot."""
    vis = [h for h in health if h.component.strip().lower() in ("vision", "camera", "cv", "jetson_cv")]
    if vis:
        h = max(vis, key=lambda x: x.last_seen_ms)
        age = round((ctx.t0 - h.last_seen_ms) / 1000, 1) if h.last_seen_ms else None
        return clean_text(h.status, 24), "device_health", age
    if latest is not None:
        age = round((ctx.t0 - latest.observed_at_ms) / 1000, 1)
        status = clean_text(latest.camera_status, 24)
        if age > CAMERA_STALE_S:
            return f"stale:{status}", "baseline_snapshot", age
        return status, "baseline_snapshot", age
    return "unknown", "none", None


async def tool_get_device_baseline_status(ctx: ToolContext, args: IncidentArgs):
    sess = await _session(ctx)
    snaps = await ctx.source.list_baseline_snapshots(ctx.incident.workstation_id, ctx.t0 + AFTER_INCIDENT_MS)
    latest = max(snaps, key=lambda s: s.observed_at_ms) if snaps else None
    health = await ctx.source.list_device_health(ctx.incident.workstation_id)
    baseline = _json_obj(sess.baseline_json) if sess else {}
    known, parse = _known_usb_devices(baseline)
    baseline_source = "session"
    if parse != "recognized" and latest is not None:
        snap_known, snap_parse = _known_usb_devices(_json_obj(latest.baseline_json))
        if snap_parse == "recognized":
            known, parse, baseline_source = snap_known, snap_parse, "snapshot"
    if parse != "recognized":
        baseline_source = "none"
    usb_rows = await ctx.source.list_device_events(
        ctx.incident.workstation_id, max(0, ctx.t0 - 300_000), ctx.t0 + AFTER_INCIDENT_MS)
    usb_rows = [r for r in usb_rows if cat.categorize(r) == cat.USB]
    comparisons = []
    evidence_ids: list[str] = []
    flags: list[str] = []
    for r in sorted(usb_rows, key=lambda e: abs(_event_time(e)[0] - ctx.t0))[:6]:
        meta = r.metadata()
        if parse != "recognized":
            in_base, match = None, "baseline_" + parse
        else:
            match = _usb_match(meta, known)
            in_base = match in ("serial_hash", "vendor_product_only")
        view = event_view(ctx, r)  # registers it for citation and timeline
        pname = clean_text(meta.get("product_name", ""), 60)
        ctx.untrusted(pname)
        comparisons.append({
            "evidence_id": view["evidence_id"],
            "kind": view["kind"],
            "product_name_untrusted": pname,
            "vendor_id": _norm_hexid(meta.get("vendor_id")),
            "product_id": _norm_hexid(meta.get("product_id")),
            "in_baseline": in_base,
            "matched_on": match,
        })
        evidence_ids.append(view["evidence_id"])
    comp_health = []
    for h in sorted(health, key=lambda h: h.component)[:8]:
        hf: list[str] = []
        hid = _sid(ctx, h.id, hf)
        detail = clean_text(h.detail, 80)
        ctx.untrusted(detail)
        comp, status = clean_text(h.component, 32), clean_text(h.status, 24)
        for name, val in (("health_detail", detail), ("health_component", comp), ("health_status", status)):
            _scan(ctx, hid, name, val, hf)
        comp_health.append({
            "evidence_id": hid, "component": comp, "status": status,
            "last_seen_t_rel_s": ctx.rel_s(h.last_seen_ms) if iso(h.last_seen_ms) else None,
            "detail_untrusted": detail, **({"flags": sorted(set(hf))} if hf else {})})
        ctx.register(hid, "device_health", h.last_seen_ms or None, f"health: {comp}={status}")
        evidence_ids.append(hid)
    cam, cam_src, cam_age = _camera_status(ctx, health, latest)
    data: dict[str, Any] = {
        "baseline_source": baseline_source,
        "baseline_parse": parse,
        "known_usb_device_count": len(known) if parse == "recognized" else None,
        "known_usb_devices": [
            {"name_untrusted": clean_text(d.get("name", d.get("product_name", "")), 48),
             "vendor_id": _norm_hexid(d.get("vendor_id")), "product_id": _norm_hexid(d.get("product_id"))}
            for d in known[:8]],
        "usb_devices_vs_baseline": comparisons,
        "camera_status": cam,
        "camera_status_source": cam_src,
        "camera_status_age_s": cam_age,
        "component_health": comp_health,
    }
    for d in data["known_usb_devices"]:
        ctx.untrusted(d["name_untrusted"])
        _scan(ctx, "baseline", "baseline_device_name", d["name_untrusted"], flags)
    if parse != "recognized":
        data["baseline_note"] = ("no usable USB baseline was recorded for this session, so membership of the "
                                 "devices below is unknown")
    if sess is not None:
        sid = _sid(ctx, sess.id, flags)
        ctx.register(sid, "session", sess.armed_at_ms or None, "session armed")
        evidence_ids.append(sid)
        data["session_evidence_id"] = sid
    if latest is not None:
        snap_id = _sid(ctx, latest.id, flags)
        active = clean_text(latest.active_app_id, 64)
        ctx.untrusted(active)
        _scan(ctx, snap_id, "active_app_id", active, flags)
        data["latest_snapshot"] = {"evidence_id": snap_id, "t_rel_s": ctx.rel_s(latest.observed_at_ms),
                                   "active_app_id": active, "camera_status": clean_text(latest.camera_status, 24)}
        ctx.register(snap_id, "baseline_snapshot", latest.observed_at_ms,
                     f"baseline snapshot (camera={data['latest_snapshot']['camera_status']})")
        evidence_ids.append(snap_id)
    if flags:
        data["flags"] = sorted(set(flags))
    not_in = sum(1 for c in comparisons if c["in_baseline"] is False)
    unknown = sum(1 for c in comparisons if c["in_baseline"] is None)
    ctx.signal("camera_status", cam)
    ctx.signal("camera_status_source", cam_src)
    ctx.signal("usb_events_not_in_baseline", not_in)
    ctx.signal("usb_events_baseline_unknown", unknown)
    ctx.signal("usb_events_in_baseline", sum(1 for c in comparisons if c["in_baseline"] is True))
    ctx.signal("usb_events_matched_vidpid_only", sum(1 for c in comparisons if c["matched_on"] == "vendor_product_only"))
    ctx.signal("usb_serial_mismatch", sum(1 for c in comparisons if c["matched_on"] == "serial_mismatch"))
    known_txt = f"{len(known)} known USB devices" if parse == "recognized" else f"USB baseline {parse}"
    return data, evidence_ids, (f"{known_txt}; {not_in}/{len(comparisons)} nearby USB events not in baseline"
                                f"{f', {unknown} unknown' if unknown else ''}; camera={cam} ({cam_src})")


# ----------------------------------------------------------------------- registry
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[_Args]
    handler: Callable[[ToolContext, Any], Awaitable[tuple[dict, list[str], str]]]
    parameters: dict


_INC = {"incident_id": {"type": "string",
                        "description": "the incident under investigation (incident:<32 hex>); optional"}}
_WIN = {**_INC,
        "window_s": {"type": "integer", "description": "seconds before the incident to search (1-1800, default 300)"},
        "limit": {"type": "integer", "description": "max events to return (1-20, default 10)"}}


def _params(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": []}


REGISTRY: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("get_incident",
             "Read the incident record: rule, severity, state, time, session and linked event ids.",
             IncidentArgs, tool_get_incident, _params(_INC)),
    ToolSpec("get_session_status",
             "Read the armed session for the incident: was it armed at incident time, auto-lock policy.",
             IncidentArgs, tool_get_session_status, _params(_INC)),
    ToolSpec("get_incident_evidence",
             "Read the device events directly linked to the incident (the events that triggered it).",
             IncidentArgs, tool_get_incident_evidence, _params(_INC)),
    ToolSpec("get_recent_usb_events",
             "List USB connect/disconnect events on the workstation around the incident time.",
             WindowArgs, _window_tool(cat.USB, "USB"), _params(_WIN)),
    ToolSpec("get_recent_vision_events",
             "List camera/CV observations (person, face match, hand, desk interaction) around the incident.",
             WindowArgs, _window_tool(cat.VISION, "vision"), _params(_WIN)),
    ToolSpec("get_recent_application_events",
             "List application launch/focus events around the incident, with the session's approved apps.",
             WindowArgs, _window_tool(cat.APPLICATION, "application"), _params(_WIN)),
    ToolSpec("get_device_baseline_status",
             "Compare nearby USB devices against the armed baseline and report camera/sensor health.",
             IncidentArgs, tool_get_device_baseline_status, _params(_INC)),
]}


# Control tool (not an evidence tool): lets the model end the investigation as an
# explicit, logged decision with a reason. It reads and changes nothing.
FINISH_TOOL = "finish_investigation"


class FinishArgs(_Args):
    reason: str = Field(min_length=1, max_length=400)


FINISH_SPEC = {"type": "function", "function": {
    "name": FINISH_TOOL,
    "description": "End the investigation when the remaining tools cannot change the assessment. "
                   "Give a one-sentence reason.",
    "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}}


def ollama_tool_specs() -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.parameters}} for t in REGISTRY.values()] + [FINISH_SPEC]


# ---------------------------------------------------------------------- execution
class ToolExecution(BaseModel):
    step: int
    call_index: int
    tool: str
    arguments_raw: Any
    arguments: dict | None = None
    status: str          # ok | invalid_arguments | unknown_tool | permission_denied | not_found | policy_denied |
                         # evidence_unavailable | contract_error | timeout | duplicate | limit_exceeded |
                         # per_turn_limit | finished | internal_error
    error_code: str | None = None
    error: str | None = None
    started_ms: float
    duration_ms: float
    result_summary: str = ""
    evidence_ids: list[str] = []
    result_chars: int = 0
    truncated_for_context: bool = False
    arguments_normalized: bool = False
    rejected_rows: int = 0
    policy_gate: str | None = None
    backend: str = ""
    live_data: bool = False


def _bound_raw(raw: Any) -> Any:
    s = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    return s if len(s) <= 400 else s[:400] + "...(truncated)"


def _fit(data: dict, max_chars: int) -> tuple[str, bool]:
    """Serialize compactly; drop events farthest from the incident until it fits."""
    s = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    truncated = False
    while len(s) > max_chars:
        lists = [(k, v) for k, v in data.get("data", {}).items() if isinstance(v, list) and v]
        if not lists:
            break
        key, longest = max(lists, key=lambda kv: len(json.dumps(kv[1])))
        if longest and isinstance(longest[0], dict) and "t_rel_s" in longest[0]:
            idx = max(range(len(longest)), key=lambda i: abs(longest[i].get("t_rel_s") or 0))
            longest.pop(idx)
        else:
            longest.pop()
        truncated = True
        data["data"]["truncated_for_context"] = True
        if "returned" in data["data"] and isinstance(data["data"].get("events"), list):
            data["data"]["returned"] = len(data["data"]["events"])
        s = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return s, truncated


def canonicalize_args(ctx: ToolContext, name: Any, raw_args: Any):
    """Shared by execution and de-duplication.

    Returns (spec, validated_args, normalized_flag, error) where error is
    (code, message) or None.
    """
    spec = REGISTRY.get(name) if isinstance(name, str) else None
    if spec is None:
        return None, None, False, ("unknown_tool",
                                   f"'{clean_text(name, 64) if name else ''}' is not an approved tool. "
                                   f"Approved tools: {', '.join(REGISTRY)}")
    args_obj = raw_args
    if isinstance(raw_args, str):
        try:
            args_obj = json.loads(raw_args) if raw_args.strip() else {}
        except ValueError:
            return spec, None, False, ("invalid_arguments", "arguments must be a JSON object")
    if args_obj is None:
        args_obj = {}
    if not isinstance(args_obj, dict):
        return spec, None, False, ("invalid_arguments", "arguments must be a JSON object")
    normalized = False
    # Deterministic canonicalization of two well-defined formats: a bare 32-hex id
    # or a bare fixture-<slug> (models often drop the "incident:" prefix) gets the
    # prefix back. The canonical value must still equal the authorized incident.
    raw_iid = args_obj.get("incident_id")
    if isinstance(raw_iid, str):
        v = raw_iid.strip().strip("'\"").lower()
        if _BARE_HEX.fullmatch(v) or _BARE_FIXTURE.fullmatch(v):
            v = "incident:" + v
        if v != raw_iid:
            args_obj = {**args_obj, "incident_id": v}
            normalized = True
    try:
        args = spec.args_model.model_validate(args_obj)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
                             for err in e.errors()[:4])
        hint = f" (the incident under investigation is {ctx.incident.id})" if "incident_id" in problems else ""
        return spec, None, normalized, ("invalid_arguments", clean_text(problems, 300) + hint)
    if args.incident_id is not None and args.incident_id != ctx.incident.id:
        return spec, args, normalized, ("permission_denied",
                                        f"this investigation is scoped to {ctx.incident.id}; "
                                        "other incidents are not accessible")
    return spec, args, normalized, None


def call_key(ctx: ToolContext, name: Any, raw_args: Any) -> str:
    """Canonical de-duplication key: tool + validated args with defaults and scope filled in."""
    spec, args, _, err = canonicalize_args(ctx, name, raw_args)
    if spec is None or args is None or err is not None:
        return json.dumps(["__raw__", name, raw_args], sort_keys=True, default=str)
    d = args.model_dump()
    d["incident_id"] = ctx.incident.id
    return json.dumps([spec.name, d], sort_keys=True, default=str)


def _drain_rejected(ctx: ToolContext) -> list[dict]:
    pop = getattr(ctx.source, "pop_rejected_rows", None)
    if pop is None:
        return []
    try:
        rows = pop() or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ws = r.get("workstation_id")
        if ws and ws != ctx.incident.workstation_id:
            continue  # belongs to another investigation's read
        out.append({"view": clean_text(r.get("view", ""), 40),
                    "id": safe_id(r.get("id"))[0] if r.get("id") else None,
                    "reason": clean_text(r.get("reason", ""), 120)})
    return out[:20]


def _rejected_notes(rejected: list[dict]) -> tuple[list[str], bool]:
    """Human notes per reason class, and whether the read is known to be incomplete."""
    kinds = {r["reason"].split(":", 1)[0] for r in rejected}
    notes = []
    if "contract_violation" in kinds:
        notes.append("some backend rows did not match the contract and were excluded")
    if "row_limit_reached" in kinds:
        notes.append("the backend row cap was reached: this window is INCOMPLETE and cannot prove an absence")
    if "unqueryable_id" in kinds:
        notes.append("some ids are outside the supported charset and were not looked up")
    if "id_limit_reached" in kinds:
        notes.append("more linked ids than the lookup limit; the rest were not looked up")
    if "out_of_scope_row_ignored" in kinds:
        notes.append("rows outside the query scope were returned by the backend and ignored")
    if "rejected_rows_overflow" in kinds:
        notes.append("more rejected rows than can be itemized")
    incomplete = bool(kinds & {"row_limit_reached", "id_limit_reached", "rejected_rows_overflow"})
    return notes, incomplete


async def execute_tool_call(ctx: ToolContext, name: Any, raw_args: Any, *, step: int,
                            call_index: int, timeout_s: float) -> tuple[ToolExecution, str]:
    """Validate, permission-check and run one model-proposed tool call.

    Returns the trace record and the JSON string handed back to the model.
    Never raises for model mistakes; those become structured errors.
    """
    started = time.perf_counter()
    prov = ctx.source.provenance
    rec = ToolExecution(step=step, call_index=call_index, tool=clean_text(name, 64) if name else "",
                        arguments_raw=_bound_raw(raw_args), status="internal_error",
                        started_ms=round(started * 1000, 1), duration_ms=0.0,
                        backend=prov.backend, live_data=prov.live)

    def fail(code: str, msg: str) -> tuple[ToolExecution, str]:
        ctx.rollback()
        rejected = _drain_rejected(ctx)
        rec.rejected_rows = len(rejected)
        rec.status = code if code in {"invalid_arguments", "unknown_tool", "permission_denied", "not_found",
                                      "evidence_unavailable", "contract_error", "timeout",
                                      "policy_denied"} else "internal_error"
        rec.error_code, rec.error = code, msg
        rec.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        payload = {"ok": False, "tool": rec.tool, "error": {"code": code, "message": msg}}
        return rec, json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    spec, args, normalized, err = canonicalize_args(ctx, name, raw_args)
    rec.arguments_normalized = normalized
    if args is not None:
        rec.arguments = args.model_dump()
    if err is not None:
        return fail(*err)

    ctx.begin()
    gate = ctx.relay_gate
    if gate is not None and spec.name == _relay.GATED_TOOL:
        rec.policy_gate = gate.label
        work = gate.execute(spec.name, rec.arguments, lambda: spec.handler(ctx, args))
    else:
        work = spec.handler(ctx, args)
    try:
        data, evidence_ids, summary = await asyncio.wait_for(work, timeout=timeout_s)
    except _relay.PolicyDenied as e:
        return fail("policy_denied", f"blocked by NeMo Relay policy before execution: {clean_text(e, 220)}")
    except asyncio.TimeoutError:
        return fail("timeout", f"tool exceeded its {timeout_s:.1f}s deadline")
    except EvidenceUnavailable as e:
        return fail("evidence_unavailable", f"evidence source unavailable: {clean_text(e, 160)}")
    except EvidenceContractError as e:
        return fail("contract_error", f"evidence did not match the agreed contract: {clean_text(e, 160)}")
    except ToolFailure as e:
        return fail(e.code, e.message)
    except ValidationError as e:  # backend rows failed contract validation
        return fail("contract_error", clean_text(str(e), 200))
    except Exception as e:  # noqa: BLE001 - never leak internals to the model
        log.exception("tool %s failed in investigation %s", spec.name, ctx.investigation_id)
        return fail("internal_error", f"tool failed: {type(e).__name__}")

    rejected = _drain_rejected(ctx)
    incomplete = False
    if rejected:
        notes, incomplete = _rejected_notes(rejected)
        data["rejected_rows"] = rejected
        data["rejected_rows_note"] = "; ".join(notes)
        if incomplete:
            data["incomplete_read"] = True
            ctx.signal("incomplete_reads", ctx.signals.get("incomplete_reads", 0) + 1)
    payload = {"ok": True, "tool": spec.name, "source": ("LIVE " if prov.live else "") + prov.label, "data": data}
    if not evidence_ids and not incomplete:
        # A query that returned no evidence items gets a citable id, so the ABSENCE
        # ("no vision events in the last 600 s") can be cited. Results that contain
        # records are cited by those records' own ids.
        result_id = f"res:{step}.{call_index}:{spec.name}"
        ctx.register(result_id, "query_result", None, f"{spec.name}: {summary}", absence=True)
        payload = {"ok": True, "tool": spec.name, "result_id": result_id, "source": payload["source"], "data": data}
    content, truncated = _fit(payload, ctx.max_result_chars)
    shown = ctx.commit(content)
    rec.status = "ok"
    rec.result_summary = summary
    rec.evidence_ids = shown
    rec.result_chars = len(content)
    rec.truncated_for_context = truncated
    rec.rejected_rows = len(rejected)
    rec.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    return rec, content
