"""FixtureEvidenceSource follows the same read contract as the live adapter (see evidence/base.py)."""

import asyncio
import json
import time

import pytest

from nemotron_investigator.evidence.base import (
    BASELINE_ROW_LIMIT,
    WINDOW_ROW_LIMIT,
    EvidenceContractError,
    EvidenceSource,
    EvidenceUnavailable,
)
from nemotron_investigator.evidence.fixture import FixtureEvidenceSource

from conftest import A_ID, A_PATH, SCENARIOS

T0 = 1_794_087_667_412
ME = "c200" + "0" * 60


def incident(iid="incident:" + "1" * 32, ws="ws:a", sid="sess:a", **kw):
    d = {"id": iid, "workstation_id": ws, "session_id": sid, "severity": "medium", "state": "open",
         "rule_id": "unknown_usb", "summary": "s", "event_ids_json": "[]", "digital_event_id": "",
         "physical_event_id": "", "created_at_ms": T0, "updated_at_ms": T0, "acknowledged_by": "",
         "resolved_by": "", "resolution_note": "", "action_required": True, "simulated": False}
    d.update(kw)
    return d


def session(sid="sess:a", ws="ws:a", **kw):
    d = {"id": sid, "workstation_id": ws, "state": "armed", "armed_by": ME, "armed_at_ms": T0 - 600_000,
         "expires_at_ms": 0, "disarmed_at_ms": 0, "baseline_json": "{}", "approved_apps_json": "[]",
         "auto_lock_enabled": True, "owner_policy_hash": "", "simulated": False}
    d.update(kw)
    return d


def event(eid, ws="ws:a", received=T0, observed=None, **kw):
    d = {"id": eid, "workstation_id": ws, "session_id": "sess:a", "device_id": "dev:1", "source": "macos_usb",
         "kind": "usb_device_connected", "observed_at_ms": received - 140 if observed is None else observed,
         "received_at_ms": received, "seq": 1, "confidence": 1.0, "metadata_json": "{}", "simulated": False,
         "schema_version": 1, "classification": ""}
    d.update(kw)
    return d


def snapshot(sid, received, ws="ws:a", observed=None):
    return {"id": sid, "workstation_id": ws, "device_id": "dev:1", "publisher": ME,
            "observed_at_ms": received - 90 if observed is None else observed, "received_at_ms": received,
            "baseline_json": "{}", "active_app_id": "", "session_id": "sess:a", "camera_status": "online",
            "health_json": "{}"}


def health(hid, ws="ws:a"):
    return {"id": hid, "workstation_id": ws, "device_id": "dev:cam", "component": "vision", "status": "online",
            "last_seen_ms": T0, "detail": ""}


def dataset(tmp_path, name="T", **tables):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"dataset_id": name, "tables": {"incidents": [incident()], **tables}}))
    return p


async def all_reads(src, ws, sid, ids):
    await src.get_incident(A_ID)
    await src.get_session(ws, sid)
    await src.get_device_events_by_ids(ws, ids)
    await src.list_device_events(ws, T0 - 300_000, T0 + 60_000)
    await src.list_baseline_snapshots(ws, T0 + 60_000)
    await src.list_device_health(ws)


# ------------------------------------------------------------ real datasets
async def test_real_scenarios_satisfy_the_strict_contract():
    src = FixtureEvidenceSource(SCENARIOS)
    assert isinstance(src, EvidenceSource)
    for f in sorted(SCENARIOS.glob("*.json")):
        inc_raw = json.loads(f.read_text())["tables"]["incidents"][0]
        inc = await src.get_incident(inc_raw["id"])
        t0 = inc.created_at_ms
        assert await src.get_session(inc.workstation_id, inc.session_id) is not None
        await src.get_device_events_by_ids(inc.workstation_id, inc.linked_event_ids())
        await src.list_device_events(inc.workstation_id, t0 - 1_800_000, t0 + 60_000)
        await src.list_baseline_snapshots(inc.workstation_id, t0 + 60_000)
        await src.list_device_health(inc.workstation_id)
        assert src.pop_rejected_rows() == [], f.name


# ------------------------------------------------------- session scope (finding)
async def test_get_session_is_bound_to_the_workstation():
    src = FixtureEvidenceSource(SCENARIOS)
    inc = await src.get_incident(A_ID)
    assert (await src.get_session(inc.workstation_id, inc.session_id)).id == inc.session_id
    assert await src.get_session("ws:fixture-b", inc.session_id) is None
    assert await src.get_session(inc.workstation_id, "") is None


