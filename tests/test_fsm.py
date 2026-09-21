import pytest

from app import config
from app.fsm import (
    DIRECTIVE_KINDS,
    TRANSITIONS,
    Effect,
    IllegalTransition,
    Phase,
    SessionState,
    apply_effects,
    mask_email,
    step,
)
from app.memory import CaseMemory, Extraction
from app.tools import ConsentTool


# helpers

def kinds(result) -> list[str]:
    return [d.kind for d in result.plan.directives]


def directive(result, kind):
    return next(d for d in result.plan.directives if d.kind == kind)


def run(state, ex, store, consent=None):
    consent = consent or ConsentTool(store, state.scenario)
    r = step(state, ex, store, consent)
    return apply_effects(r.state, r.effects), r


NADIA_FULL = Extraction(
    caller_role="policyholder",
    claimed_name="Nadia Okonkwo",
    policy_number="POL-3318",
    dob="1987-06-09",
    id_last4="2907",
    case_type="healthcare",
    status="denied",
    month=7,
    case_hint_text="denied healthcare claim from July",
    intent="denial_question",
)


@pytest.fixture
def verified_nadia(store):
    """State after the opening message: verified, resolved, answered."""
    s, _ = run(SessionState(), NADIA_FULL, store)
    return s


# structure

def test_transition_table_is_exactly_the_spec():
    P = Phase
    expected = {
        (P.VERIFY_ID, P.RESOLVE_INTENT),
        (P.RESOLVE_INTENT, P.PROCESS_CASE),
        (P.PROCESS_CASE, P.POST_PROCESS),
        (P.POST_PROCESS, P.CLOSED),
        (P.POST_PROCESS, P.PROCESS_CASE),
        (P.PROCESS_CASE, P.RESOLVE_INTENT),
        (P.CLOSED, P.PROCESS_CASE),
        (P.CLOSED, P.RESOLVE_INTENT),
        (P.RESOLVE_INTENT, P.POST_PROCESS),
    } | {(p, P.ESCALATED) for p in P if p is not P.ESCALATED} \
      | {(P.ESCALATED, p) for p in P if p is not P.ESCALATED}
    assert TRANSITIONS == frozenset(expected)
    # both reopen paths and the escalation return path, spelled out
    assert (P.POST_PROCESS, P.PROCESS_CASE) in TRANSITIONS
    assert (P.CLOSED, P.PROCESS_CASE) in TRANSITIONS
    assert (P.ESCALATED, P.VERIFY_ID) in TRANSITIONS


def test_step_is_pure(store):
    before = SessionState()
    step(before, NADIA_FULL, store, ConsentTool(store, "default"))
    assert before.phase is Phase.VERIFY_ID
    assert before.memory == CaseMemory()
    assert before.turn == 0


def test_mask_email():
    assert mask_email("nadia@email.com") == "n····@email.com"
    assert mask_email("ab@x.io") == "a·@x.io"
    assert "*" not in mask_email("nadia@email.com")  # asterisks get markdown-escaped


# scenario 1: the opening message, in one turn

def test_opening_message_verifies_resolves_and_answers_in_one_turn(store):
    s, r = run(SessionState(), NADIA_FULL, store)
    assert s.phase is Phase.PROCESS_CASE
    assert s.memory.verified_party_id == "PH-4021"
    assert s.memory.resolved_case_id == "CLM-7710"
    k = kinds(r)
    assert k.index("VERIFIED") < k.index("CONFIRM_RESOLVED_CASE") < k.index("ANSWER_FROM_FACTS")
    assert "OFFER_ANYTHING_ELSE" in k
    assert directive(r, "CONFIRM_RESOLVED_CASE").args["case_id"] == "CLM-7710"
    assert any("Denial reason" in f.text for f in r.plan.facts)
    assert r.effects == ()
    assert s.turn == 1


