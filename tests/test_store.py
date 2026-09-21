import shutil
from datetime import date

import pytest

from app import config
from app.store import FIXTURE_FILES, Claim, Policyholder, Representative, Store


def test_loads_all_fixtures(store):
    assert len(store.policyholders) == 4
    assert len(store.claims) == 5
    assert len(store.representatives) == 1
    assert "claim_followup_guidance" in store.guideline
    assert set(store.consent_scenarios) == {"default", "timeout"}
    assert "field_descriptions" in store.claim_schema


def test_types_are_frozen_dataclasses(store):
    assert isinstance(store.policyholders[0], Policyholder)
    assert isinstance(store.claims[0], Claim)
    assert isinstance(store.representatives[0], Representative)
    with pytest.raises(AttributeError):
        store.claims[0].status = "open"


# load-time guard

def test_fixture_file_list_is_the_six_expected():
    assert FIXTURE_FILES == (
        "claim_schema.json",
        "claims.json",
        "consent_scenarios.json",
        "policyholders.json",
        "representatives.json",
        "required_document_guideline.json",
    )


def test_missing_data_dir_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        Store(tmp_path / "nowhere")
    assert "nowhere" in str(exc.value)


def test_missing_fixture_files_are_named(tmp_path):
    for name in ("claims.json", "policyholders.json"):
        shutil.copy(config.DATA_DIR / name, tmp_path / name)
    with pytest.raises(FileNotFoundError) as exc:
        Store(tmp_path)
    msg = str(exc.value)
    for missing in (
        "claim_schema.json",
        "consent_scenarios.json",
        "representatives.json",
        "required_document_guideline.json",
    ):
        assert missing in msg
    assert "claims.json" not in msg
    assert "policyholders.json" not in msg


# policyholders

def test_policy_number_lookup_is_normalised(store):
    for raw in ("POL-3318", "pol 3318", "pol3318"):
        ph = store.policyholder_by_policy_number(raw)
        assert ph is not None and ph.party_id == "PH-4021"
    assert store.policyholder_by_policy_number("POL-0000") is None
    assert store.policyholder_by_policy_number(None) is None


@pytest.mark.parametrize(
    "raw",
    ["Ann Marie Kovac", "ann marie kovac", "Annmarie Kovac", "annmarie kovac", "ANNMARIE KOVAC", "  Annmarie   Kovac "],
)
def test_name_lookup_normalises_both_input_and_aliases(store, raw):
    assert [p.party_id for p in store.policyholders_by_name(raw)] == ["PH-4045"]


def test_name_lookup_basic_and_misses(store):
    assert [p.party_id for p in store.policyholders_by_name("nadia okonkwo")] == ["PH-4021"]
    assert [p.party_id for p in store.policyholders_by_name("  NADIA   OKONKWO ")] == ["PH-4021"]
    assert store.policyholders_by_name("Nobody Here") == []
    assert store.policyholders_by_name(None) == []


@pytest.mark.parametrize(
    "raw",
    ["a.kovac@gmail.com", "A.Kovac@Gmail.com", "a.kovac@example.com", " A.KOVAC@EXAMPLE.COM "],
)
def test_email_lookup_normalises_both_input_and_aliases(store, raw):
    assert [p.party_id for p in store.policyholders_by_email(raw)] == ["PH-4045"]


def test_email_lookup_misses(store):
    assert store.policyholders_by_email("nobody@example.com") == []
    assert store.policyholders_by_email("not an email") == []
    assert store.policyholders_by_email(None) == []


def test_policyholder_matches_methods(store):
    kovac = store.policyholder_by_party("PH-4045")
    assert kovac.matches_name("ANNMARIE KOVAC")
    assert not kovac.matches_name("Nadia Okonkwo")
    assert kovac.matches_email("A.Kovac@Example.com")
    assert not kovac.matches_email("other@example.com")
    assert kovac.matches_phone("(415) 555-0187")
    assert kovac.matches_phone("+1 415 555 0187")
    assert not kovac.matches_phone("(415) 555-0182")
    assert not kovac.matches_phone(None)


