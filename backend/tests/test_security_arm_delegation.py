"""Voice ARM through the owner's one-use grant (Sid's live contract on locki-w91bw). Speech alone never arms: the Mac consumes only a grant
the owner issued for this device, and LOCKI says armed only when the database's armed session comes back. No test talks to the real database."""

from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest

from app.security.arm_delegation import EXPIRY_MARGIN_MS, ArmGrant, ArmGrants, grant_from_row
from app.security.publisher import Delivery
from app.security.spacetime_adapter import SpacetimeSubscriber

from .test_security_spacetime import CONFIG, Recording, following, remote

GRANT_COLUMNS = ["id", "workstation_id", "owner", "owner_connection_id", "device_id", "delegate", "session_id", "baseline_json", "active_app_id",
                 "approved_apps_json", "duration_ms", "owner_policy_hash", "issued_at_ms", "expires_at_ms", "status", "consumed_at_ms"]  # read from the live view
MAC_ID, OWNER_ID = "a" * 64, "b" * 64


def grant_row(status="authorized", expires_in_ms=45_000, auth="auth-1", session="dhruv-m4-air-voice-1", device=CONFIG.device_id, workstation=CONFIG.workstation_id):
    now = int(time.time() * 1000)
    return [auth, workstation, ["0x" + OWNER_ID], ["0xconn"], device, ["0x" + MAC_ID], session, "[]", "com.apple.Terminal", "[]", 600_000, "hash-1234", now - 1_000,
            now + expires_in_ms, status, 0]


def grant(**kw) -> ArmGrant:
    return grant_from_row(dict(zip(GRANT_COLUMNS, grant_row(**kw), strict=False)))


def test_only_an_authorized_unexpired_grant_for_this_mac_is_usable():
    grants = ArmGrants(CONFIG.workstation_id, CONFIG.device_id)
    assert grants.usable() is None and grants.why_none() == "no owner authorization for voice arming"
    assert not grants.apply(grant(device="mac-other")) and not grants.apply(grant(workstation="someone-else"))
    grants.apply(grant(expires_in_ms=EXPIRY_MARGIN_MS - 100))
    assert grants.usable() is None and grants.why_none() == "the owner's authorization expired"
    grants.apply(grant(auth="auth-2", status="consumed"))
    assert grants.usable() is None
    grants.apply(grant(auth="auth-3"))
    assert grants.usable().authorization_id == "auth-3"
    grants.apply(grant(auth="auth-3", status="revoked"))  # the same grant, revoked by the owner or their disconnect
    assert grants.usable() is None


async def test_grant_rows_from_the_view_reach_the_mac():
    seen = []
    sub = SpacetimeSubscriber(CONFIG, on_incident=print, on_lock_request=print, on_session=print, on_arm_grant=seen.append)
    assert "my_arm_authorizations" in sub._views()
    assert "my_arm_authorizations" not in SpacetimeSubscriber(CONFIG, on_incident=print, on_lock_request=print, on_session=print)._views()
    sub._columns = {"my_arm_authorizations": (GRANT_COLUMNS, [False] * len(GRANT_COLUMNS))}
    tables = [{"table_name": "my_arm_authorizations", "updates": [{"deletes": [], "inserts": [json.dumps(grant_row())]}]}]
    await sub._message(json.dumps({"TransactionUpdateLight": {"request_id": 0, "update": {"tables": tables}}}))
    assert len(seen) == 1 and seen[0].authorization_id == "auth-1" and seen[0].status == "authorized" and sub.rows_rejected == 0


