"""Assessment schema (constrained decoding) and deterministic grounding checks.

The model drafts the interpretation; this module decides what may be presented
as OBSERVED. Rules (all deterministic):
  * A fact survives only if it cites at least one evidence id and EVERY cited id
    was committed by a successful tool call in this investigation.
  * A fact that cites only "absence" result ids must actually state an absence.
  * Observed facts may not speculate, and may not assert malware, intent,
    attribution of an action to a person, or actions the agent took. Hedging
    words do not exempt such claims in facts; only explicit negations do.
  * Explanations are hypotheses (they carry a likelihood); a malicious
    hypothesis is flagged only when rated HIGH without a scoped hedge.
  * If the summary or spoken summary trips a rule, it is replaced by a
    deterministic summary built from grounded facts; the model draft is kept
    under `model_drafts` for audit. Same for a flagged risk rationale.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

LIKELIHOOD = ["low", "medium", "high"]
RISK = ["low", "medium", "high", "critical"]

# Layout rule for constrained decoding (measured 2026-11-08, see CONTEXT.md): inside
# every object the FREE-TEXT field comes LAST and structured fields (ids, enums) come
# first. A required key after a sentence makes the model want to close the object
# while the grammar forbids it, and decoding degenerates into whitespace. Text-last
# layout: 13/13 valid, 0 loops on replayed conversations (incl. one that had failed).
_FACTS = {"type": "array", "maxItems": 7, "items": {
    "type": "object",
    "properties": {"evidence_ids": {"type": "array", "items": {"type": "string"}},
                   "fact": {"type": "string"}},
    "required": ["evidence_ids", "fact"]}}
_EXPLANATIONS = {"type": "array", "maxItems": 4, "items": {
    "type": "object",
    "properties": {"supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
                   "likelihood": {"type": "string", "enum": LIKELIHOOD},
                   "explanation": {"type": "string"}},
    "required": ["supporting_evidence_ids", "likelihood", "explanation"]}}
_ACTIONS = {"type": "array", "maxItems": 4, "items": {
    "type": "object",
    "properties": {"action": {"type": "string"}, "rationale": {"type": "string"}},
    "required": ["action"]}}
_RISK = {"type": "object", "properties": {
    "rationale": {"type": "string"},
    "level": {"type": "string", "enum": RISK},
    "confidence": {"type": "string", "enum": LIKELIHOOD}},
    "required": ["rationale", "level", "confidence"]}

# Key order matters for constrained decoding on this model. Measured on
# 2026-11-07 (bench notes in CONTEXT.md): when a REQUIRED key remains but the
# model "wants" to close the object, grammar-constrained decoding emits
# whitespace until the token cap (observed after risk.confidence -> rationale and
# after summary -> spoken_summary). Risk first (rationale -> level -> confidence),
# then the required evidence lists, then OPTIONAL trailing keys: 9/9 valid, 0 loops.
ASSESSMENT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "risk_assessment": _RISK,
        "observed_facts": _FACTS,
        "possible_explanations": _EXPLANATIONS,
        "missing_information": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "recommended_actions": _ACTIONS,
        "summary": {"type": "string"},
        "spoken_summary": {"type": "string"},
    },
    "required": ["risk_assessment", "observed_facts", "possible_explanations", "missing_information"],
}


def _without_required(x):
    if isinstance(x, dict):
        return {k: _without_required(v) for k, v in x.items() if k != "required"}
    if isinstance(x, list):
        return [_without_required(v) for v in x]
    return x


# Fallback grammar for a retry: closing is always legal, so it cannot loop.
ASSESSMENT_JSON_SCHEMA_RELAXED: dict = _without_required(ASSESSMENT_JSON_SCHEMA)


class Fact(BaseModel):
    fact: str = Field(max_length=600)
    evidence_ids: list[str] = []


class Explanation(BaseModel):
    explanation: str = Field(max_length=600)
    likelihood: Literal["low", "medium", "high"]
    supporting_evidence_ids: list[str] = []


class Action(BaseModel):
    action: str = Field(max_length=400)
    rationale: str = Field(default="", max_length=600)


class Risk(BaseModel):
    level: Literal["low", "medium", "high", "critical"]
    confidence: Literal["low", "medium", "high"]
    rationale: str = Field(max_length=800)


class ModelAssessment(BaseModel):
    risk_assessment: Risk
    observed_facts: list[Fact] = Field(max_length=25)
    possible_explanations: list[Explanation] = Field(max_length=12)
    missing_information: list[str] = Field(max_length=15)
    recommended_actions: list[Action] = Field(default=[], max_length=10)
    summary: str = Field(default="", max_length=1200)
    spoken_summary: str = Field(default="", max_length=600)


# ------------------------------------------------- relaxed contract (retry path)
class _RExplanation(BaseModel):
    explanation: str = Field(max_length=600)
    likelihood: Literal["low", "medium", "high"] = "low"
    supporting_evidence_ids: list[str] = []


class _RRisk(BaseModel):
    level: Literal["low", "medium", "high", "critical"] | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    rationale: str = Field(default="", max_length=800)


class RelaxedModelAssessment(BaseModel):
    """Acceptance contract that matches ASSESSMENT_JSON_SCHEMA_RELAXED (no required keys)."""
    risk_assessment: _RRisk | None = None
    observed_facts: list[Fact] = Field(default=[], max_length=25)
    possible_explanations: list[_RExplanation] = Field(default=[], max_length=12)
    missing_information: list[str] = Field(default=[], max_length=15)
    recommended_actions: list[Action] = Field(default=[], max_length=10)
    summary: str = Field(default="", max_length=1200)
    spoken_summary: str = Field(default="", max_length=600)

    def omitted(self, raw_keys: set[str]) -> list[str]:
        out = [k for k in ("risk_assessment", "observed_facts", "possible_explanations", "missing_information")
               if k not in raw_keys]
        if self.risk_assessment is not None and self.risk_assessment.level is None:
            out.append("risk_assessment.level")
        return out


# ---------------------------------------------------------------- overclaim rules
_SCOPED_HEDGE = re.compile(
    r"\b(may|might|could|possibly|possible|potentially|potential|suspected|suspect|likely|unlikely|"
    r"perhaps|presumably|consistent with|suggests?|appears? to|seems? to|hypothes\w*|if confirmed|"
    r"cannot (confirm|determine|establish|rule out)|unverified|unconfirmed|not (necessarily|confirmed|proven|"
    r"established)|no evidence (of|that)|does not (prove|confirm|show)|doesn't (prove|confirm|show)|"
    r"is not proof|not proof)\b", re.I)
_NEGATED_CLAIM = re.compile(
    r"\b(no evidence (of|that)|without evidence|not (necessarily |confirmed |proven |known to be |shown to be )?"
    r"(an? )?(malware|malicious|attack|intru\w*|exfiltrat\w*)|no (malware|malicious|signs? of)|"
    r"cannot (confirm|determine|establish)|does not (prove|show|confirm|indicate)|"
    r"doesn't (prove|show|confirm|indicate)|is not proof|not proof)\b", re.I)
_SPECULATIVE = re.compile(
    r"\b(may|might|possibly|probably|perhaps|likely|unlikely|potentially|suspected|presumably|"
    r"seems? to|appears? to)\b|\bcould\b(?!\s+not)", re.I)
_MALWARE = re.compile(
    r"\b(malware|virus|trojan|ransomware|keylogger|spyware|rootkit|backdoor|worm|infected|infection|"
    r"rubber ?ducky|bad ?usb|malicious (device|payload|software|usb|code|program|firmware|implant))\b", re.I)
_ATTRIBUTION = re.compile(
    r"\b(person|individual|intruder|stranger|someone|somebody|visitor|he|she|they|attacker|"
    r"unrecognized (person|individual|face|user)|unknown (person|individual|face|user))\b"
    r"[^.;]{0,80}\b(inserted|plugged|connected|attached|installed|launched|opened|ran)\b", re.I)
_INTENT = re.compile(r"\b(malicious intent|with intent|intended to|intruder|hacker|attacker|"
                     r"breach(ed)?|compromised|exfiltrat\w*|stole|stealing|theft)\b", re.I)
_ACT_VERBS = (r"locked|unlocked|disarmed|armed|blocked|quarantined|ejected|deleted|disabled|shut down|isolated|"
              r"wiped|approved|resolved|acknowledged|revoked|removed|killed|terminated")
_EXECUTED = re.compile(
    r"\b(i|we|the agent|the investigator|this investigator|nemotron)"
    r"((\s+|')(have|has|had|ve|d|just|already|now|successfully|also|then|immediately))*\s+"
    rf"({_ACT_VERBS})\b|"
    rf"\b({_ACT_VERBS}) by (me|us|the agent|the investigator|nemotron)\b", re.I)
_EXECUTED_FRAGMENT = re.compile(rf"^\s*({_ACT_VERBS})\s+(the|this|that|a|an|its|their)\b", re.I)
_ABSENCE = re.compile(
    r"\b(no|not|none|never|zero|without|absent|absence|missing|empty|nothing|lack\w*|unavailable|"
    r"did not|didn't|was not|were not|wasn't|weren't|could not|cannot|not found|not recorded|"
    r"not observed|not detected|0)\b", re.I)


_ID_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z_]{1,24}:[A-Za-z0-9:_\-.@/+=]{1,128}")


def _repair_citation(text: str, retrieved: set[str]) -> tuple[list[str], str]:
    """Move retrieved evidence ids that the model wrote INSIDE the sentence into the
    citation list. Only ids that exist in the committed evidence index qualify."""
    ids: list[str] = []
    for m in _ID_TOKEN.finditer(text):
        tok = m.group(0).rstrip(".,;:)")
        if tok in retrieved and tok not in ids:
            ids.append(tok)
    if not ids:
        return [], text
    cleaned = text
    for i in ids:
        cleaned = re.sub(rf"\s*(\(|\[)?\s*(citing|cite|see|per|from|evidence:?)?\s*{re.escape(i)}\s*(\)|\])?", "", cleaned,
                         flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip().rstrip(",;:") or text
    return ids, cleaned


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?;])\s+", text) if s.strip()]


# ---------------------------------------------------------------- official-severity guard
# Model text must never present an incident as less severe than LOCKI's official severity.
# This check is deliberately broad: a false positive only means a deterministic sentence is
# used instead of the model's wording.
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_HYPHENS = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u00ad"), "-")
_LEVEL_WORD_LEVEL = {"negligible": "low", "minimal": "low", "minor": "low", "low": "low",
                     "moderate": "medium", "medium": "medium", "elevated": "high", "high": "high",
                     "severe": "high", "critical": "critical"}
_LEVEL_TOKEN = re.compile(r"\b(negligible|minimal|minor|low|moderate|medium|elevated|high|severe|critical)\b"
                          r"(?![\s-]*(?:confidence|certainty|likelihood|probability))", re.I)
_NEGATION_BEFORE = re.compile(r"(?:\bnot|n't|\bnever|\bbelow|\bless\s+than|\blower\s+than|\bunder|"
                              r"\brather\s+than|\binstead\s+of)\W+(?:\w+\W+){0,2}$", re.I)
_DISMISSAL = re.compile(r"\b(benign|harmless|innocuous|false[\s-]+(?:positive|alarm)|no\s+(?:real\s+)?"
                        r"(?:risk|threat|danger|concern)|not\s+(?:a\s+|an\s+)?(?:real\s+)?(?:threat|risk|concern)|"
                        r"safe\s+to\s+(?:ignore|dismiss|close)|nothing\s+to\s+worry|no\s+(?:further\s+)?action\s+"
                        r"(?:is\s+)?(?:needed|required)|can\s+(?:safely\s+)?be\s+(?:ignored|dismissed|closed))\b", re.I)


def understates_severity(text: str, official_level: str | None) -> bool:
    """True if `text` names a level below the official one, negates the official level, or
    dismisses the incident while LOCKI rated it medium or higher. With no recognized official
    level, any level word or dismissal counts (nothing can be compared against)."""
    t = (text or "").translate(_HYPHENS)
    if official_level not in SEVERITY_ORDER:
        return bool(_LEVEL_TOKEN.search(t) or _DISMISSAL.search(t))
    o = SEVERITY_ORDER[official_level]
    for m in _LEVEL_TOKEN.finditer(t):
        lvl = SEVERITY_ORDER[_LEVEL_WORD_LEVEL[m.group(1).lower()]]
        if lvl < o:
            return True
        if lvl <= o and _NEGATION_BEFORE.search(t[max(0, m.start() - 48):m.start()]):
            return True  # "not a high-risk event" for a high-severity incident
    return o >= SEVERITY_ORDER["medium"] and bool(_DISMISSAL.search(t))


def overclaims(text: str, *, observed: bool) -> list[str]:
    """Rule codes violated by `text`. `observed` = rules for observed facts (no hedge exemption)."""
    codes: list[str] = []
    for s in _sentences(text):
        negated = bool(_NEGATED_CLAIM.search(s))
        hedged = bool(_SCOPED_HEDGE.search(s)) or negated
        exempt = negated if observed else hedged
        if _MALWARE.search(s) and not exempt:
            codes.append("unsupported_malware_claim")
        if _ATTRIBUTION.search(s) and (observed or not hedged):
            codes.append("unsupported_attribution")
        if _INTENT.search(s) and not exempt:
            codes.append("unsupported_intent_claim")
        if _EXECUTED.search(s) or _EXECUTED_FRAGMENT.search(s):
            codes.append("claims_executed_action")
        if observed and _SPECULATIVE.search(s) and not negated:
            codes.append("speculative_observation")
    return sorted(set(codes))


def _deterministic_summary(level: str, confidence: str, facts: list[dict],
                           official: str | None = None) -> tuple[str, str]:
    # The official severity (incident record) leads; Nemotron's level is reported as its own view.
    lead = ((f"Official severity {official}. " if official else "")
            + f"Nemotron assessed risk {level} with {confidence} confidence.")
    spoken_lead = ((f"LOCKI rated this incident {official} severity. " if official else "")
                   + f"Nemotron's review: {level} risk, {confidence} confidence.")
    if facts:
        body = " ".join(f["fact"].rstrip(".")[:140] + "." for f in facts[:2])
        spoken_fact = facts[0]["fact"].rstrip(".")[:140] + "."
    else:
        body = "No observation could be grounded in retrieved evidence."
        spoken_fact = body
    return (f"{lead} {body} Human review is required.", f"{spoken_lead} {spoken_fact} This needs human review.")


def ground(ma: ModelAssessment | RelaxedModelAssessment, evidence_index: dict[str, dict], *,
           omitted: list[str] | None = None, official_level: str | None = None) -> tuple[dict, dict]:
    """Apply deterministic grounding. Returns (assessment_fields, validation_report)."""
    retrieved = set(evidence_index)
    absence_ids = {k for k, v in evidence_index.items() if v.get("type") == "query_result"}
    incident_ids = {k for k, v in evidence_index.items() if v.get("type") == "incident"}
    flags: list[dict] = []
    unsupported: list[dict] = []
    facts_out: list[dict] = []
    cited_total = cited_valid = 0

    repaired = 0
    for i, f in enumerate(ma.observed_facts):
        ids = [x.strip() for x in f.evidence_ids if x and x.strip()]
        fact_text = f.fact
        if not ids:
            moved, fact_text = _repair_citation(f.fact, retrieved)
            if moved:
                ids, repaired = moved, repaired + 1
                flags.append({"field": "observed_facts", "index": i, "code": "citation_moved_from_text",
                              "ids": moved})
        f = Fact(fact=fact_text, evidence_ids=ids)
        valid = [x for x in ids if x in retrieved]
        invalid = [x for x in ids if x not in retrieved]
        cited_total += len(ids)
        cited_valid += len(valid)
        if invalid:
            flags.append({"field": "observed_facts", "index": i, "code": "invalid_citation", "ids": invalid})
            unsupported.append({"claim": f.fact, "reason": "cites evidence that was not retrieved", "cited_ids": ids})
            continue
        if not valid:
            unsupported.append({"claim": f.fact, "reason": "no valid evidence citation", "cited_ids": ids})
            continue
        codes = overclaims(f.fact, observed=True)
        if codes:
            for c in codes:
                flags.append({"field": "observed_facts", "index": i, "code": c, "text": f.fact})
            unsupported.append({"claim": f.fact, "reason": ",".join(codes), "cited_ids": valid})
            continue
        if set(valid) <= absence_ids:
            if not _ABSENCE.search(f.fact):
                flags.append({"field": "observed_facts", "index": i, "code": "absence_citation_for_positive_claim",
                              "text": f.fact})
                unsupported.append({"claim": f.fact, "reason": "cites only an empty result but asserts a "
                                                               "positive observation", "cited_ids": valid})
                continue
            basis = "observed_absence"
        elif set(valid) <= incident_ids:
            basis = "incident_record"
        else:
            basis = "observed"
        facts_out.append({"fact": f.fact, "evidence_ids": valid, "basis": basis})

    expl_out = []
    for i, e in enumerate(ma.possible_explanations):
        ids = [x.strip() for x in e.supporting_evidence_ids if x and x.strip()]
        valid = [x for x in ids if x in retrieved]
        invalid = [x for x in ids if x not in retrieved]
        cited_total += len(ids)
        cited_valid += len(valid)
        if invalid:
            flags.append({"field": "possible_explanations", "index": i, "code": "invalid_citation", "ids": invalid})
        # Explanations are hypotheses by construction (they carry a likelihood). A malicious
        # hypothesis is only an overclaim when the model rates it HIGH without a scoped hedge.
        codes = overclaims(e.explanation, observed=False)
        if e.likelihood != "high":
            codes = [c for c in codes if c == "claims_executed_action"]
        for c in codes:
            flags.append({"field": "possible_explanations", "index": i, "code": c, "text": e.explanation,
                          "likelihood": e.likelihood})
        expl_out.append({"explanation": e.explanation, "likelihood": e.likelihood,
                         "supporting_evidence_ids": valid, "basis": "inferred"})

    actions_out = []
    for i, a in enumerate(ma.recommended_actions):
        if "claims_executed_action" in overclaims(a.action + ". " + a.rationale, observed=False):
            flags.append({"field": "recommended_actions", "index": i, "code": "claims_executed_action",
                          "text": a.action})
        actions_out.append({"action": a.action, "rationale": a.rationale,
                            "requires_human_approval": True, "executed_by_agent": False})

    model_drafts: dict = {}
    risk = ma.risk_assessment
    if risk is None or getattr(risk, "level", None) is None:
        flags.append({"field": "risk_assessment", "code": "model_omitted_risk"})
        risk_out = {"level": "undetermined", "confidence": "low",
                    "rationale": "[system] the model did not provide a risk assessment", "determined_by": "system"}
    else:
        risk_out = {"level": risk.level, "confidence": risk.confidence, "rationale": risk.rationale,
                    "determined_by": "model_advisory"}
        r_codes = overclaims(risk.rationale, observed=False)
        for c in r_codes:
            flags.append({"field": "risk_assessment.rationale", "code": c, "text": risk.rationale})
        if r_codes:
            model_drafts["risk_rationale"] = risk.rationale
            risk_out["rationale"] = f"[system] model rationale withheld by grounding ({', '.join(r_codes)})"
    risk_out["note"] = "advisory only; deterministic policy and human approval decide actions"

    summary, spoken, summary_source = ma.summary.strip(), ma.spoken_summary.strip(), "model"
    s_codes = sorted(set(overclaims(summary, observed=False)) | set(overclaims(spoken, observed=False)))
    for fld, txt in (("summary", summary), ("spoken_summary", spoken)):
        for c in overclaims(txt, observed=False):
            flags.append({"field": fld, "code": c, "text": txt})
        if official_level is not None and txt and understates_severity(txt, official_level):
            # The model's wording presents the incident as less severe than LOCKI's official level.
            flags.append({"field": fld, "code": "understates_official_severity", "text": txt})
            s_codes = sorted(set(s_codes) | {"understates_official_severity"})
    det_summary, det_spoken = _deterministic_summary(risk_out["level"], risk_out["confidence"], facts_out,
                                                     official=official_level)
    if s_codes:
        model_drafts.update({"summary": summary, "spoken_summary": spoken})
        summary, spoken, summary_source = det_summary, det_spoken, "deterministic_due_to_flags"
    else:
        if not summary and spoken:
            summary, summary_source = spoken, "model_spoken_summary"
        if not summary:
            summary, summary_source = det_summary, "deterministic"
        if not spoken:
            spoken = " ".join(_sentences(summary)[:2])

    for k in omitted or []:
        flags.append({"field": k, "code": "model_omitted_field"})

    fields = {
        "summary": summary,
        "spoken_summary": spoken,
        "summary_source": summary_source,
        "observed_facts": facts_out,
        "possible_explanations": expl_out,
        "missing_information": list(ma.missing_information),
        "recommended_actions": actions_out,
        "risk_assessment": risk_out,
        "unsupported_claims": unsupported,
        "model_drafts": model_drafts,
    }
    report = {
        "facts_proposed": len(ma.observed_facts),
        "facts_grounded": len(facts_out),
        "facts_rejected": len(unsupported),
        "citations_total": cited_total,
        "citations_valid": cited_valid,
        "citation_accuracy": round(cited_valid / cited_total, 4) if cited_total else None,
        "flags": flags,
        "citations_moved_from_text": repaired,
        "overclaim_flag_count": sum(1 for f in flags if f["code"] not in ("invalid_citation", "model_omitted_field",
                                                                          "citation_moved_from_text")),
    }
    return fields, report