def test_hint_only_message_is_answered_in_the_same_turn(store):
    # The opening message states the claim but asks no explicit question and
    # the extractor may leave intent null. The caller still gets an answer.
    ex = Extraction(claimed_name="Nadia Okonkwo", policy_number="POL-3318", dob="1987-06-09",
                    id_last4="2907", case_type="healthcare", status="denied", month=7,
                    case_hint_text="denied healthcare claim from July")
    s, r = run(SessionState(), ex, store)
    assert s.phase is Phase.PROCESS_CASE
    assert "ANSWER_FROM_FACTS" in kinds(r)
    assert directive(r, "ANSWER_FROM_FACTS").args["intent"] == "denial_question"
    assert any("Denial reason" in f.text for f in r.plan.facts)
    assert not any(f.source.startswith("document:") for f in r.plan.facts)  # not asked about submitting
    assert not any("USD" in f.text for f in r.plan.facts)  # not asked about money


def test_open_claim_without_intent_defaults_to_status(store):
    s, _ = run(SessionState(), Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907"), store)
    s, r = run(s, Extraction(case_type="auto"), store)
    assert s.memory.resolved_case_id == "CLM-7802"
    assert directive(r, "ANSWER_FROM_FACTS").args["intent"] == "status_inquiry"
    assert any("USD" in f.text for f in r.plan.facts)  # status checks include money


def test_wrap_up_offers_email_with_masked_address(verified_nadia, store):
    s, r = run(verified_nadia, Extraction(wants_to_wrap_up=True), store)
    assert s.phase is Phase.POST_PROCESS
    assert s.sub_state == "awaiting_email_decision"
    assert kinds(r) == ["OFFER_EMAIL_SUMMARY"]
    assert directive(r, "OFFER_EMAIL_SUMMARY").args["email"] == "n····@email.com"


def test_question_after_email_offer_returns_to_process_case(verified_nadia, store):
    s, _ = run(verified_nadia, Extraction(wants_to_wrap_up=True), store)
    s, r = run(s, Extraction(intent="document_submission", question_text="how do I submit them?"), store)
    assert s.phase is Phase.PROCESS_CASE
    assert s.sub_state is None
    assert "ANSWER_FROM_FACTS" in kinds(r)
    assert any(f.source == "guideline:submission_method" for f in r.plan.facts)
    s, r = run(s, Extraction(wants_to_wrap_up=True), store)
    assert s.phase is Phase.POST_PROCESS
    assert kinds(r) == ["OFFER_EMAIL_SUMMARY"]


def test_email_yes_emits_effect_and_closes(verified_nadia, store):
    s, _ = run(verified_nadia, Extraction(wants_to_wrap_up=True), store)
    s, r = run(s, Extraction(email_decision="yes"), store)
    assert s.phase is Phase.CLOSED
    assert kinds(r) == ["EMAIL_WILL_BE_SENT"]
    assert len(r.effects) == 1
    eff = r.effects[0]
    assert eff.kind == "email_send"
    assert eff.args["to"] == "nadia@email.com"
    assert eff.args["recipient_name"] == "Nadia Okonkwo"
    assert eff.args["case_id"] == "CLM-7710"
    assert eff.args["discussed"] == ["Why was my claim denied?"]


def test_email_no_closes_without_effect(verified_nadia, store):
    s, _ = run(verified_nadia, Extraction(wants_to_wrap_up=True), store)
    s, r = run(s, Extraction(email_decision="no"), store)
    assert s.phase is Phase.CLOSED
    assert kinds(r) == ["EMAIL_SKIPPED"]
    assert r.effects == ()


def test_non_answer_repeats_email_offer(verified_nadia, store):
    s, _ = run(verified_nadia, Extraction(wants_to_wrap_up=True), store)
    s, r = run(s, Extraction(), store)
    assert s.phase is Phase.POST_PROCESS
    assert kinds(r) == ["REPEAT_EMAIL_OFFER"]


def test_closed_reopens_on_claim_question_and_invites_otherwise(verified_nadia, store):
    s, _ = run(verified_nadia, Extraction(wants_to_wrap_up=True), store)
    s, _ = run(s, Extraction(email_decision="no"), store)
    assert s.phase is Phase.CLOSED
    s, r = run(s, Extraction(), store)
    assert s.phase is Phase.CLOSED
    assert kinds(r) == ["SESSION_CLOSED_INVITE_MORE"]
    s, r = run(s, Extraction(intent="status_inquiry", question_text="what's the status now?"), store)
    assert s.phase is Phase.PROCESS_CASE
    assert "ANSWER_FROM_FACTS" in kinds(r)


def test_closed_plus_off_topic_stays_closed(verified_nadia, store):
    s, _ = run(verified_nadia, Extraction(wants_to_wrap_up=True), store)
    s, _ = run(s, Extraction(email_decision="no"), store)
    s, r = run(s, Extraction(in_scope=False, question_text="what is RL?"), store)
    assert s.phase is Phase.CLOSED
    assert kinds(r) == ["DECLINE_OFF_TOPIC"]


# VERIFY_ID: no claim data, deferral, partial answers

def test_verify_id_has_no_facts_and_defers_the_question(store):
    ex = Extraction(claimed_name="Nadia Okonkwo", case_type="healthcare", status="denied",
                    month=7, intent="denial_question", question_text="why was it denied?")
    s, r = run(SessionState(), ex, store)
    assert s.phase is Phase.VERIFY_ID
    assert r.plan.facts == ()
    assert "ASK_FOR_PII" in kinds(r)
    assert "DEFER_CLAIM_QUESTION" in kinds(r)
    assert s.memory.month == 7 and s.memory.intent == "denial_question"  # remembered


def test_partial_answers_accumulate_then_verify_with_national_id(store):
    s, r = run(SessionState(), Extraction(claimed_name="Irene Bauer", policy_number="POL-7194"), store)
    ask = directive(r, "ASK_FOR_PII")
    assert ask.args["id_type"] == "national_id_last4"
    assert ask.args["fields"] == ["dob", "phone", "email", "id_last4"]
    s, r = run(s, Extraction(question_text="why do you need my date of birth?"), store)
    assert s.phase is Phase.VERIFY_ID
    assert "ASK_FOR_PII" in kinds(r)
    assert s.verify_attempts == 0
    s, r = run(s, Extraction(dob="1961-04-22"), store)
    assert s.phase is Phase.VERIFY_ID
    s, r = run(s, Extraction(id_last4="8450", status="denied", intent="denial_question"), store)
    assert s.phase is Phase.PROCESS_CASE
    assert s.memory.resolved_case_id == "CLM-7915"


def test_unparseable_date_is_asked_again_without_an_attempt(store):
    s, r = run(SessionState(), Extraction(claimed_name="Nadia Okonkwo", dob="15/03/1985", id_last4="2907"), store)
    assert s.phase is Phase.VERIFY_ID
    assert s.verify_attempts == 0
    ask = directive(r, "ASK_FOR_PII")
    assert ask.args["unparseable"] == ["dob"]


# mismatch, reconfirmation, recovery

def test_mismatch_invalidates_and_waits_for_new_pii(store):
    wrong = Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907", phone="650-555-0000")
    s, r = run(SessionState(), wrong, store)
    assert s.phase is Phase.VERIFY_ID
    assert kinds(r) == ["PII_MISMATCH_RETRY"]
    assert directive(r, "PII_MISMATCH_RETRY").args == {}
    assert s.verify_attempts == 1
    assert s.memory.phone is None
    assert s.memory.invalidated_fields == {"phone"}
    assert s.awaiting_reconfirm is True
    # a turn with no identity fields does not re-run the gate or burn an attempt
    s, r = run(s, Extraction(question_text="what do you mean?"), store)
    assert kinds(r) == ["PII_MISMATCH_RETRY"]
    assert s.verify_attempts == 1
    # corrected phone verifies
    s, r = run(s, Extraction(phone="+14155550182"), store)
    assert s.phase is Phase.RESOLVE_INTENT
    assert s.awaiting_reconfirm is False
    assert "VERIFIED" in kinds(r)
    assert "ASK_HOW_CAN_I_HELP" in kinds(r)


def test_after_threshold_offers_human_but_still_verifies(store):
    s = SessionState()
    for _ in range(config.VERIFY_OFFER_HUMAN_AT):
        s, r = run(s, Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="9999"), store)
    assert s.verify_attempts == config.VERIFY_OFFER_HUMAN_AT
    assert "VERIFY_OFFER_HUMAN" in kinds(r)
    assert s.phase is Phase.VERIFY_ID
    s, r = run(s, Extraction(id_last4="2907"), store)
    assert s.phase is Phase.RESOLVE_INTENT
    assert "VERIFIED" in kinds(r)


