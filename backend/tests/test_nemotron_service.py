"""LOCKI's client for Mustafa's Nemotron investigator (INTEGRATION.md v0.3), against a stand-in of that HTTP API. Only real incidents
go, always live; LOCKI speaks only the service's own spoken summary, and anything else is reported as unavailable. Plus STOP SPEAKING on
the real speech queue."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.intelligence import nemotron_service as ns
from app.intelligence.nemotron_service import InvestigatorUnavailable, NemotronService
from app.security.contracts import IncidentUpdateV1

from .test_security_spacetime import remote

REAL = "incident:" + "a" * 32
SUMMARY = "LOCKI rated this incident high severity. An unknown USB device was connected. Nemotron's evidence review: medium risk, medium confidence. The official high severity stands. This needs human review."


def service(tmp_path, handler, key="test-key"):
    kf = tmp_path / "investigator.key"
    kf.write_text(key)
    kf.chmod(0o600)
    return NemotronService(url="http://127.0.0.1:18787", key_file=str(kf), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def api(statuses=("running", "completed_with_warnings"), badge="LIVE", post_status=202, mode="live", seen=None):
    states = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path, request.headers.get("authorization"), request.content and json.loads(request.content)))
        if request.method == "POST":
            if post_status != 202:
                return httpx.Response(post_status, json={"detail": "no"})
            return httpx.Response(202, json={"investigation_id": "inv_1", "status": "queued", "evidence_mode": mode, "evidence_badge": badge})
        status = states.pop(0) if len(states) > 1 else states[0]
        return httpx.Response(200, json={"investigation_id": "inv_1", "status": status, "incident_id": REAL, "evidence_mode": mode, "evidence_badge": badge,
                                         "evidence_live": badge == "LIVE", "spoken_summary": SUMMARY if status != "running" else None, "needs_review": True,
                                         "incident_simulated": False, "official_severity": "high", "effective_severity": "high",
                                         "spoken_summary_source": "grounded_facts"})
    return handler


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ns, "POLL_S", 0.01)


async def test_a_real_incident_is_investigated_live_and_the_services_own_summary_comes_back(tmp_path):
    seen = []
    s = service(tmp_path, api(seen=seen))
    view = await s.investigate(REAL)
    assert view["spoken_summary"] == SUMMARY and view["evidence_badge"] == "LIVE" and view["total_ms"] >= 0
    post = seen[0]
    assert post[0] == "POST" and post[2] == "Bearer test-key" and post[3]["evidence_mode"] == "live" and post[3]["incident_id"] == REAL
    assert s.last["investigation_id"] == "inv_1" and s.last["status"] == "completed_with_warnings" and "spoken_summary" not in s.last


@pytest.mark.parametrize("incident", ["incident:fixture-b-person-then-usb", "local-abc123", "", None])
async def test_only_live_spacetimedb_incidents_are_ever_sent(tmp_path, incident):
    s = service(tmp_path, api())
    with pytest.raises(InvestigatorUnavailable, match="live SpacetimeDB"):
        await s.investigate(incident)


@pytest.mark.parametrize("post_status, why", [(401, "key was refused"), (422, "can't take this incident live"), (429, "busy"), (503, "backend is unavailable")])
async def test_refusals_say_why(tmp_path, post_status, why):
    with pytest.raises(InvestigatorUnavailable, match=why):
        await service(tmp_path, api(post_status=post_status)).investigate(REAL)


async def test_no_assessment_and_test_data_are_never_spoken_as_a_live_result(tmp_path):
    with pytest.raises(InvestigatorUnavailable, match="ended failed"):
        await service(tmp_path, api(statuses=("running", "failed"))).investigate(REAL)
    with pytest.raises(InvestigatorUnavailable, match="live evidence"):
        await service(tmp_path, api(badge="TEST DATA", mode="fixture")).investigate(REAL)


async def test_unreachable_tunnel_and_missing_key(tmp_path):
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(InvestigatorUnavailable, match="unreachable"):
        await service(tmp_path, down).investigate(REAL)
    assert not NemotronService(key_file=str(tmp_path / "missing.key")).configured()


# ------------------------------------------------------------------ in the runtime
class StandIn:
    def __init__(self, view=None, error=None):
        self.view, self.error, self.calls, self.last = view, error, [], {"investigation_id": "inv_1"}

    def configured(self):
        return True

    @staticmethod
    def accepts(incident_id):
        return NemotronService.accepts(incident_id)

    async def investigate(self, incident_id, note=""):
        self.calls.append(incident_id)
        if self.error:
            raise InvestigatorUnavailable(self.error)
        return self.view


def real_incident(rt, incident_id=REAL, authoritative=True):
    rt.security.enabled = True
    rt.security.apply_remote_session(remote(session_id="nemotron-session"))
    rt.security._last_incident_update = IncidentUpdateV1(incident_id=incident_id, session_id="nemotron-session", severity="high",
                                                       state="SUSPECTED_INTRUSION", evidence_ids=["e1"], is_fixture=False)
    rt.security.incident = {"incident_id": incident_id, "source": "spacetimedb" if authoritative else "local", "authoritative": authoritative, "state": "SUSPECTED_INTRUSION",
                            "severity": "high", "reason": "correlated_usb_physical", "evidence_ids": ["e1"]}


async def test_what_happened_about_a_real_incident_speaks_nemotrons_summary(rt):
    rt.nemotron_service = StandIn({"spoken_summary": SUMMARY, "status": "completed_with_warnings", "evidence_badge": "LIVE", "investigation_id": "inv_1", "total_ms": 9100,
                                  "incident_id": REAL, "evidence_mode": "live", "evidence_live": True, "incident_simulated": False,
                                  "official_severity": "high", "effective_severity": "high"})
    old = []
    rt.security.what_happened = lambda origin="voice": old.append(origin)  # the older on-Mac investigator must not answer
    real_incident(rt)
    r = await rt.handle_utterance("LOCKI, what happened while I was gone?", source="recorded", stt_confidence=0.95)
    assert r.ok and r.reply == SUMMARY and rt.nemotron_service.calls == [REAL] and old == []
    assert any(h["text"] == SUMMARY for h in rt.speaker.history) or rt.last_reply["text"] == SUMMARY


async def test_when_nemotron_cannot_answer_locki_says_so_and_reads_only_the_record(rt):
    rt.nemotron_service = StandIn(error="the investigator's evidence backend is unavailable")
    real_incident(rt)
    r = await rt.handle_utterance("what happened", source="recorded", stt_confidence=0.95)
    assert not r.ok and "Nemotron couldn't review this one" in (r.reply or "") and "evidence backend is unavailable" in r.reply
    assert "Nemotron's evidence review" not in r.reply  # nothing is presented as Nemotron's assessment


async def test_a_local_only_incident_never_goes_to_the_service(rt):
    rt.nemotron_service = StandIn({"spoken_summary": SUMMARY})
    real_incident(rt, incident_id="local-abc123", authoritative=False)
    await rt.handle_utterance("what happened", source="recorded", stt_confidence=0.95)
    assert rt.nemotron_service.calls == []


async def test_stop_speaking_stops_an_answer_but_a_p0_warning_still_plays():
    from app.voice.tts import P0_SAFETY, P2_REPLY

    from .test_speaker import make

    sp, eng = await make(0.3)
    sp.say("a long ordinary answer", priority=P2_REPLY)
    await asyncio.sleep(0.05)
    sp.say("Security alert. Step away.", priority=P0_SAFETY, force=True)
    sp.interrupt("owner pressed stop speaking")  # what STOP SPEAKING calls
    await asyncio.sleep(0.8)
    assert "Security alert. Step away." in eng.finished and "a long ordinary answer" not in eng.finished
    await sp.close()


FULL_RECORD = {"investigation_id": "inv_1", "incident_id": REAL, "status": "completed_with_warnings", "evidence_mode": "live",
               "result": {"evidence_mode": "live", "evidence_badge": "LIVE", "evidence_provenance": {"live": True}, "incident_simulated": False,
                          "official_severity": {"level": "high"}, "effective_severity": {"level": "high"}, "headline": "Official severity high. Nemotron's evidence review: medium risk.",
                          "observed_facts": [{"fact": "An unknown USB device was connected.", "evidence_ids": ["evt:1"], "basis": "observed"}],
                          "tool_execution_trace": [{"step": 1, "tool": "get_incident", "status": "ok", "live_data": True, "arguments": {"incident_id": REAL}, "result_summary": "x"}],
                          "total_latency_ms": 9120, "conversation": ["the raw model messages"], "untrusted_text_values": ["EvilUSB"], "summary": "..."}}


async def test_the_display_record_is_trimmed_and_holds_no_conversation(tmp_path):
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key" and request.url.path == "/investigations/inv_1"
        return httpx.Response(200, json=FULL_RECORD)

    rec = await service(tmp_path, handler).record("inv_1")
    assert rec["evidence_badge"] == "LIVE" and rec["headline"].startswith("Official severity high") and rec["status"] == "completed_with_warnings"
    assert rec["official_severity"] == "high" and rec["effective_severity"] == "high" and rec["needs_review"] is True
    assert rec["observed_facts"] == [{"fact": "An unknown USB device was connected.", "evidence_ids": ["evt:1"]}]
    assert rec["tools"] == [{"tool": "get_incident", "status": "ok", "live_data": True}] and rec["total_latency_ms"] == 9120
    import json as _json

    dumped = _json.dumps(rec)
    assert "raw model messages" not in dumped and "untrusted_text_values" not in dumped and "arguments" not in dumped


async def test_only_investigation_ids_are_fetched(tmp_path):
    with pytest.raises(InvestigatorUnavailable, match="not an investigation id"):
        await service(tmp_path, api()).record("../health")


def test_the_display_route_is_local_and_needs_the_investigator(settings, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.runtime import LockedInRuntime

    from .test_voice_trials import RecordingTTS

    runtime = LockedInRuntime(settings, persist_settings=False, tts_engine=RecordingTTS(), enable_vision=False, enable_voice=False)
    runtime.nemotron_service = NemotronService(key_file=str(tmp_path / "missing.key"))
    with TestClient(create_app(settings, runtime)) as c:
        assert c.get("/api/security/investigation/inv_1", headers={"origin": "http://evil.example"}).status_code == 403
        assert c.get("/api/security/investigation/inv_1").status_code == 503
        kf = tmp_path / "k.key"
        kf.write_text("test-key")
        kf.chmod(0o600)
        runtime.nemotron_service = NemotronService(url="http://127.0.0.1:18787", key_file=str(kf), client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=FULL_RECORD))))
        body = c.get("/api/security/investigation/inv_1").json()
        assert body["evidence_badge"] == "LIVE" and body["observed_facts"][0]["evidence_ids"] == ["evt:1"]


# ------------------------------------------------------------------ review fixes
async def test_a_failed_run_never_shows_the_previous_runs_live_result(tmp_path):
    good = service(tmp_path, api())
    await good.investigate(REAL)
    assert good.last["evidence_badge"] == "LIVE"
    good._client = httpx.AsyncClient(transport=httpx.MockTransport(api(post_status=503)))
    with pytest.raises(InvestigatorUnavailable):
        await good.investigate(REAL)
    assert good.last["status"] == "unavailable" and good.last["evidence_badge"] is None and "investigation_id" not in good.last


async def test_the_key_only_goes_to_loopback_and_never_through_a_proxy(tmp_path):
    kf = tmp_path / "k.key"
    kf.write_text("k")
    kf.chmod(0o600)
    far = NemotronService(url="http://100.88.230.68:8787", key_file=str(kf))
    assert not far.configured()
    with pytest.raises(InvestigatorUnavailable, match="loopback"):
        await far.investigate(REAL)
    near = NemotronService(url="http://127.0.0.1:18787", key_file=str(kf))
    assert near.configured()
    import inspect

    assert "trust_env=False" in inspect.getsource(NemotronService._new_client)


def test_the_key_file_must_be_owner_only_and_not_a_link(tmp_path):
    kf = tmp_path / "k.key"
    kf.write_text("k")
    kf.chmod(0o644)
    assert not NemotronService(key_file=str(kf)).configured()
    kf.chmod(0o600)
    assert NemotronService(key_file=str(kf)).configured()
    link = tmp_path / "link.key"
    link.symlink_to(kf)
    assert not NemotronService(key_file=str(link)).configured()


async def test_an_unreadable_reply_is_reported_not_raised(tmp_path):
    def junk(request):
        return httpx.Response(202, text="<html>not json</html>")

    with pytest.raises(InvestigatorUnavailable, match="could not be read"):
        await service(tmp_path, junk).investigate(REAL)
    with pytest.raises(InvestigatorUnavailable, match="bounded valid token"):
        await service(tmp_path, api(), key="kéy-with-non-ascii").investigate(REAL)


def test_incident_ids_must_match_exactly():
    assert NemotronService.accepts(REAL) and not NemotronService.accepts(REAL + "\n") and not NemotronService.accepts(REAL.upper())


async def test_asking_twice_while_nemotron_works_starts_one_run(rt):
    release = asyncio.Event()

    class Slow(StandIn):
        async def investigate(self, incident_id, note=""):
            self.calls.append(incident_id)
            await release.wait()
            return {"spoken_summary": SUMMARY, "status": "completed", "evidence_badge": "LIVE", "investigation_id": "inv_1", "total_ms": 1,
                    "incident_id": REAL, "evidence_mode": "live", "evidence_live": True, "incident_simulated": False, "official_severity": "high", "effective_severity": "high"}

    rt.nemotron_service = Slow()
    real_incident(rt)
    first = asyncio.ensure_future(rt.handle_utterance("what happened", source="recorded", stt_confidence=0.95))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(rt.handle_utterance("what happened while I was gone", source="recorded", stt_confidence=0.95))
    await asyncio.sleep(0.01)
    assert rt.nemotron_service.calls == [REAL] and not second.done()
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert all(result.reply == SUMMARY for result in results)
    assert sum(turn["text"] == SUMMARY for turn in rt.speaker.history) <= 1
