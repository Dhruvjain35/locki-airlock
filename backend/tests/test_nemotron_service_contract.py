"""Strict tunnel/credential/response boundaries using fake HTTP only."""

import asyncio
import os

import httpx
import pytest

from app.intelligence import nemotron_service as ns
from app.intelligence.nemotron_service import InvestigatorUnavailable, NemotronService

from .test_nemotron_service import REAL, api, service


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(ns, "POLL_S", 0.001)


@pytest.mark.parametrize("url", ["http://localhost.evil.test:18787", "http://127.0.0.1.evil.test:18787",
                               "http://localhost@evil.test:18787", "https://example.com:8787",
                               "http://127.0.0.1:18787/path", "http://127.0.0.1:18787?secret=x",
                               "http://localhost:18787#x", "http://localhost", "http://127.0.0.1:99999"])
async def test_invalid_destination_rejected_before_key_or_network(tmp_path, url):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("network reached")))
    s = NemotronService(url=url, key_file=str(tmp_path / "missing"), client=client)
    with pytest.raises(InvestigatorUnavailable, match="loopback"):
        await s.investigate(REAL)
    await client.aclose()


@pytest.mark.parametrize("problem", ["public", "symlink", "oversize", "empty", "multiline", "not-owner", "fifo"])
async def test_key_must_be_private_owned_bounded_regular_file(tmp_path, monkeypatch, problem):
    s = service(tmp_path, lambda _: pytest.fail("invalid key reached network"))
    path = s.key_file
    if problem == "public":
        path.chmod(0o644)
    elif problem == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    elif problem == "oversize":
        path.write_text("x" * (ns.MAX_KEY_BYTES + 1))
    elif problem == "empty":
        path.write_text("")
    elif problem == "multiline":
        path.write_text("key\r\nInjected: header")
    elif problem == "not-owner":
        owner = os.geteuid()
        monkeypatch.setattr(ns.os, "geteuid", lambda: owner + 1)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    with pytest.raises(InvestigatorUnavailable, match="private owned regular"):
        await s.investigate(REAL)


@pytest.mark.parametrize("phase", ["POST", "GET"])
@pytest.mark.parametrize("payload", [b"not json", b"[]", b"null", b"{}"])
async def test_malformed_protocol_is_unavailable(tmp_path, phase, payload):
    normal = api(statuses=("completed",))
    def handler(request):
        return httpx.Response(202 if phase == "POST" else 200, content=payload) if request.method == phase else normal(request)
    with pytest.raises(InvestigatorUnavailable):
        await service(tmp_path, handler).investigate(REAL)


@pytest.mark.parametrize("field,value", [
    ("incident_id", "incident:" + "b" * 32), ("investigation_id", "inv_other"),
    ("evidence_live", None), ("evidence_live", 1), ("incident_simulated", None), ("incident_simulated", True),
    ("evidence_badge", "TEST DATA"), ("status", []), ("official_severity", None),
    ("official_severity", "urgent"), ("effective_severity", "medium"),
    ("spoken_summary", 1), ("spoken_summary", "x" * 2001),
    ("spoken_summary", "This is a low severity incident."), ("spoken_summary_source", "status_template"),
])
async def test_final_view_is_bound_and_explicitly_live(tmp_path, field, value):
    normal = api(statuses=("completed",))
    def handler(request):
        response = normal(request)
        if request.method == "GET":
            data = response.json()
            data[field] = value
            return httpx.Response(200, json=data)
        return response
    with pytest.raises(InvestigatorUnavailable):
        await service(tmp_path, handler).investigate(REAL)


async def test_post_identifier_cannot_inject_path(tmp_path):
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(202, json={"investigation_id": "inv_x/../../health", "status": "queued", "evidence_mode": "live", "evidence_badge": "LIVE"})
    with pytest.raises(InvestigatorUnavailable, match="invalid investigation"):
        await service(tmp_path, handler).investigate(REAL)


async def test_response_body_is_bounded_and_redirects_are_not_followed(tmp_path):
    with pytest.raises(InvestigatorUnavailable, match="size limit"):
        await service(tmp_path, lambda _: httpx.Response(202, content=b"x" * (ns.MAX_RESPONSE_BYTES + 1))).investigate(REAL)
    calls = []
    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://example.com/"})
    with pytest.raises(InvestigatorUnavailable, match="302"):
        await service(tmp_path, redirect).investigate(REAL)
    assert len(calls) == 1


async def test_whole_operation_deadline_bounds_a_stalled_request(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "DEADLINE_S", 0.02)
    async def stalled(_):
        await asyncio.Event().wait()
    with pytest.raises(InvestigatorUnavailable, match="no result within"):
        await service(tmp_path, stalled).investigate(REAL)


async def test_default_client_disables_proxies_and_closes_on_cancellation(tmp_path, monkeypatch):
    entered = asyncio.Event()
    async def stalled(_):
        entered.set()
        await asyncio.Event().wait()
    factory, made = httpx.AsyncClient, []
    def create(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        client = factory(transport=httpx.MockTransport(stalled), **kwargs)
        made.append(client)
        return client
    monkeypatch.setattr(ns.httpx, "AsyncClient", create)
    path = tmp_path / "key"
    path.write_text("fake-key")
    path.chmod(0o600)
    s = NemotronService(key_file=str(path))
    task = asyncio.create_task(s.investigate(REAL))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made[0].is_closed


async def test_fullmatch_and_per_call_last_reset(tmp_path):
    s = service(tmp_path, api(statuses=("completed",)))
    await s.investigate(REAL)
    assert s.last["status"] == "completed"
    with pytest.raises(InvestigatorUnavailable, match="live SpacetimeDB"):
        await s.investigate(REAL + "\n")
    assert s.last is None