# emotion, refusal, escalation

def test_angry_refusal_ladder_then_explicit_human(store):
    angry = Extraction(emotion="angry", refuses_verification=True,
                       question_text="just tell me why my claim was denied", intent="denial_question")
    s, r = run(SessionState(), angry, store)
    assert s.phase is Phase.VERIFY_ID
    assert kinds(r)[0] == "EMPATHIZE"
    assert directive(r, "EMPATHIZE").args["emotion"] == "angry"
    assert "EXPLAIN_WHY_VERIFY" in kinds(r)
    assert "OFFER_ALT_FIELDS" in kinds(r)
    assert "DEFER_CLAIM_QUESTION" in kinds(r)
    assert r.plan.facts == ()
    assert s.refusals == 1
    s, r = run(s, Extraction(emotion="angry", refuses_verification=True), store)
    assert "OFFER_HUMAN" in kinds(r)
    assert "OFFER_ALT_FIELDS" not in kinds(r)
    s, r = run(s, Extraction(emotion="angry", refuses_verification=True), store)
    assert s.phase is Phase.VERIFY_ID  # never escalates by itself
    assert "OFFER_HUMAN" in kinds(r)
    s, r = run(s, Extraction(wants_human=True), store)
    assert s.phase is Phase.ESCALATED
    assert s.handoffs == 1
    assert s.escalated_from is Phase.VERIFY_ID
    assert kinds(r) == ["HANDOFF_HUMAN"]
    s, r = run(s, Extraction(), store)  # bare acknowledgement stays handed off
    assert s.phase is Phase.ESCALATED
    assert kinds(r) == ["SESSION_ENDED"]
    s, r = run(s, Extraction(claimed_name="Nadia Okonkwo"), store)  # substantive: resumes
    assert s.phase is Phase.VERIFY_ID
    assert kinds(r)[0] == "RESUME_AFTER_HANDOFF"
    assert "ASK_FOR_PII" in kinds(r)
    assert s.escalated_from is None


