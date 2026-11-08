"""Owner-authorized one-use voice ARM, on Sid's deployed contract (docs/airlock/ARM_DELEGATION.md, live on locki-w91bw since 2026-11-08).

The owner, on the dashboard's persistent SDK connection, issues one grant with `authorize_arm` for this Mac's enrolled device: a fixed
session id, duration, reviewed baseline and apps, valid for at most 60 seconds. This Mac sees it in `my_arm_authorizations`. A spoken ARM
intent may then call `arm_session_delegated(workstationId, authorizationId)` and nothing else: the transcript never chooses a reducer, a
session, a duration or a scope, and the Mac supplies no baseline or policy. The server rechecks everything and consumes the grant
atomically with the arm. LOCKI says it is armed only when the armed session row comes back through `my_sessions` (the service's
apply_remote_session), never on the reducer's reply alone. Disarming stays the owner's; nothing here can disarm.
"""

from __future__ import annotations

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

    def usable(self, now_ms: int) -> bool:
        return self.status == "authorized" and self.expires_at_ms - EXPIRY_MARGIN_MS > now_ms


def grant_from_row(row: dict) -> ArmGrant:
    """A my_arm_authorizations row (snake_case columns, read from the live view)."""
    return ArmGrant(str(row["id"]), str(row["workstation_id"]), str(row["device_id"]), str(row["session_id"]), int(row.get("duration_ms") or 0),
                    int(row.get("issued_at_ms") or 0), int(row["expires_at_ms"]), str(row.get("status") or "").lower())


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
        """The newest grant that is still authorized and not about to expire. Only one pending grant exists per workstation on the server."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        live = [g for g in self._grants.values() if g.usable(now)]
        return max(live, key=lambda g: g.issued_at_ms) if live else None

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
