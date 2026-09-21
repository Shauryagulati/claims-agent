import pytest
from pydantic import ValidationError

from app.memory import (
    IDENTITY_FIELDS,
    INTENTS,
    CaseMemory,
    Extraction,
)


def test_identity_fields_and_intents_are_fixed():
    assert IDENTITY_FIELDS == ("claimed_name", "dob", "phone", "email", "id_last4")
    assert INTENTS == (
        "status_inquiry",
        "denial_question",
        "document_submission",
        "next_steps",
        "general_claim_question",
    )


def test_extraction_defaults_are_neutral():
    e = Extraction()
    assert e.caller_role == "unknown"
    assert e.in_scope is True
    assert e.emotion == "neutral"
    assert e.wants_human is False
    assert e.email_decision is None
    assert e.has_signal() is False


def test_extraction_rejects_unknown_fields_and_bad_enums():
    with pytest.raises(ValidationError):
        Extraction(surprise="x")
    with pytest.raises(ValidationError):
        Extraction(intent="chit_chat")
    with pytest.raises(ValidationError):
        Extraction(month=13)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"claimed_name": "Nadia Okonkwo"},
        {"policy_number": "POL-3318"},
        {"caller_role": "representative"},
        {"rep_name": "Julian Okonkwo"},
        {"case_type": "healthcare"},
        {"month": 1},
        {"case_hint_text": "denied claim from July"},
        {"intent": "denial_question"},
        {"wants_human": True},
        {"wants_to_wrap_up": True},
        {"email_decision": "no"},
        {"switching_claim": True},
    ],
)
def test_has_signal_true_cases(kwargs):
    assert Extraction(**kwargs).has_signal() is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"in_scope": False},
        {"emotion": "angry"},
        {"refuses_verification": True},
        {"question_text": "what is RL?"},
    ],
)
def test_has_signal_false_cases(kwargs):
    # Scope, emotion, refusal, and a bare question are not "useful
    # extraction" for the out-of-scope ladder (NOTES D5).
    assert Extraction(**kwargs).has_signal() is False


# merge

def test_merge_adds_fields_and_records_provenance():
    m = CaseMemory().merge(Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-09"), turn=1)
    assert m.claimed_name == "Nadia Okonkwo"
    assert m.dob == "1987-06-09"
    assert m.provenance == {"claimed_name": 1, "dob": 1}


def test_merge_is_pure():
    original = CaseMemory()
    original.merge(Extraction(claimed_name="Nadia Okonkwo"), turn=1)
    assert original.claimed_name is None


def test_merge_never_clears_on_omission():
    m = CaseMemory().merge(Extraction(claimed_name="Nadia Okonkwo"), turn=1)
    m = m.merge(Extraction(dob="1987-06-09"), turn=2)
    assert m.claimed_name == "Nadia Okonkwo"
    assert m.dob == "1987-06-09"


def test_merge_correction_wins_and_tracks_first_seen_and_last_changed():
    m = CaseMemory().merge(Extraction(dob="1987-06-10"), turn=1)
    assert m.provenance["dob"] == 1
    assert m.last_changed["dob"] == 1
    m = m.merge(Extraction(dob="1987-06-09"), turn=2)
    assert m.dob == "1987-06-09"
    assert m.provenance["dob"] == 1
    assert m.last_changed["dob"] == 2


def test_merge_same_value_again_does_not_touch_last_changed():
    m = CaseMemory().merge(Extraction(dob="1987-06-09"), turn=1)
    m = m.merge(Extraction(dob="1987-06-09"), turn=4)
    assert m.last_changed["dob"] == 1


def test_resupply_after_invalidation_updates_last_changed():
    m = CaseMemory().merge(Extraction(dob="1987-06-10"), turn=1).invalidate(["dob"])
    m = m.merge(Extraction(dob="1987-06-09"), turn=3)
    assert m.provenance["dob"] == 1
    assert m.last_changed["dob"] == 3


def test_merge_caller_role_unknown_does_not_overwrite():
    m = CaseMemory().merge(Extraction(caller_role="representative"), turn=1)
    m = m.merge(Extraction(caller_role="unknown"), turn=2)
    assert m.caller_role == "representative"


def test_merge_case_hints_and_intent():
    m = CaseMemory().merge(
        Extraction(
            case_type="healthcare",
            status="denied",
            month=7,
            case_hint_text="denied healthcare claim from July",
            intent="denial_question",
        ),
        turn=1,
    )
    assert (m.case_type, m.status, m.month) == ("healthcare", "denied", 7)
    assert m.intent == "denial_question"
    assert m.free_text == ["denied healthcare claim from July"]


def test_merge_free_text_deduplicates():
    e = Extraction(case_hint_text="denied claim from July")
    m = CaseMemory().merge(e, turn=1).merge(e, turn=2)
    assert m.free_text == ["denied claim from July"]


def test_merge_ignores_per_turn_signals():
    m = CaseMemory().merge(
        Extraction(wants_human=True, emotion="angry", email_decision="yes", in_scope=False),
        turn=1,
    )
    assert m == CaseMemory()


# invalidate

def test_invalidate_clears_identity_field_and_marks_it():
    m = CaseMemory().merge(
        Extraction(claimed_name="Nadia Okonkwo", dob="1987-06-10", phone="4155550182"),
        turn=1,
    )
    m2 = m.invalidate(["dob"])
    assert m2.dob is None
    assert m2.invalidated_fields == {"dob"}
    assert m2.claimed_name == "Nadia Okonkwo"
    assert m2.phone == "4155550182"
    assert m.dob == "1987-06-10"  # pure


def test_resupply_clears_invalidation():
    m = CaseMemory().merge(Extraction(dob="1987-06-10"), turn=1).invalidate(["dob"])
    m = m.merge(Extraction(dob="1987-06-09"), turn=3)
    assert m.dob == "1987-06-09"
    assert m.invalidated_fields == set()


def test_invalidate_rejects_non_identity_field():
    with pytest.raises(ValueError):
        CaseMemory().invalidate(["policy_number"])


# helpers

def test_supplied_and_missing_identity():
    m = CaseMemory().merge(Extraction(claimed_name="Nadia Okonkwo", id_last4="2907"), turn=1)
    assert m.supplied_identity() == {"claimed_name": "Nadia Okonkwo", "id_last4": "2907"}
    assert m.missing_identity() == ["dob", "phone", "email"]


def test_memory_round_trips_through_json():
    m = CaseMemory().merge(Extraction(claimed_name="Nadia Okonkwo"), turn=1).invalidate(["claimed_name"])
    again = CaseMemory.model_validate_json(m.model_dump_json())
    assert again == m
