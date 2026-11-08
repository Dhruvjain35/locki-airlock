"""Live-case enrichment races with an in-memory service; no inference or hardware."""

import asyncio
import time

import pytest

from app.intelligence.nemotron_service import InvestigatorUnavailable
from app.security.contracts import IncidentUpdateV1, Severity
from app.security.session import ArmedSession, ResponsePolicy

REAL = "incident:" + "a" * 32
OTHER = "incident:" + "b" * 32
SUMMARY = "LOCKI rated this incident high severity. This needs human review."


def current_case(rt, *, incident_id=REAL, severity="high", simulated=False, session_id="case-session"):
    now = int(time.time() * 1000)
    rt.security.enabled = True
    rt.security.session = ArmedSession(session_id, now, now + 60_000, {}, ResponsePolicy(), "spacetimedb")
    rt.security._last_incident_update = IncidentUpdateV1(
        incident_id=incident_id, session_id=session_id, severity=severity,
        state="SUSPECTED_INTRUSION", evidence_ids=["event-1"], is_fixture=simulated,
        updated_at=now,
    )
    rt.security.incident = {"incident_id": incident_id, "source": "spacetimedb", "authoritative": True,
                            "severity": severity, "state": "SUSPECTED_INTRUSION", "evidence_ids": ["event-1"]}


class DeferredInvestigator:
    def __init__(self, *, configured=True, error=None, **view):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.is_configured = configured
        self.error = error
        self.last = {"investigation_id": "wrong-other-call", "incident_id": OTHER}
        self.view = {"incident_id": REAL, "investigation_id": "inv_our_case", "status": "completed_with_warnings",
                     "evidence_mode": "live", "evidence_badge": "LIVE", "evidence_live": True,
                     "incident_simulated": False, "official_severity": "high", "effective_severity": "high",
                     "spoken_summary": SUMMARY, "spoken_summary_source": "grounded_facts", "total_ms": 1,
                     **view}

    def configured(self):
        return self.is_configured

    def accepts(self, value):
        return value in (REAL, OTHER)

    async def investigate(self, incident_id):
        self.calls.append(incident_id)
        self.started.set()
        await self.release.wait()
        if self.error:
            raise InvestigatorUnavailable(self.error)
        return dict(self.view)


async def question(rt):
    return await rt.handle_utterance("LOCKI, what happened?", source="recorded", stt_confidence=1.0)


def summaries(rt):
    return [h for h in rt.speaker.history if h.get("text") == SUMMARY]


async def test_fresh_case_uses_own_result_metadata_and_never_second_investigator(rt, monkeypatch):
    current_case(rt)
    service = DeferredInvestigator()
    rt.nemotron_service = service
    monkeypatch.setattr(rt.security, "what_happened", lambda **kw: pytest.fail("second model forbidden"))
    task = asyncio.create_task(question(rt))
    await service.started.wait()
    service.release.set()
    result = await task
    assert result.ok and result.reply == SUMMARY and service.calls == [REAL]
    assert result.data["nemotron"]["investigation_id"] == "inv_our_case"
    assert result.data["nemotron"]["incident_id"] == REAL
    assert "wrong-other-call" not in str(result.to_dict())


async def test_concurrent_same_case_is_singleflight_and_speaks_once(rt):
    current_case(rt)
    service = DeferredInvestigator()
    rt.nemotron_service = service
    first = asyncio.create_task(question(rt))
    await service.started.wait()
    second = asyncio.create_task(question(rt))
    await asyncio.sleep(0)
    assert service.calls == [REAL]
    service.release.set()
    a, b = await asyncio.gather(first, second)
    assert a.ok and b.ok and a is not b and a.data is not b.data
    assert len(summaries(rt)) == 1


async def test_cancelled_waiter_does_not_cancel_shared_investigation(rt):
    current_case(rt)
    service = DeferredInvestigator()
    rt.nemotron_service = service
    first = asyncio.create_task(question(rt))
    await service.started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not rt._nemotron_task.done()
    second = asyncio.create_task(question(rt))
    await asyncio.sleep(0)
    service.release.set()
    assert (await second).ok and service.calls == [REAL]


