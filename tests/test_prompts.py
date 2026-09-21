import pytest

from app.fsm import DIRECTIVE_KINDS, Directive, Phase, TurnPlan
from app.memory import INTENTS
from app.prompts import (
    FIELD_LABELS,
    PHASE_BLOCKS,
    RENDERED_KINDS,
    extractor_system,
    id_label,
    render_directive,
    render_plan,
    responder_system,
)
from app.retrieval import Fact

SAMPLE_ARGS = {
    "EMPATHIZE": {"emotion": "angry"},
    "ASK_FOR_PII": {"fields": ["dob", "phone", "id_last4"], "unparseable": ["dob"], "id_type": "ssn_last4", "located": True},
    "OFFER_ALT_FIELDS": {"fields": ["phone", "email", "id_last4"], "id_type": "national_id_last4"},
    "VERIFIED": {"name": "Nadia Okonkwo"},
    "CONFIRM_RESOLVED_CASE": {"case_id": "CLM-7710", "case_type": "healthcare", "status": "denied", "filed": "July 14, 2026"},
    "ASK_WHICH_CLAIM": {"candidates": [{"case_id": "CLM-7710", "case_type": "healthcare", "status": "denied", "filed": "July 14, 2026"}]},
    "NO_MATCHING_CLAIM": {"candidates": []},
    "ASK_HOW_CAN_I_HELP": {"candidates": []},
    "ANSWER_FROM_FACTS": {"question": "Why was it denied?", "intent": "denial_question"},
    "OFFER_EMAIL_SUMMARY": {"email": "m*******@email.com"},
    "REPEAT_EMAIL_OFFER": {"email": "m*******@email.com"},
    "EMAIL_WILL_BE_SENT": {"email": "m*******@email.com"},
}


@pytest.mark.parametrize("kind", DIRECTIVE_KINDS)
def test_every_directive_kind_renders(kind):
    text = render_directive(Directive(kind, SAMPLE_ARGS.get(kind, {})))
    assert isinstance(text, str) and len(text) > 10


def test_undeclared_kind_cannot_be_constructed():
    with pytest.raises(ValueError):
        Directive("NOT_A_KIND")


def test_renderers_and_kinds_are_the_same_set():
    # A kind added to the FSM without a renderer, or a renderer without a
    # kind, fails here rather than producing an empty directive at runtime.
    assert set(RENDERED_KINDS) == set(DIRECTIVE_KINDS)


def test_mismatch_retry_names_no_field():
    text = render_directive(Directive("PII_MISMATCH_RETRY")).lower()
    for label in FIELD_LABELS.values():
        assert label.lower() not in text
    for word in ("phone", "birth", "email", "last four", "policy"):
        assert word not in text


def test_ask_for_pii_uses_id_type_and_format_hint():
    text = render_directive(Directive("ASK_FOR_PII", SAMPLE_ARGS["ASK_FOR_PII"]))
    assert "Social Security" in text
    assert "month, day, year" in text  # format hint for the unparseable date
    national = render_directive(Directive("ASK_FOR_PII", {**SAMPLE_ARGS["ASK_FOR_PII"], "id_type": "national_id_last4", "fields": ["id_last4"], "unparseable": []}))
    assert "national ID" in national


def test_id_label_covers_unknown():
    assert "Social Security" in id_label("ssn_last4")
    assert "national ID" in id_label("national_id_last4")
    assert "SSN or national ID" in id_label(None)


def test_consent_timeout_is_plain_and_offers_human():
    text = render_directive(Directive("CONSENT_NOT_RECEIVED_OFFER_HUMAN")).lower()
    assert "not received" in text or "was not received" in text
    assert "human" in text or "representative" in text
    for word in ("please", "urge", "encourage", "convince"):
        assert word not in text


def test_verify_id_plan_has_no_facts_sentence():
    plan = TurnPlan(Phase.VERIFY_ID, (Directive("ASK_FOR_PII", SAMPLE_ARGS["ASK_FOR_PII"]), Directive("DEFER_CLAIM_QUESTION")), ())
    text = render_plan(plan)
    assert "No claim facts are available" in text
    assert "Instructions for this reply" in text
    assert "lab result" not in text.lower()


def test_process_case_plan_renders_facts():
    plan = TurnPlan(Phase.PROCESS_CASE, (Directive("ANSWER_FROM_FACTS", SAMPLE_ARGS["ANSWER_FROM_FACTS"]),), (Fact("claim", "Status: denied"),))
    text = render_plan(plan)
    assert "[claim] Status: denied" in text
    assert "only from" in text.lower()


def test_responder_system_per_phase():
    v = responder_system(Phase.VERIFY_ID)
    for label in ("full name", "date of birth", "phone", "email", "last four"):
        assert label in v
    assert "not" in v.lower() and "claim" in v.lower()
    p = responder_system(Phase.PROCESS_CASE)
    assert "only" in p.lower()
    assert set(PHASE_BLOCKS) == set(Phase)


def test_persona_forbids_liability_inference_and_recitation():
    text = responder_system(Phase.PROCESS_CASE).lower()
    assert "never infer who owes" in text
    assert "answer what was asked" in text
    answer = render_directive(Directive("ANSWER_FROM_FACTS", SAMPLE_ARGS["ANSWER_FROM_FACTS"]))
    assert "only this question" in answer
    close = render_directive(Directive("OFFER_ANYTHING_ELSE"))
    assert "not a question every time" in close


def test_persona_pacing_and_name():
    from app import config
    text = responder_system(Phase.VERIFY_ID)
    assert text.startswith(f"You are {config.AGENT_NAME},")
    assert "Two to four sentences" in text
    assert "hand the turn back" in text
    assert "not paragraphs to write" in text
    assert "Do not open two replies in a row the same way" in text


def test_persona_style_rules():
    text = responder_system(Phase.VERIFY_ID)
    assert "No em dashes" in text
    assert "no bold text" in text
    assert "No bullet lists" in text
    assert '"I\'d be happy to"' in text
    assert '"great question"' in text
    assert "a person typing in a chat window" in text
    assert "contact-centre English" in text


def test_persona_never_mentions_internals():
    for phase in Phase:
        text = responder_system(phase).lower()
        assert "directive" not in text
        assert "fsm" not in text
        assert "phase name" not in text


def test_extractor_system_lists_every_enum():
    text = extractor_system(Phase.VERIFY_ID)
    for intent in INTENTS:
        assert intent in text
    for emotion in ("neutral", "frustrated", "angry", "anxious", "confused", "refusing"):
        assert emotion in text
    for role in ("policyholder", "representative", "unknown"):
        assert role in text
    assert "VERIFY_ID" in text
    assert "only what the user" in text.lower()


def test_extractor_distinguishes_transfer_questions_from_requests():
    text = extractor_system(Phase.VERIFY_ID)
    assert "do you have human agents?" in text
    assert "would a human be able to help?" in text
    assert "not a request" in text
