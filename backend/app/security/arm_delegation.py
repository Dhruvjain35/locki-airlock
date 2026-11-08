"""Owner-authorized one-use voice ARM, on Sid's deployed contract (docs/airlock/ARM_DELEGATION.md, live on locki-w91bw since 2026-11-08).

The owner, on the dashboard's persistent SDK connection, issues one grant with `authorize_arm` for this Mac's enrolled device: a fixed
session id, duration, reviewed baseline and apps, valid for at most 60 seconds. This Mac sees it in `my_arm_authorizations`. A spoken ARM
intent may then call `arm_session_delegated(workstationId, authorizationId)` and nothing else: the transcript never chooses a reducer, a
session, a duration or a scope, and the Mac supplies no baseline or policy. The server rechecks everything and consumes the grant
atomically with the arm. LOCKI says it is armed only after fresh authenticated reads verify the consumed grant and exact live session,
never from the subscription cache or the reducer's reply alone. Disarming stays the owner's; nothing here can disarm.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

ARM_DELEGATED_REDUCER = "arm_session_delegated"
GRANTS_VIEW = "my_arm_authorizations"
EXPIRY_MARGIN_MS = 1_500  # a grant this close to expiry is treated as gone: the reducer call itself takes time, and the server's clock decides


@dataclass(frozen=True)
class ArmGrant:
    authorization_id: str
    workstation_id: str
    device_id: str
    session_id: str
    duration_ms: int
    issued_at_ms: int
    expires_at_ms: int
    status: str
    delegate: str = ""
    owner: str = ""
    baseline_json: str = ""
    approved_apps_json: str = ""
    owner_policy_hash: str = ""
    consumed_at_ms: int = 0

    def usable(self, now_ms: int) -> bool:
        return self.status == "authorized" and self.expires_at_ms - EXPIRY_MARGIN_MS > now_ms


def grant_from_row(row: dict) -> ArmGrant:
    """A my_arm_authorizations row (snake_case columns, read from the live view)."""
    return ArmGrant(str(row["id"]), str(row["workstation_id"]), str(row["device_id"]), str(row["session_id"]), int(row.get("duration_ms") or 0),
                    int(row.get("issued_at_ms") or 0), int(row["expires_at_ms"]), str(row.get("status") or "").lower(),
                    identity_hex(row.get("delegate")), identity_hex(row.get("owner")), str(row.get("baseline_json") or ""),
                    str(row.get("approved_apps_json") or ""), str(row.get("owner_policy_hash") or ""), int(row.get("consumed_at_ms") or 0))


def identity_hex(value) -> str:
    while isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, dict) and set(value) == {"__identity__"}:
        value = value["__identity__"]
    value = str(value or "").removeprefix("0x").lower()
    return value if re.fullmatch(r"[a-f0-9]{64}", value) else ""


def mac_principal(snapshot: dict, workstation: str, device: str) -> str:
    """my_memberships is caller-scoped: its matching row proves the actual caller."""
    memberships = [m for m in snapshot["memberships"] if m.get("workstation_id") == workstation and m.get("enabled") is True and m.get("role") == "mac"]
    if len(memberships) != 1 or not (principal := identity_hex(memberships[0].get("identity"))):
        raise ValueError("A matching enabled Mac membership is required")
    devices = [d for d in snapshot["devices"] if d.get("id") == device and d.get("workstation_id") == workstation and d.get("role") == "mac" and d.get("enabled") is True and identity_hex(d.get("identity")) == principal]
    if len(devices) != 1:
        raise ValueError("The configured device must belong to this Mac credential")
    return principal


def select_fresh_grant(snapshot: dict, workstation: str, device: str, now_ms: int | None = None) -> ArmGrant:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    principal = mac_principal(snapshot, workstation, device)
    grants = [grant_from_row(row) for row in snapshot["grants"] if row.get("workstation_id") == workstation and row.get("device_id") == device]
    grants = [g for g in grants if g.usable(now) and g.delegate == principal and g.owner and g.owner != principal]
    if len(grants) != 1:
        raise ValueError("Exactly one fresh owner authorization for this Mac is required")
    grant = grants[0]
    if not all(re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", value) for value in (grant.authorization_id, grant.session_id)) or not 1000 <= grant.expires_at_ms - grant.issued_at_ms <= 60000 or grant.issued_at_ms > now + 5000 or not 60000 <= grant.duration_ms <= 3600000:
        raise ValueError("Authorization timing or identifiers are invalid")
    stations = [w for w in snapshot["workstations"] if w.get("id") == workstation and identity_hex(w.get("owner")) == grant.owner and w.get("auto_lock_enabled") is False and w.get("owner_policy_hash") == grant.owner_policy_hash and not w.get("active_session_id")]
    if len(stations) != 1:
        raise ValueError("Owner policy or workstation state changed")
    return grant


def confirmed_session_row(snapshot: dict, grant: ArmGrant, now_ms: int | None = None) -> dict:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if mac_principal(snapshot, grant.workstation_id, grant.device_id) != grant.delegate:
        raise ValueError("Mac identity changed")
    rows = [r for r in snapshot["grants"] if r.get("id") == grant.authorization_id]
    if len(rows) != 1:
        raise ValueError("Consumed authorization is unavailable")
    current = grant_from_row(rows[0])
    immutable = ("authorization_id", "workstation_id", "device_id", "session_id", "duration_ms", "issued_at_ms", "expires_at_ms", "delegate", "owner", "baseline_json", "approved_apps_json", "owner_policy_hash")
    if current.status != "consumed" or not grant.issued_at_ms <= current.consumed_at_ms < grant.expires_at_ms or any(getattr(current, key) != getattr(grant, key) for key in immutable):
        raise ValueError("Matching consumed authorization was not confirmed")
    sessions = [s for s in snapshot["sessions"] if s.get("id") == grant.session_id and s.get("workstation_id") == grant.workstation_id]
    if len(sessions) != 1:
        raise ValueError("Bound session unavailable")
    session = sessions[0]
    if (session.get("state") != "armed" or session.get("simulated") is not False or session.get("auto_lock_enabled") is not False
            or identity_hex(session.get("armed_by")) != grant.delegate or session.get("disarmed_at_ms") != 0
            or session.get("armed_at_ms") != current.consumed_at_ms or session.get("expires_at_ms", 0) <= now
            or session.get("expires_at_ms") != current.consumed_at_ms + grant.duration_ms
            or session.get("baseline_json") != grant.baseline_json or session.get("approved_apps_json") != grant.approved_apps_json or session.get("owner_policy_hash") != grant.owner_policy_hash):
        raise ValueError("Exact live delegated session was not confirmed")
    if not any(w.get("id") == grant.workstation_id and w.get("active_session_id") == grant.session_id and w.get("auto_lock_enabled") is False and w.get("owner_policy_hash") == grant.owner_policy_hash and identity_hex(w.get("owner")) == grant.owner for w in snapshot["workstations"]):
        raise ValueError("Workstation no longer selects the delegated session")
    return session


def security_voice_intent(raw: str) -> str | None:
    """Whole original utterance only: no quotes, negation, discussion or overrides."""
    if not isinstance(raw, str) or len(raw) > 300 or re.search(r'["“”`;:\n]', raw):
        return None
    text = raw.lower().replace("i'm", "i am").replace("i’m", "i am").replace("we're", "we are").replace("we’re", "we are").replace("i'll", "i will").replace("i’ll", "i will")
    text = " ".join(re.sub(r"[,.!?]", " ", text).split())
    prefix = r"(?:(?:(?:hey|yo) )?(?:locki|airlock) )?(?:(?:can|could|would) you )?(?:please )?"
    obj = r"(?:my |the )?(?:setup|desk|workstation|computer|laptop|mac|stuff)"
    arm = rf"(?:arm(?: (?:my |the )?(?:setup|desk|workstation|computer|laptop|mac|security|system))?|(?:watch|monitor|guard|protect|secure|start watching|start monitoring) {obj}|(?:enable|start) {obj} monitoring|(?:i am heading out )?keep an eye on (?:everything|{obj})|(?:i will be back )?make sure nobody touches {obj})"
    disarm = rf"(?:disarm(?: {obj})?|(?:you can )?(?:stop watching|stop monitoring|stop guarding)(?: {obj})?|(?:disable|stop) {obj} monitoring|(?:alright we are good )?stand down)"
    for intent, command in (("arm", arm), ("disarm", disarm)):
        if re.fullmatch(prefix + command + r"(?: now)?(?: please)?", text):
            return intent
    return None


class ArmGrants:
    """The grants this Mac can see. Rows for another workstation or device are never kept."""

    def __init__(self, workstation_id: str, device_id: str):
        self.workstation_id, self.device_id = workstation_id, device_id
        self._grants: dict[str, ArmGrant] = {}
        self.last_seen_at_ms: int | None = None

    def apply(self, grant: ArmGrant) -> bool:
        if grant.workstation_id != self.workstation_id or grant.device_id != self.device_id:
            return False
        self._grants[grant.authorization_id] = grant
        self.last_seen_at_ms = int(time.time() * 1000)
        while len(self._grants) > 64:  # consumed and revoked rows are tombstones on the server; only recent ones matter here
            self._grants.pop(next(iter(self._grants)))
        return True

    def usable(self, now_ms: int | None = None) -> ArmGrant | None:
        """Display cache only: exactly one usable grant, never authoritative for consumption."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        live = [g for g in self._grants.values() if g.usable(now)]
        return live[0] if len(live) == 1 else None

    def clear(self):
        self._grants.clear()
        self.last_seen_at_ms = None

    def remove(self, authorization_id: str):
        self._grants.pop(authorization_id, None)

    def why_none(self, now_ms: int | None = None) -> str:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if not self._grants:
            return "no owner authorization for voice arming"
        newest = max(self._grants.values(), key=lambda g: g.issued_at_ms)
        if newest.status == "authorized" and newest.expires_at_ms - EXPIRY_MARGIN_MS <= now:
            return "the owner's authorization expired"
        return f"the owner's last authorization is {newest.status or 'unknown'}"

    def snapshot(self, now_ms: int | None = None) -> dict:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        g = self.usable(now)
        return {"usable": None if g is None else {"authorization_id": g.authorization_id, "session_id": g.session_id, "expires_in_ms": g.expires_at_ms - now,
                                                  "duration_ms": g.duration_ms},
                "why_not": None if g is not None else self.why_none(now), "seen": len(self._grants)}