def test_refusal_with_new_pii_runs_the_gate(store):
    ex = Extraction(emotion="frustrated", refuses_verification=True,
                    claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907")
    s, r = run(SessionState(), ex, store)
    assert s.phase is Phase.RESOLVE_INTENT
    assert kinds(r)[0] == "EMPATHIZE"
    assert "VERIFIED" in kinds(r)
    assert s.refusals == 0


def test_wants_human_escalates_from_any_phase(verified_nadia, store):
    s, r = run(verified_nadia, Extraction(wants_human=True), store)
    assert s.phase is Phase.ESCALATED
    assert kinds(r) == ["HANDOFF_HUMAN"]


# out-of-scope ladder

def test_out_of_scope_ladder_never_ends_the_session(store):
    s = SessionState()
    noise = Extraction(in_scope=False, question_text="what is RL?")
    s, r = run(s, noise, store)
    assert kinds(r) == ["DECLINE_OFF_TOPIC"]
    s, r = run(s, noise, store)
    assert kinds(r) == ["DECLINE_OFF_TOPIC_OFFER_HUMAN"]
    s, r = run(s, noise, store)
    assert kinds(r) == ["DECLINE_OFF_TOPIC_OFFER_HUMAN"]
    assert s.phase is Phase.VERIFY_ID
    assert s.oos_streak == 3
    s, r = run(s, Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907"), store)
    assert s.oos_streak == 0
    assert s.phase is Phase.RESOLVE_INTENT


def test_pii_plus_aside_is_not_penalised(store):
    ex = Extraction(in_scope=False, claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907")
    s, r = run(SessionState(), ex, store)
    assert s.oos_streak == 0
    assert s.phase is Phase.RESOLVE_INTENT


# resolution

def test_ambiguous_then_narrowed(store):
    s, _ = run(SessionState(), Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907"), store)
    assert s.phase is Phase.RESOLVE_INTENT
    s, r = run(s, Extraction(case_type="healthcare", intent="status_inquiry", question_text="status of my healthcare claim?"), store)
    assert s.phase is Phase.RESOLVE_INTENT
    ask = directive(r, "ASK_WHICH_CLAIM")
    assert sorted(c["case_id"] for c in ask.args["candidates"]) == ["CLM-7412", "CLM-7710"]
    s, r = run(s, Extraction(month=7, case_hint_text="the July one"), store)
    assert s.phase is Phase.PROCESS_CASE
    assert s.memory.resolved_case_id == "CLM-7710"
    assert "ANSWER_FROM_FACTS" in kinds(r)


def test_no_matching_claim_lists_what_exists(store):
    s, _ = run(SessionState(), Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907"), store)
    s, r = run(s, Extraction(case_id="CL-0000"), store)
    assert s.phase is Phase.RESOLVE_INTENT
    assert len(directive(r, "NO_MATCHING_CLAIM").args["candidates"]) == 4


def test_switching_claim_resets_hints_and_resolves(verified_nadia, store):
    s, r = run(verified_nadia, Extraction(switching_claim=True, case_type="dental",
                                              intent="status_inquiry", question_text="and my dental one?"), store)
    assert s.phase is Phase.PROCESS_CASE
    assert s.memory.resolved_case_id == "CLM-7203"
    assert s.memory.status is None  # "denied" from the first claim was cleared
    assert "CONFIRM_RESOLVED_CASE" in kinds(r)


def test_followup_not_covered_for_closed_claim(store):
    s, _ = run(SessionState(), Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907"), store)
    s, r = run(s, Extraction(case_type="dental", intent="document_submission", question_text="how do I upload documents for it?"), store)
    assert s.memory.resolved_case_id == "CLM-7203"
    assert "NOT_COVERED_OFFER_HUMAN" in kinds(r)


def test_process_uses_default_question_when_only_intent_is_known(verified_nadia):
    # The opening message carried an intent and no explicit question.
    assert verified_nadia.discussed == (("CLM-7710", "Why was my claim denied?"),)


def test_email_discussed_covers_final_claim_only(verified_nadia, store):
    s, _ = run(verified_nadia, Extraction(switching_claim=True, case_type="dental",
                                             intent="status_inquiry", question_text="and my dental one?"), store)
    assert s.memory.resolved_case_id == "CLM-7203"
    s, _ = run(s, Extraction(wants_to_wrap_up=True), store)
    s, r = run(s, Extraction(email_decision="yes"), store)
    eff = r.effects[0]
    assert eff.args["case_id"] == "CLM-7203"
    assert eff.args["discussed"] == ["and my dental one?"]


def test_wrap_up_in_resolve_intent_without_claim_closes_without_summary(store):
    s, _ = run(SessionState(), Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907"), store)
    assert s.phase is Phase.RESOLVE_INTENT
    s, r = run(s, Extraction(wants_to_wrap_up=True), store)
    assert s.phase is Phase.CLOSED
    assert kinds(r) == ["CLOSE_WITHOUT_SUMMARY"]
    assert r.effects == ()
    # and a later claim question from CLOSED goes back to resolution, not processing
    s, r = run(s, Extraction(case_type="dental", intent="status_inquiry", question_text="my dental claim?"), store)
    assert s.phase is Phase.PROCESS_CASE
    assert s.memory.resolved_case_id == "CLM-7203"


def test_escalation_preserves_consent_sub_state(store):
    consent = ConsentTool(store, "default")
    s, _ = run(SessionState(), REP, store, consent)
    assert s.sub_state == "awaiting_consent"
    s, r = run(s, Extraction(wants_human=True), store, consent)
    assert s.phase is Phase.ESCALATED
    assert s.sub_state == "awaiting_consent"
    s, r = run(s, Extraction(question_text="actually, any update on the consent?"), store, consent)
    assert kinds(r)[0] == "RESUME_AFTER_HANDOFF"
    assert "CONSENT_APPROVED" in kinds(r)
    assert s.phase is Phase.RESOLVE_INTENT


def test_repeated_human_request_while_escalated_stays_escalated(verified_nadia, store):
    s, _ = run(verified_nadia, Extraction(wants_human=True), store)
    s, r = run(s, Extraction(wants_human=True, question_text="where is the human?"), store)
    assert s.phase is Phase.ESCALATED
    assert kinds(r) == ["SESSION_ENDED"]


# representative path

REP = Extraction(
    caller_role="representative",
    claimed_name="Julian Okonkwo",
    rep_name="Julian Okonkwo",
    rep_relationship="son",
    policyholder_name="Nadia Okonkwo",
    policy_number="POL-3318",
    dob="1987-06-09",
    phone="4155550182",
)


def test_representative_default_scenario(store):
    consent = ConsentTool(store, "default")
    s, r = run(SessionState(scenario="default"), REP, store, consent)
    assert s.phase is Phase.VERIFY_ID
    assert s.sub_state == "awaiting_consent"
    assert kinds(r) == ["VERIFIED", "CONSENT_REQUESTED"]
    assert r.effects == (Effect("consent_advance", {}),)
    assert s.consent_polls == 1
    assert s.consent_status == "pending"
    s, r = run(s, Extraction(question_text="any update?"), store, consent)
    assert s.consent_status == "approved"
    assert s.sub_state is None
    assert s.phase is Phase.RESOLVE_INTENT
    assert kinds(r) == ["CONSENT_APPROVED", "ASK_HOW_CAN_I_HELP"]
    s, r = run(s, Extraction(case_type="healthcare", intent="status_inquiry", question_text="status of her healthcare claim?"), store, consent)
    assert "ASK_WHICH_CLAIM" in kinds(r)
    s, r = run(s, Extraction(month=7), store, consent)
    assert s.phase is Phase.PROCESS_CASE
    assert s.memory.resolved_case_id == "CLM-7710"


def test_representative_timeout_is_plain_and_offers_human(store):
    consent = ConsentTool(store, "timeout")
    s, r = run(SessionState(scenario="timeout"), REP, store, consent)
    assert kinds(r) == ["VERIFIED", "CONSENT_REQUESTED"]
    for _ in range(4):
        s, r = run(s, Extraction(question_text="anything yet?"), store, consent)
        assert kinds(r) == ["CONSENT_STILL_PENDING"]
        assert r.effects == (Effect("consent_advance", {}),)
    assert s.consent_polls == 5
    s, r = run(s, Extraction(question_text="anything yet?"), store, consent)
    assert s.consent_status == "timeout"
    assert kinds(r) == ["CONSENT_NOT_RECEIVED_OFFER_HUMAN"]
    assert r.effects == ()
    assert "EMPATHIZE" not in kinds(r)
    s, r = run(s, Extraction(question_text="can you try again?"), store, consent)
    assert kinds(r) == ["CONSENT_NOT_RECEIVED_OFFER_HUMAN"]
    assert s.phase is Phase.VERIFY_ID
    s, r = run(s, Extraction(wants_human=True), store, consent)
    assert s.phase is Phase.ESCALATED


def test_representative_not_on_file(store):
    bad = Extraction(**{**REP.model_dump(), "rep_name": "Someone Else"})
    s, r = run(SessionState(), bad, store)
    assert s.phase is Phase.VERIFY_ID
    assert s.sub_state is None
    assert kinds(r) == ["VERIFIED", "REP_NOT_FOUND"]


# guards

def test_illegal_transition_raises():
    from app.fsm import assert_transition
    assert_transition(Phase.VERIFY_ID, Phase.RESOLVE_INTENT)
    with pytest.raises(IllegalTransition):
        assert_transition(Phase.VERIFY_ID, Phase.PROCESS_CASE)
    with pytest.raises(IllegalTransition):
        assert_transition(Phase.CLOSED, Phase.VERIFY_ID)  # identity never becomes unverified


def test_every_emitted_kind_is_declared(store):
    seen = set()
    scenarios = [
        [NADIA_FULL, Extraction(wants_to_wrap_up=True), Extraction(email_decision="yes"), Extraction()],
        [Extraction(in_scope=False)],
        [Extraction(emotion="angry", refuses_verification=True), Extraction(wants_human=True), Extraction()],
    ]
    for script in scenarios:
        s = SessionState()
        for ex in script:
            s, r = run(s, ex, store)
            seen |= set(kinds(r))
    assert seen <= set(DIRECTIVE_KINDS)