async def test_incident_naming_a_foreign_session_gets_none(tmp_path):
    # incidents.session_id is device-supplied: it can name a session on another workstation.
    p = dataset(tmp_path, incidents=[incident(sid="sess:victim")],
                sessions=[session("sess:victim", ws="ws:victim")])
    src = FixtureEvidenceSource(p)
    assert await src.get_session("ws:a", "sess:victim") is None
    assert (await src.get_session("ws:victim", "sess:victim")).workstation_id == "ws:victim"


# -------------------------------------------------------- server-clock windows
async def test_event_window_selects_on_received_at_ms(tmp_path):
    p = dataset(tmp_path, device_events=[
        event("evt:skewed", received=T0 - 1_000, observed=T0 - 3_600_000),   # device clock 1 h behind
        event("evt:late", received=T0 + 120_000, observed=T0 - 1_000),      # device clock claims "before"
        event("evt:other-ws", ws="ws:b", received=T0)])
    src = FixtureEvidenceSource(p)
    rows = await src.list_device_events("ws:a", T0 - 300_000, T0 + 60_000)
    assert [r.id for r in rows] == ["evt:skewed"]
    assert await src.list_device_events("ws:a", T0 + 1, T0) == []
    assert await src.list_device_events("ws:a", -10, T0 + 60_000) == rows  # negative bound clamps to 0


async def test_baseline_window_is_server_clock_and_24h_bounded(tmp_path):
    until = T0 + 60_000
    p = dataset(tmp_path, baseline_snapshots=[
        snapshot("base:too-old", until - 90_000_000),
        snapshot("base:ok", until - 3_000_000),
        snapshot("base:after", until + 5_000, observed=until - 10_000)])
    src = FixtureEvidenceSource(p)
    assert [s.id for s in await src.list_baseline_snapshots("ws:a", until)] == ["base:ok"]
    assert await src.list_baseline_snapshots("ws:a", -1) == []


# --------------------------------------------------------- per-row validation
async def test_invalid_rows_are_dropped_and_reported_not_fatal(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"dataset_id": "bad", "tables": {
        "incidents": [incident()],
        "device_events": [event("evt:good"), event("evt:nan", confidence=float("nan")),
                          event("evt:retyped", seq="7"), event("evt:bad-window", received_at_ms="soon")],
        "device_health": [health("h:good"), {**health("h:bad"), "last_seen_ms": -5}]}}))
    src = FixtureEvidenceSource(p)  # loading no longer fails on one bad row
    rows = await src.list_device_events("ws:a", T0 - 300_000, T0 + 60_000)
    assert [r.id for r in rows] == ["evt:good"]
    rej = src.pop_rejected_rows()
    assert [(r["view"], r["id"]) for r in rej] == [("my_device_events", "evt:nan"),
                                                   ("my_device_events", "evt:retyped"),
                                                   ("my_device_events", "evt:bad-window")]
    assert all(r["reason"].startswith("contract_violation: ") for r in rej)
    assert "confidence" in rej[0]["reason"] and "seq" in rej[1]["reason"]
    assert src.pop_rejected_rows() == []
    assert [h.id for h in await src.list_device_health("ws:a")] == ["h:good"]
    assert [r["id"] for r in src.pop_rejected_rows()] == ["h:bad"]


async def test_invalid_single_rows_raise_contract_errors(tmp_path):
    p = dataset(tmp_path, incidents=[incident(created_at_ms="x")], sessions=[session(armed_by="not-hex")])
    src = FixtureEvidenceSource(p)
    with pytest.raises(EvidenceContractError, match="my_incidents row failed contract validation: created_at_ms"):
        await src.get_incident("incident:" + "1" * 32)
    with pytest.raises(EvidenceContractError, match="armed_by"):
        await src.get_session("ws:a", "sess:a")


async def test_identity_columns_are_canonical(tmp_path):
    p = dataset(tmp_path, sessions=[session(armed_by="0xC2" + "0" * 60)],
                baseline_snapshots=[{**snapshot("base:1", T0), "publisher": ["0xc2" + "0" * 60]}])
    src = FixtureEvidenceSource(p)
    s = await src.get_session("ws:a", "sess:a")
    assert s.armed_by == "00c2" + "0" * 60  # 0x and case dropped, zero-padded to 64
    (b,) = await src.list_baseline_snapshots("ws:a", T0)
    assert b.publisher == s.armed_by and len(b.publisher) == 64