def test_alias_tuples_include_primary_first(store):
    kovac = store.policyholder_by_party("PH-4045")
    assert kovac.all_names[0] == "Ann Marie Kovac"
    assert "Annmarie Kovac" in kovac.all_names
    assert kovac.all_emails == ("a.kovac@gmail.com", "a.kovac@example.com")
    nadia = store.policyholder_by_party("PH-4021")
    assert nadia.all_names == ("Nadia Okonkwo",)
    assert nadia.all_phones == ("+14155550182",)


def test_dob_is_parsed_at_load(store):
    assert store.policyholder_by_party("PH-4021").dob == date(1987, 6, 9)


def test_malformed_dob_fails_at_load(tmp_path):
    import json
    for name in FIXTURE_FILES:
        shutil.copy(config.DATA_DIR / name, tmp_path / name)
    rows = json.loads((tmp_path / "policyholders.json").read_text())
    rows[0]["dob"] = "15/03/1985"
    (tmp_path / "policyholders.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        Store(tmp_path)


def test_id_type_varies(store):
    assert store.policyholder_by_party("PH-4021").id_type == "ssn_last4"
    assert store.policyholder_by_party("PH-4033").id_type == "national_id_last4"
    assert store.policyholder_by_party("PH-4033").id_last4 == "8450"


# claims

def test_claims_for_party(store):
    ids = sorted(c.case_id for c in store.claims_for_party("PH-4021"))
    assert ids == ["CLM-7203", "CLM-7412", "CLM-7710", "CLM-7802"]
    assert [c.case_id for c in store.claims_for_party("PH-4033")] == ["CLM-7915"]
    assert store.claims_for_party("PH-4007") == []


def test_claim_dates_are_parsed(store):
    c = store.claim_by_id("CLM-7710")
    assert c.created_at == date(2026, 7, 14)
    assert c.appeal_deadline == date(2026, 10, 1)


def test_denied_claim_has_denial_fields(store):
    c = store.claim_by_id("CLM-7710")
    assert c.status == "denied"
    assert "lab result letter" in c.denial_reason
    assert c.documents_needed == ("lab result letter", "visit summary")


def test_closed_and_open_claims_tolerate_missing_keys(store):
    for case_id in ("CLM-7412", "CLM-7203", "CLM-7802"):
        c = store.claim_by_id(case_id)
        assert c.denial_reason is None
        assert c.documents_needed == ()
        assert c.appeal_deadline is None


def test_amounts_stay_as_strings(store):
    c = store.claim_by_id("CLM-7412")
    assert c.net_pay == "915.00"
    assert c.allowed_max_amount == "950.00"


def test_claim_by_id_unknown(store):
    assert store.claim_by_id("CL-0000") is None
    assert store.claim_by_id(None) is None


# representatives

def test_representative_match(store):
    rep = store.representative_match("julian okonkwo", "Son", "Nadia Okonkwo")
    assert rep is not None
    assert rep.buyer_party_id == "PH-4021"
    assert store.representative_match("Julian Okonkwo", "daughter", "Nadia Okonkwo") is None
    assert store.representative_match("Julian Okonkwo", "son", "Tomas Ribeiro") is None
    assert store.representative_match("Someone Else", "son", "Nadia Okonkwo") is None
    assert store.representative_match(None, "son", "Nadia Okonkwo") is None


# consent and schema

def test_consent_sequences(store):
    assert store.consent_sequence("default") == ["pending", "approved"]
    assert store.consent_sequence("timeout") == ["pending"] * 5
    with pytest.raises(ValueError):
        store.consent_sequence("nope")


def test_field_description(store):
    assert "finalized" in store.field_description("net_pay")
    assert store.field_description("not_a_field") is None


def test_store_is_read_only_view_of_fixtures(store):
    again = Store()
    assert again.claims == store.claims
    assert again.policyholders == store.policyholders
