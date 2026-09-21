import pytest

from app import config
from app.gates import (
    VerificationResult,
    is_pure_noise,
    out_of_scope_rung,
    refusal_rung,
    verify_offer_human,
    verify_identity,
    verify_representative,
)
from app.memory import CaseMemory, Extraction


def mem(**kwargs) -> CaseMemory:
    return CaseMemory().merge(Extraction(**kwargs), turn=1)


NADIA = dict(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="2907")


# verified

def test_nadia_with_policy_number_verifies(store):
    r = verify_identity(mem(policy_number="POL-3318", **NADIA), store)
    assert r.status == "verified"
    assert r.party_id == "PH-4021"
    assert r.matched == ("claimed_name", "dob", "id_last4")
    assert r.mismatched == ()
    assert r.id_type == "ssn_last4"


def test_nadia_located_by_name_verifies(store):
    r = verify_identity(mem(**NADIA), store)
    assert r.status == "verified"
    assert r.party_id == "PH-4021"


def test_policy_number_does_not_count_toward_three(store):
    r = verify_identity(
        mem(policy_number="POL-3318", claimed_name="Nadia Okonkwo", dob="1987-06-09"), store
    )
    assert r.status == "insufficient"
    assert r.matched == ("claimed_name", "dob")
    assert r.missing == ("phone", "email", "id_last4")


def test_national_id_caller_verifies(store):
    r = verify_identity(mem(claimed_name="Irene Bauer", dob="1961-04-22", id_last4="8450"), store)
    assert r.status == "verified"
    assert r.party_id == "PH-4033"
    assert r.id_type == "national_id_last4"


def test_aliases_match_on_both_sides(store):
    r = verify_identity(
        mem(claimed_name="ANNMARIE KOVAC", dob="02/17/1988", email="A.KOVAC@EXAMPLE.COM"), store
    )
    assert r.status == "verified"
    assert r.party_id == "PH-4045"


def test_phone_in_any_format_matches(store):
    r = verify_identity(
        mem(claimed_name="Nadia Okonkwo", dob="June 9, 1987", phone="(415) 555-0182"), store
    )
    assert r.status == "verified"


# insufficient and missing

def test_two_fields_is_insufficient_and_lists_missing(store):
    r = verify_identity(mem(claimed_name="Nadia Okonkwo", dob="1987-06-09"), store)
    assert r.status == "insufficient"
    assert r.missing == ("phone", "email", "id_last4")
    assert r.id_type == "ssn_last4"


def test_unparseable_date_is_missing_not_mismatched(store):
    # Day-first input. norm_date returns None; this must not count as an
    # attempt and must not invalidate anything (ARCHITECTURE section 6).
    r = verify_identity(
        mem(claimed_name="Nadia Okonkwo", dob="15/03/1985", id_last4="2907"), store
    )
    assert r.status == "insufficient"
    assert r.unparseable == ("dob",)
    assert "dob" in r.missing
    assert r.mismatched == ()
    assert r.matched == ("claimed_name", "id_last4")


def test_unparseable_last4_is_missing(store):
    r = verify_identity(mem(claimed_name="Nadia Okonkwo", dob="1987-06-09", id_last4="29071"), store)
    assert r.status == "insufficient"
    assert r.unparseable == ("id_last4",)


# mismatch

def test_three_right_one_wrong_is_mismatch(store):
    r = verify_identity(mem(phone="6505550000", **NADIA), store)
    assert r.status == "mismatch"
    assert r.mismatched == ("phone",)
    assert r.matched == ("claimed_name", "dob", "id_last4")
    assert r.party_id == "PH-4021"


def test_wrong_name_with_policy_number_is_mismatch(store):
    r = verify_identity(
        mem(policy_number="POL-3318", claimed_name="Nadia Chan", dob="1987-06-09", id_last4="2907"),
        store,
    )
    assert r.status == "mismatch"
    assert r.mismatched == ("claimed_name",)


def test_mismatch_after_invalidation_and_correction_verifies(store):
    m = mem(phone="6505550000", **NADIA)
    first = verify_identity(m, store)
    assert first.status == "mismatch"
    m = m.invalidate(first.mismatched)
    assert verify_identity(m, store).status == "verified"  # three matched, phone gone
    m = m.merge(Extraction(phone="+14155550182"), turn=2)
    r = verify_identity(m, store)
    assert r.status == "verified"
    assert r.matched == ("claimed_name", "dob", "phone", "id_last4")


# no record

def test_unknown_name_is_no_record(store):
    r = verify_identity(mem(claimed_name="Nobody Here", dob="1987-06-09"), store)
    assert r.status == "no_record"
    assert r.party_id is None
    assert r.missing == ("policy_number", "claimed_name")


def test_wrong_policy_number_falls_back_to_name(store):
    # Regression: policy_number is not invalidatable, so a typo there must
    # not block a caller whose name, DOB and last four are all correct.
    r = verify_identity(mem(policy_number="POL-0000", **NADIA), store)
    assert r.status == "verified"
    assert r.party_id == "PH-4021"


