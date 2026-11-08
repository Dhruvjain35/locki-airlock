from nemotron_investigator.assessment import ModelAssessment, ground, overclaims, understates_severity


def idx(*ids, kind="device_event"):
    return {i: {"type": ("query_result" if i.startswith("res:") else kind)} for i in ids}


def ma(**kw):
    base = {"risk_assessment": {"rationale": "r", "level": "medium", "confidence": "low"},
            "observed_facts": [], "possible_explanations": [], "missing_information": []}
    base.update(kw)
    return ModelAssessment.model_validate(base)


def test_fact_requires_valid_citation():
    a = ma(observed_facts=[{"fact": "USB connected", "evidence_ids": ["evt:1"]},
                           {"fact": "made up", "evidence_ids": ["evt:404"]},
                           {"fact": "uncited", "evidence_ids": []},
                           {"fact": "partly", "evidence_ids": ["evt:1", "evt:nope"]}])
    fields, rep = ground(a, idx("evt:1"))
    kept = [f["fact"] for f in fields["observed_facts"]]
    assert kept == ["USB connected"]                       # partly-fabricated citation demotes the fact
    assert {u["claim"] for u in fields["unsupported_claims"]} == {"made up", "uncited", "partly"}
    assert rep["citations_total"] == 4 and rep["citations_valid"] == 2 and rep["citation_accuracy"] == 0.5


def test_overclaim_rules():
    assert "unsupported_malware_claim" in overclaims("The USB device is malware.", observed=True)
    assert overclaims("The device could be malware, but there is no evidence.", observed=False) == []
    assert "unsupported_attribution" in overclaims("The unknown person inserted the USB drive.", observed=True)
    assert "unsupported_attribution" in overclaims("The person possibly connected the drive.", observed=True)
    assert overclaims("The person possibly connected the drive.", observed=False) == []
    assert "claims_executed_action" in overclaims("I have locked the workstation.", observed=False)
    assert "claims_executed_action" in overclaims("The screen was locked by the agent.", observed=False)
    assert overclaims("The session was disarmed 420 s after the incident.", observed=True) == []
    assert "unsupported_intent_claim" in overclaims("An attacker exfiltrated data.", observed=True)


def test_overclaimed_fact_demoted_and_flagged():
    a = ma(observed_facts=[{"fact": "The stranger inserted the SanDisk drive.", "evidence_ids": ["evt:1"]}])
    fields, rep = ground(a, idx("evt:1"))
    assert fields["observed_facts"] == []
    assert fields["unsupported_claims"][0]["reason"] == "unsupported_attribution"
    assert rep["overclaim_flag_count"] == 1


def test_actions_always_need_human_approval():
    a = ma(recommended_actions=[{"action": "Lock the workstation", "rationale": "policy"}])
    fields, _ = ground(a, {})
    assert fields["recommended_actions"][0]["requires_human_approval"] is True
    assert fields["recommended_actions"][0]["executed_by_agent"] is False
    assert fields["risk_assessment"]["determined_by"] == "model_advisory"


def test_summary_fallbacks():
    fields, _ = ground(ma(spoken_summary="Short spoken."), {})
    assert fields["summary"] == "Short spoken." and fields["summary_source"] == "model_spoken_summary"
    fields, _ = ground(ma(), {})
    assert fields["summary_source"] == "deterministic" and fields["spoken_summary"]


def test_explanation_flags_only_high_likelihood_unhedged():
    low = ma(possible_explanations=[{"explanation": "An attacker exfiltrated data.", "likelihood": "low",
                                     "supporting_evidence_ids": []}])
    high = ma(possible_explanations=[{"explanation": "An attacker exfiltrated data.", "likelihood": "high",
                                      "supporting_evidence_ids": []}])
    assert ground(low, {})[1]["overclaim_flag_count"] == 0
    assert ground(high, {})[1]["overclaim_flag_count"] == 1



def test_hedges_do_not_launder_overclaims_in_facts():
    for bad in ["The unknown USB device is a Rubber Ducky.", "The unknown USB device is malware.",
                "The device not in the baseline is a keylogger.", "The device may be malware.",
                "An unknown person probably inserted the drive.", "I've locked the workstation.",
                "Locked the workstation after the event."]:
        assert overclaims(bad, observed=True), bad
    for ok in ["No evidence of malware was found in the retrieved events.",
               "The USB device is not in the baseline.", "The session was disarmed 420 s after the incident.",
               "The device could not be identified from the descriptor."]:
        assert overclaims(ok, observed=True) == [], ok


