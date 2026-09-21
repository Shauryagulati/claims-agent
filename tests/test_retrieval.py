from dataclasses import replace
from datetime import date

import pytest

from app import config
from app.retrieval import (
    ALTERNATIVE_KEYWORDS,
    FOLLOWUP_INTENTS,
    Fact,
    claim_facts,
    fmt_date,
    guidance_facts,
    render,
    select,
)


@pytest.fixture
def denied(store):
    return store.claim_by_id("CLM-7710")


@pytest.fixture
def closed(store):
    return store.claim_by_id("CLM-7412")


@pytest.fixture
def irene(store):
    return store.claim_by_id("CLM-7915")


def texts(facts, prefix=""):
    return [f.text for f in facts if f.source.startswith(prefix)]


def sources(facts):
    return [f.source for f in facts]


def test_fmt_date():
    assert fmt_date(date(2026, 10, 1)) == "October 1, 2026"
    assert fmt_date(date(2026, 1, 5)) == "January 5, 2026"


# claim facts

def test_denied_claim_facts(denied, store):
    facts = claim_facts(denied, store)
    t = "\n".join(texts(facts, "claim"))
    assert "Claim ID: CLM-7710" in t
    assert "Type: healthcare" in t
    assert "Status: denied" in t
    assert f"Filed: {fmt_date(denied.created_at)}" in t
    assert "Denial reason: the review file did not include the lab result letter" in t
    assert "Documents needed: lab result letter, visit summary" in t


def test_deadline_days_derive_from_fixture_and_demo_now(denied, store):
    # Never a literal: if the fixture or DEMO_NOW moves, this must move with it.
    days = (denied.appeal_deadline - config.DEMO_NOW).days
    assert days > 0
    t = "\n".join(texts(claim_facts(denied, store), "claim"))
    assert f"Appeal deadline: {fmt_date(denied.appeal_deadline)} ({days} days from today)" in t


def test_closed_claim_facts_omit_denial_lines(closed, store):
    facts = claim_facts(closed, store)
    t = "\n".join(texts(facts, "claim"))
    assert "Status: closed" in t
    assert "Denial reason" not in t
    assert "Documents needed" not in t
    assert "Appeal deadline" not in t


def test_monetary_fields_carry_schema_descriptions(closed, store):
    facts = claim_facts(closed, store)
    t = "\n".join(texts(facts, "claim"))
    assert "Net pay: 915.00 USD" in t
    assert "Allowed max amount: 950.00 USD" in t
    schema = {f.source: f.text for f in facts if f.source.startswith("schema:")}
    assert set(schema) == {
        "schema:expected_reimbursement_amount",
        "schema:allowed_max_amount",
        "schema:net_pay",
        "schema:net_fee",
    }
    assert "finalized" in schema["schema:net_pay"]


def test_past_deadline_reads_as_passed(store):
    past = config.DEMO_NOW.replace(month=config.DEMO_NOW.month - 1)
    c = replace(store.claim_by_id("CLM-7710"), appeal_deadline=past)
    t = "\n".join(texts(claim_facts(c, store), "claim"))
    assert f"Appeal deadline: {fmt_date(past)} (passed)" in t


# guideline entries

def test_submission_method_fills_placeholders(denied, store):
    facts = guidance_facts("document_submission", "how do I submit these?", denied, store)
    assert "guideline:submission_method" in sources(facts)
    t = next(f.text for f in facts if f.source == "guideline:submission_method")
    assert "On claim CLM-7710" in t
    assert "lab result letter, visit summary" in t
    assert "{" not in t


def test_processing_time_fills_average(denied, store):
    facts = guidance_facts("document_submission", "how long after I submit?", denied, store)
    t = next(f.text for f in facts if f.source == "guideline:processing_time_after_submission")
    assert "usually less than a week" in t


def test_keyword_match_is_case_insensitive_substring(denied, store):
    facts = guidance_facts("next_steps", "HOW SOON DO I NEED TO SUBMIT them", denied, store)
    assert "guideline:submission_timing" in sources(facts)


def test_intent_gate(denied, store):
    # submission_method lists only document_submission in intent_hints.
    facts = guidance_facts("status_inquiry", "how do I submit?", denied, store)
    assert "guideline:submission_method" not in sources(facts)