@pytest.mark.parametrize("change", ["stop", "barge", "new_turn", "p0"])
async def test_interruption_suppresses_late_reply_without_blocking_warning(rt, change):
    current_case(rt)
    service = DeferredInvestigator()
    rt.nemotron_service = service
    task = asyncio.create_task(question(rt))
    await service.started.wait()
    if change == "stop":
        rt.stop_speaking()
    elif change == "barge":
        rt._barge_in({})
    elif change == "new_turn":
        await rt.handle_utterance("how much time is left", source="recorded")
    else:
        assert rt.speaker.say("Security alert. Step away.", priority=0, force=True)
        await asyncio.sleep(0.01)
        assert any(h.get("text") == "Security alert. Step away." for h in rt.speaker.history)
    service.release.set()
    result = await task
    assert not result.ok and result.reply is None and not summaries(rt)


@pytest.mark.parametrize("change", ["new_case", "severity", "session", "disarm", "evidence", "simulation"])
async def test_changed_authoritative_context_withholds_old_assessment(rt, change):
    current_case(rt, severity="medium")
    service = DeferredInvestigator(official_severity="medium")
    rt.nemotron_service = service
    task = asyncio.create_task(question(rt))
    await service.started.wait()
    if change == "new_case":
        current_case(rt, incident_id=OTHER)
    elif change == "severity":
        rt.security.incident["severity"] = "high"
        rt.security._last_incident_update = rt.security._last_incident_update.model_copy(update={"severity": Severity.HIGH})
    elif change == "session":
        current_case(rt, session_id="replacement-session")
    elif change == "disarm":
        rt.security.session = None
    elif change == "evidence":
        rt.security.incident["evidence_ids"].append("new-event")
    else:
        rt.security._last_incident_update = rt.security._last_incident_update.model_copy(update={"is_fixture": True})
    service.release.set()
    result = await task
    assert not result.ok and result.reply != SUMMARY and not summaries(rt)


@pytest.mark.parametrize("reason", ["missing_key", "fixture", "unknown_update", "wrong_update", "unknown_session"])
async def test_unverified_or_unconfigured_real_case_uses_local_record_without_models(rt, monkeypatch, reason):
    current_case(rt, simulated=reason == "fixture")
    service = DeferredInvestigator(configured=reason != "missing_key")
    rt.nemotron_service = service
    monkeypatch.setattr(rt.security, "what_happened", lambda **kw: pytest.fail("no second investigator"))
    if reason == "unknown_update":
        rt.security._last_incident_update = None
    elif reason == "wrong_update":
        rt.security._last_incident_update = rt.security._last_incident_update.model_copy(update={"incident_id": OTHER})
    elif reason == "unknown_session":
        rt.security.session = None
    result = await question(rt)
    assert not result.ok and "From the local record" in result.reply and service.calls == []


async def test_service_failure_never_uses_another_calls_metadata_or_a_second_model(rt, monkeypatch):
    current_case(rt)
    service = DeferredInvestigator(error="service unavailable")
    rt.nemotron_service = service
    monkeypatch.setattr(rt.security, "what_happened", lambda **kw: pytest.fail("second investigator"))
    service.release.set()
    result = await question(rt)
    assert not result.ok and result.data["nemotron"]["incident_id"] == REAL
    assert "wrong-other-call" not in str(result.to_dict())


async def test_returned_lower_official_severity_never_spoken(rt):
    current_case(rt, severity="high")
    service = DeferredInvestigator(official_severity="medium")
    rt.nemotron_service = service
    service.release.set()
    result = await question(rt)
    assert not result.ok and not summaries(rt)


async def test_invalid_authoritative_id_never_falls_through_to_second_model(rt, monkeypatch):
    current_case(rt)
    rt.security.incident["incident_id"] = "unverified-id"
    service = DeferredInvestigator()
    rt.nemotron_service = service
    monkeypatch.setattr(rt.security, "what_happened", lambda **kw: pytest.fail("no second investigator"))
    result = await question(rt)
    assert not result.ok and service.calls == []
    assert "LOCKI rated the current incident high severity." in result.reply
    assert result.data["nemotron"]["incident_id"] == "unverified"