# --------------------------------------------------------------- linked ids
async def test_unqueryable_linked_ids_are_reported_not_missing(tmp_path):
    p = dataset(tmp_path, device_events=[event("evt:1"), event("evt 2"), event("evt:3", ws="ws:b")])
    src = FixtureEvidenceSource(p)
    rows = await src.get_device_events_by_ids("ws:a", ["evt:1", "evt 2", "evt:1", "evt:3"])
    assert [r.id for r in rows] == ["evt:1"]  # evt:3 belongs to another workstation
    assert [(r["id"], r["reason"]) for r in src.pop_rejected_rows()] == [("evt 2", "unqueryable_id")]


@pytest.mark.parametrize("call", [
    lambda s: s.list_device_events("ws a", 0, 10),
    lambda s: s.list_baseline_snapshots("ws;a", 10),
    lambda s: s.list_device_health("ws'a"),
    lambda s: s.get_session("ws:a", "sess a"),
    lambda s: s.get_device_events_by_ids("ws a", ["evt:1"]),
])
async def test_ids_outside_charset_are_contract_errors(call):
    with pytest.raises(EvidenceContractError, match="identifier outside supported charset") as ei:
        await call(FixtureEvidenceSource(A_PATH))
    assert isinstance(ei.value, ValueError)


# ---------------------------------------------------------------- row caps
async def test_window_row_cap_matches_live_adapter(tmp_path):
    p = dataset(tmp_path, device_events=[event(f"evt:{i}", received=T0 - i) for i in range(WINDOW_ROW_LIMIT + 1)])
    src = FixtureEvidenceSource(p)
    assert len(await src.list_device_events("ws:a", T0 - 300_000, T0)) == WINDOW_ROW_LIMIT
    assert [r["reason"].split(":")[0] for r in src.pop_rejected_rows()] == ["row_limit_reached"]


async def test_baseline_cap_narrows_to_find_the_newest_snapshot(tmp_path):
    until = T0
    old = [snapshot(f"base:old{i}", until - 50_000_000 + i) for i in range(BASELINE_ROW_LIMIT + 50)]
    new = [snapshot(f"base:new{i}", until - 600_000 + i) for i in range(3)]
    src = FixtureEvidenceSource(dataset(tmp_path, baseline_snapshots=old + new))
    rows = await src.list_baseline_snapshots("ws:a", until)
    assert {r.id for r in rows} == {"base:new0", "base:new1", "base:new2"}
    assert src.pop_rejected_rows() == []
    assert src.reads == 1  # narrowing is internal: still one read


# ------------------------------------------- unchanged fixture semantics (#7)
async def test_reads_counter_counts_each_read_call():
    src = FixtureEvidenceSource(A_PATH)
    inc = await FixtureEvidenceSource(A_PATH).get_incident(A_ID)
    await all_reads(src, inc.workstation_id, inc.session_id, inc.linked_event_ids())
    assert src.reads == 6
    await src.ping()
    assert src.reads == 6


@pytest.mark.parametrize("call", [
    lambda s: s.get_incident(A_ID),
    lambda s: s.get_session("ws:fixture-a", "sess:559aead08264d5795d390971"),
    lambda s: s.get_device_events_by_ids("ws:fixture-a", ["evt:29f4d6f26ab896f4838e1906"]),
    lambda s: s.list_device_events("ws:fixture-a", 0, T0),
    lambda s: s.list_baseline_snapshots("ws:fixture-a", T0),
    lambda s: s.list_device_health("ws:fixture-a"),
])
async def test_fail_mode_unavailable_applies_to_every_read(call):
    src = FixtureEvidenceSource(A_PATH, fail_mode="unavailable")
    with pytest.raises(EvidenceUnavailable):
        await call(src)
    assert src.reads == 1
    assert (await src.ping())["reachable"] is False


def test_legacy_row_lists_still_expose_valid_rows(tmp_path):
    src = FixtureEvidenceSource(dataset(tmp_path, sessions=[session(), session("sess:bad", armed_at_ms=-1)],
                                        device_events=[event("evt:1")]))
    assert [s.id for s in src._sessions] == ["sess:a"]
    assert [e.id for e in src._events] == ["evt:1"] and len(src._incidents) == 1
    assert src._baselines == [] and src._health == []


async def test_delay_applies_before_each_read():
    src = FixtureEvidenceSource(A_PATH, delay_s=0.05)
    t = time.perf_counter()
    await src.list_device_health("ws:fixture-a")
    assert time.perf_counter() - t >= 0.045
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(FixtureEvidenceSource(A_PATH, fail_mode="slow").get_incident(A_ID), 0.05)