def test_alternatives_entry_is_keyword_gated(denied, store):
    assert "don't have" in ALTERNATIVE_KEYWORDS
    plain = guidance_facts("document_submission", "how do I submit these?", denied, store)
    assert "guideline:missing_required_material_alternatives" not in sources(plain)
    asking = guidance_facts("next_steps", "I don't have the lab result letter anymore", denied, store)
    assert "guideline:missing_required_material_alternatives" in sources(asking)


def test_document_alternatives_target_the_mentioned_document(denied, store):
    facts = guidance_facts("next_steps", "I don't have the lab result letter anymore", denied, store)
    s = sources(facts)
    assert "document_alternative:lab result letter" in s
    assert "document_alternative:visit summary" not in s


def test_document_alternatives_cover_all_when_none_mentioned(denied, store):
    facts = guidance_facts("next_steps", "what if I can't get the documents?", denied, store)
    s = sources(facts)
    assert "document_alternative:lab result letter" in s
    assert "document_alternative:visit summary" in s


def test_document_guidance_matches_by_substring(denied, store):
    # Fixture key is "original lab result letter"; claim says "lab result letter".
    facts = guidance_facts("document_submission", "what format?", denied, store)
    s = sources(facts)
    assert "document:lab result letter" in s
    assert "document:visit summary" in s
    assert "guidance:default" in s
    assert "guidance:healthcare" in s
    t = next(f.text for f in facts if f.source == "document:lab result letter")
    assert "sample or panel identifier" in t


def test_document_format_guidance_only_for_document_intents(denied, store):
    for intent in ("denial_question", "status_inquiry", "general_claim_question"):
        s = sources(guidance_facts(intent, "why was it denied?", denied, store))
        assert not any(x.startswith("document:") for x in s), intent
        assert "guidance:default" not in s, intent
    s = sources(guidance_facts("next_steps", "what now?", denied, store))
    assert "document:lab result letter" in s and "guidance:default" in s


def test_money_facts_only_when_asked_or_status(denied, store):
    from app.retrieval import wants_money
    assert wants_money("status_inquiry", "how's it going?") is True
    assert wants_money("denial_question", "why was it denied?") is False
    assert wants_money("general_claim_question", "how much will I get paid?") is True
    assert wants_money("next_steps", "what do I owe?") is True
    no_money = select("denial_question", "why was it denied?", denied, store)
    assert not any("USD" in f.text for f in no_money)
    assert not any(f.source.startswith("schema:") for f in no_money)
    with_money = select("status_inquiry", "what's the status?", denied, store)
    assert any("Net pay: 0.00 USD" in f.text for f in with_money)
    assert any(f.source == "schema:net_pay" for f in with_money)


def test_irene_document_has_no_specific_guidance(irene, store):
    facts = guidance_facts("document_submission", "what format?", irene, store)
    s = sources(facts)
    assert not any(src.startswith("document:") for src in s)
    assert "guidance:default" in s
    assert "guidance:healthcare" in s


def test_fallback_when_nothing_matches_a_followup(irene, store):
    assert FOLLOWUP_INTENTS == ("document_submission", "next_steps", "general_claim_question")
    facts = guidance_facts("next_steps", "so what happens now?", irene, store)
    assert "guideline:fallback" in sources(facts)


def test_no_fallback_for_record_answerable_intents(denied, store):
    facts = guidance_facts("status_inquiry", "what's going on with it?", denied, store)
    assert "guideline:fallback" not in sources(facts)


def test_closed_claim_gets_no_document_guidance_and_no_fallback(closed, store):
    facts = guidance_facts("document_submission", "how do I submit?", closed, store)
    assert facts == []


def test_none_intent_is_treated_as_general(denied, store):
    facts = guidance_facts(None, "I lost the visit summary", denied, store)
    assert "guideline:missing_required_material_alternatives" in sources(facts)


# select and render

def test_select_is_claim_then_guidance(denied, store):
    facts = select("document_submission", "how do I submit?", denied, store)
    assert facts[0].source == "claim"
    assert any(f.source.startswith("guideline:") for f in facts)


def test_render_one_line_per_fact():
    out = render([Fact("claim", "Status: denied"), Fact("guideline:x", "Do this.")])
    assert out == "[claim] Status: denied\n[guideline:x] Do this."


def test_fact_is_frozen():
    f = Fact("claim", "x")
    with pytest.raises(AttributeError):
        f.text = "y"