class Delegating(Recording):
    """The Mac's transport, with the reducer reply and the database's later session row under the test's control."""

    def __init__(self, rt, reply="delivered", confirm=True):
        super().__init__()
        self.rt, self.reply, self.confirm, self.calls = rt, reply, confirm, []
        self.committed = None
        self.snapshots = 0

    def read_arm_snapshot(self):
        self.snapshots += 1
        grants = self.rt.security.arm_grants._grants.values()
        rows = []
        for g in grants:
            row = dict(zip(GRANT_COLUMNS, grant_row(auth=g.authorization_id, session=g.session_id, status=g.status, device=g.device_id, workstation=g.workstation_id), strict=False))
            row.update(issued_at_ms=g.issued_at_ms, expires_at_ms=g.expires_at_ms)
            if self.committed and self.committed[0] == g.authorization_id:
                row.update(status="consumed", consumed_at_ms=self.committed[1])
            rows.append(row)
        result = {"grants": rows, "memberships": [{"workstation_id": CONFIG.workstation_id, "identity": ["0x" + MAC_ID], "role": "mac", "enabled": True}],
                  "devices": [{"id": CONFIG.device_id, "workstation_id": CONFIG.workstation_id, "identity": ["0x" + MAC_ID], "role": "mac", "enabled": True}],
                  "sessions": [], "workstations": [{"id": CONFIG.workstation_id, "owner": ["0x" + OWNER_ID], "active_session_id": "", "auto_lock_enabled": False, "owner_policy_hash": "hash-1234"}]}
        if self.committed:
            g = next(row for row in rows if row["id"] == self.committed[0])
            result["sessions"] = [{"id": g["session_id"], "workstation_id": CONFIG.workstation_id, "armed_by": ["0x" + MAC_ID], "state": "armed", "armed_at_ms": self.committed[1], "expires_at_ms": self.committed[1] + g["duration_ms"], "disarmed_at_ms": 0, "baseline_json": g["baseline_json"], "approved_apps_json": g["approved_apps_json"], "owner_policy_hash": g["owner_policy_hash"], "auto_lock_enabled": False, "simulated": False}]
            result["workstations"][0]["active_session_id"] = g["session_id"]
        return copy.deepcopy(result)

    def call_reducer(self, reducer, args):
        self.calls.append((reducer, list(args)))
        if self.reply == "delivered" and self.confirm:
            g = self.rt.security.arm_grants.usable()
            self.committed = (g.authorization_id, int(time.time() * 1000))
            loop = self.rt.security._loop or asyncio.get_event_loop()
            loop.call_soon_threadsafe(self.rt.security.apply_remote_session, remote(session_id=g.session_id if g else "?"))
        return Delivery(self.reply, self.name, f"{reducer}: {self.reply}")


async def delegating(rt, **kw):
    h = await following(rt)
    rt.security.transport = Delegating(rt, **kw)
    rt.security.arm_grants = ArmGrants(CONFIG.workstation_id, CONFIG.device_id)
    return h


async def say(rt, text="LOCKI, watch my desk."):
    return await rt.handle_utterance(text, source="text", stt_confidence=1.0)


async def test_without_the_owners_grant_a_spoken_arm_changes_nothing(rt):
    h = await delegating(rt)
    r = await say(rt, "I'm heading out. Keep an eye on everything.")
    assert not r.ok and rt.security.session is None and rt.security.transport.calls == []
    assert await h.said("Approve voice arming on the dashboard") or await h.said("go-ahead on the dashboard")
    assert rt.security.voice_arm["outcome"] == "no_grant"


async def test_the_owners_grant_arms_once_and_locki_says_so_only_after_the_database_confirms(rt):
    h = await delegating(rt)
    rt.security.arm_grants.apply(grant())
    r = await say(rt)
    assert r.ok and rt.security.session.session_id == "dhruv-m4-air-voice-1" and rt.security.session.source == "spacetimedb"
    assert rt.security.transport.calls == [("arm_session_delegated", [CONFIG.workstation_id, "auth-1"])]  # nothing the speaker chose
    assert await h.said("Armed.") and rt.security.voice_arm["outcome"] == "armed" and rt.security.voice_arm["readback_ms"] >= rt.security.voice_arm["reducer_ms"]


async def test_an_expired_or_consumed_grant_is_never_called(rt):
    await delegating(rt)
    rt.security.arm_grants.apply(grant(expires_in_ms=500))
    r = await say(rt)
    assert not r.ok and rt.security.transport.calls == [] and "unavailable" in rt.security.voice_arm["why"]
    rt.security.arm_grants.apply(grant(auth="auth-2", status="consumed"))  # a replay of a grant already used
    r = await say(rt)
    assert not r.ok and rt.security.transport.calls == [] and rt.security.session is None


@pytest.mark.parametrize("reply, outcome, line", [("refused", "refused", "hasn't confirmed"), ("failed", "unconfirmed", "hasn't confirmed")])
async def test_a_refused_or_failed_call_never_claims_to_be_armed(rt, reply, outcome, line):
    h = await delegating(rt, reply=reply)
    rt.security.arm_grants.apply(grant())
    r = await say(rt)
    assert not r.ok and rt.security.session is None and rt.security.voice_arm["outcome"] == outcome
    assert await h.said(line) and not any("Armed." in s for s in h.spoken())


async def test_an_accepted_call_with_no_armed_session_back_is_reported_unconfirmed(rt, monkeypatch):
    import app.security.service as service

    monkeypatch.setattr(service, "ARM_READBACK_S", 0.3)
    h = await delegating(rt, confirm=False)
    rt.security.arm_grants.apply(grant())
    r = await say(rt)
    assert not r.ok and rt.security.session is None and rt.security.voice_arm["outcome"] == "unconfirmed"
    assert await h.said("hasn't confirmed") and not any("Armed." in s for s in h.spoken())


async def test_a_spoken_disarm_still_changes_nothing_after_a_voice_arm(rt):
    await delegating(rt)
    rt.security.arm_grants.apply(grant())
    await say(rt)
    r = await say(rt, "Alright, we're good. Stand down.")
    assert not r.ok and rt.security.session is not None and len(rt.security.transport.calls) == 1