def test_absence_citation_must_state_absence():
    a = ma(observed_facts=[{"fact": "No vision events were recorded in the window.", "evidence_ids": ["res:5.0:x"]},
                           {"fact": "A stranger was seen at the desk.", "evidence_ids": ["res:5.0:x"]}])
    fields, rep = ground(a, idx("res:5.0:x"))
    assert [f["fact"] for f in fields["observed_facts"]] == ["No vision events were recorded in the window."]
    assert fields["observed_facts"][0]["basis"] == "observed_absence"
    assert any(f["code"] == "absence_citation_for_positive_claim" for f in rep["flags"])


def test_flagged_summary_replaced_and_draft_kept():
    a = ma(observed_facts=[{"fact": "USB device connected while armed", "evidence_ids": ["evt:1"]}],
           summary="An intruder plugged a malicious USB device in.", spoken_summary="I have locked the workstation.")
    fields, rep = ground(a, idx("evt:1"))
    assert fields["summary_source"] == "deterministic_due_to_flags"
    assert "locked" not in fields["spoken_summary"].lower() and "human review" in fields["spoken_summary"].lower()
    assert fields["model_drafts"]["spoken_summary"] == "I have locked the workstation."


def test_incident_record_basis():
    a = ma(observed_facts=[{"fact": "The incident rule is unknown_usb", "evidence_ids": ["incident:1"]}])
    fields, _ = ground(a, idx("incident:1", kind="incident"))
    assert fields["observed_facts"][0]["basis"] == "incident_record"



def test_citation_written_inside_sentence_is_repaired_only_for_real_ids():
    a = ma(observed_facts=[{"fact": "The session was armed at incident time citing sess:1", "evidence_ids": []},
                           {"fact": "A device connected citing evt:404", "evidence_ids": []}])
    fields, rep = ground(a, idx("sess:1", kind="session"))
    assert [f["fact"] for f in fields["observed_facts"]] == ["The session was armed at incident time"]
    assert fields["observed_facts"][0]["evidence_ids"] == ["sess:1"]
    assert rep["citations_moved_from_text"] == 1
    assert fields["unsupported_claims"][0]["claim"] == "A device connected citing evt:404"


def test_schema_puts_free_text_last_in_every_object():
    from nemotron_investigator.assessment import ASSESSMENT_JSON_SCHEMA as S
    assert list(S["properties"]["observed_facts"]["items"]["properties"]) == ["evidence_ids", "fact"]
    assert list(S["properties"]["possible_explanations"]["items"]["properties"])[-1] == "explanation"
    assert S["properties"]["recommended_actions"]["items"]["required"] == ["action"]


def test_model_summary_understating_the_official_severity_is_replaced():
    a = ma(risk_assessment={"rationale": "r", "level": "medium", "confidence": "high"},
           summary="Medium risk: a USB drive connected while armed; the owner was nearby.",
           spoken_summary="Medium risk. A USB drive connected while the workstation was armed.")
    fields, rep = ground(a, idx("evt:1"), official_level="high")
    assert fields["summary_source"] == "deterministic_due_to_flags"
    assert fields["summary"].startswith("Official severity high. Nemotron assessed risk medium")
    assert fields["spoken_summary"].startswith("LOCKI rated this incident high severity.")
    assert {f["code"] for f in rep["flags"]} >= {"understates_official_severity"}
    assert fields["model_drafts"]["summary"].startswith("Medium risk:")
    # the same wording is fine when it matches the official level
    fields, rep = ground(a, idx("evt:1"), official_level="medium")
    assert fields["summary_source"] == "model" and not rep["flags"]


def test_understates_severity_catches_reviewed_phrasings():
    for text in ("The severity is assessed as medium.", "This is a medium security risk.", "Risk is considered low.",
                 "The risk here is medium.", "Risk rating: medium.", "a medium-level risk", "Low overall risk.",
                 "negligible risk", "Nemotron rates this incident medium.", "medium\u2011risk incident",
                 "This is not a high-risk event.", "likely a false positive", "a benign event"):
        assert understates_severity(text, "high"), text
    assert not understates_severity("This is not a critical-severity incident.", "high")
    assert understates_severity("This is not a critical-severity incident.", "critical")
    assert not understates_severity("An unknown USB device connected while armed.", "high")