def test_wrong_policy_number_and_unknown_name_is_no_record(store):
    r = verify_identity(mem(policy_number="POL-0000", claimed_name="Nobody Here", dob="1987-06-09"), store)
    assert r.status == "no_record"


def test_name_that_normalises_to_nothing_is_missing_not_mismatched(store):
    r = verify_identity(mem(policy_number="POL-3318", claimed_name="...", dob="1987-06-09", id_last4="2907"), store)
    assert r.status == "insufficient"
    assert r.unparseable == ("claimed_name",)
    assert r.mismatched == ()


def test_nothing_supplied_is_no_record(store):
    r = verify_identity(CaseMemory(), store)
    assert r.status == "no_record"
    assert r.missing == ("policy_number", "claimed_name")


def test_policy_number_wins_over_name_as_locator(store):
    # Name points at PH-4007, policy number at PH-4021. The policy number locates.
    r = verify_identity(mem(policy_number="POL-3318", claimed_name="Tomas Ribeiro", dob="1987-06-09"), store)
    assert r.party_id == "PH-4021"
    assert r.status == "mismatch"
    assert r.mismatched == ("claimed_name",)


# representative path

REP = dict(
    caller_role="representative",
    claimed_name="Julian Okonkwo",
    rep_name="Julian Okonkwo",
    rep_relationship="son",
    policyholder_name="Nadia Okonkwo",
)


def test_representative_uses_policyholder_name_as_name_field(store):
    r = verify_identity(mem(dob="1987-06-09", phone="4155550182", **REP), store)
    assert r.status == "verified"
    assert r.party_id == "PH-4021"
    assert r.matched == ("policyholder_name", "dob", "phone")


def test_representative_name_mismatch_names_policyholder_field(store):
    bad = dict(REP, policyholder_name="Nadia Chan")
    r = verify_identity(mem(policy_number="POL-3318", dob="1987-06-09", phone="4155550182", **bad), store)
    assert r.status == "mismatch"
    assert r.mismatched == ("policyholder_name",)
    m = mem(policy_number="POL-3318", dob="1987-06-09", phone="4155550182", **bad).invalidate(r.mismatched)
    assert m.policyholder_name is None


def test_representative_missing_policyholder_name_asks_for_it(store):
    r = verify_identity(mem(caller_role="representative", rep_name="Julian Okonkwo"), store)
    assert r.status == "no_record"
    assert r.missing == ("policy_number", "policyholder_name")


def test_verify_representative(store):
    m = mem(**REP)
    assert verify_representative(m, store, "PH-4021") is True
    assert verify_representative(m, store, "PH-4033") is False
    assert verify_representative(mem(**dict(REP, rep_relationship="daughter")), store, "PH-4021") is False
    assert verify_representative(mem(**dict(REP, rep_name="Someone Else")), store, "PH-4021") is False
    assert verify_representative(CaseMemory(), store, "PH-4021") is False


# ladders

@pytest.mark.parametrize(
    "ex, expected",
    [
        (Extraction(in_scope=False), True),
        (Extraction(in_scope=False, emotion="angry"), True),
        (Extraction(in_scope=False, question_text="what is RL?"), True),
        (Extraction(in_scope=False, claimed_name="Nadia Okonkwo"), False),
        (Extraction(in_scope=False, month=7), False),
        (Extraction(in_scope=False, case_hint_text="my July claim"), False),
        (Extraction(in_scope=False, wants_human=True), False),
        (Extraction(in_scope=True), False),
    ],
)
def test_is_pure_noise(ex, expected):
    assert is_pure_noise(ex) is expected


def test_out_of_scope_rung_never_escalates():
    assert out_of_scope_rung(1) == "decline"
    assert out_of_scope_rung(config.OOS_OFFER_HUMAN_AT) == "decline_offer_human"
    assert out_of_scope_rung(3) == "decline_offer_human"
    assert out_of_scope_rung(50) == "decline_offer_human"


def test_refusal_rung_never_escalates():
    assert refusal_rung(1) == "explain_offer_alt_fields"
    assert refusal_rung(config.REFUSAL_OFFER_HUMAN_AT) == "explain_offer_human"
    assert refusal_rung(9) == "explain_offer_human"


def test_verify_offer_human_threshold():
    assert verify_offer_human(config.VERIFY_OFFER_HUMAN_AT - 1) is False
    assert verify_offer_human(config.VERIFY_OFFER_HUMAN_AT) is True
    assert verify_offer_human(config.VERIFY_OFFER_HUMAN_AT + 1) is True


def test_result_is_frozen(store):
    r = verify_identity(mem(**NADIA), store)
    assert isinstance(r, VerificationResult)
    with pytest.raises(AttributeError):
        r.status = "verified"
