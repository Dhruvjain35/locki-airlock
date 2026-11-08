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

REAL = "incident:" + "a" * 32
SUMMARY = "LOCKI rated this incident high severity. An unknown USB device was connected. Nemotron's evidence review: medium risk, medium confidence. The official high severity stands. This needs human review."


def service(tmp_path, handler, key="test-key"):
    kf = tmp_path / "investigator.key"
    kf.write_text(key)
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
    rt.security.incident = {"incident_id": incident_id, "source": "spacetimedb" if authoritative else "local", "authoritative": authoritative, "state": "SUSPECTED_INTRUSION",
                            "severity": "high", "reason": "correlated_usb_physical", "evidence_ids": ["e1"]}


async def test_what_happened_about_a_real_incident_speaks_nemotrons_summary(rt):
    rt.nemotron_service = StandIn({"spoken_summary": SUMMARY, "status": "completed_with_warnings", "evidence_badge": "LIVE", "investigation_id": "inv_1", "total_ms": 9100})
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
