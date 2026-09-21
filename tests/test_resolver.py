import pytest

from app.memory import CaseMemory, Extraction
from app.resolver import Resolution, resolve, year_for_month


def mem(**kwargs) -> CaseMemory:
    return CaseMemory().merge(Extraction(**kwargs), turn=1)


@pytest.fixture
def nadia(store):
    return store.claims_for_party("PH-4021")


def ids(res: Resolution) -> list[str]:
    return sorted(c.case_id for c in res.candidates)


def test_year_for_month_is_most_recent_on_or_before_demo_now():
    # DEMO_NOW is 2026-09-18
    assert year_for_month(1) == 2026
    assert year_for_month(3) == 2026
    assert year_for_month(10) == 2025
    assert year_for_month(11) == 2025


def test_opening_hint_resolves_to_denied_claim(nadia):
    r = resolve(mem(case_type="healthcare", status="denied", month=7), nadia)
    assert r.status == "resolved"
    assert r.claim.case_id == "CLM-7710"


def test_month_alone_picks_this_year(nadia):
    r = resolve(mem(month=7), nadia)
    assert r.status == "resolved"
    assert r.claim.case_id == "CLM-7710"


def test_month_and_year_picks_last_year(nadia):
    r = resolve(mem(month=7, year=2025), nadia)
    assert r.status == "resolved"
    assert r.claim.case_id == "CLM-7412"


def test_month_wrapping_to_previous_year(nadia):
    r = resolve(mem(month=11), nadia)
    assert r.status == "resolved"
    assert r.claim.case_id == "CLM-7203"


def test_type_alone_is_ambiguous(nadia):
    r = resolve(mem(case_type="healthcare"), nadia)
    assert r.status == "ambiguous"
    assert r.claim is None
    assert ids(r) == ["CLM-7412", "CLM-7710"]


def test_dental_resolves(nadia):
    r = resolve(mem(case_type="dental"), nadia)
    assert r.status == "resolved"
    assert r.claim.case_id == "CLM-7203"


def test_status_open_resolves(nadia):
    r = resolve(mem(status="open"), nadia)
    assert r.claim.case_id == "CLM-7802"


def test_case_id_exact_wins_over_other_hints(nadia):
    r = resolve(mem(case_id="CLM-7802", case_type="dental"), nadia)
    assert r.status == "resolved"
    assert r.claim.case_id == "CLM-7802"


def test_case_id_is_normalised(nadia):
    assert resolve(mem(case_id="clm 7802"), nadia).claim.case_id == "CLM-7802"


def test_unknown_case_id_is_none_with_all_candidates(nadia):
    r = resolve(mem(case_id="CL-0000"), nadia)
    assert r.status == "none"
    assert r.claim is None
    assert len(r.candidates) == 4


def test_no_survivors_is_none_with_all_candidates(nadia):
    r = resolve(mem(month=6), nadia)
    assert r.status == "none"
    assert len(r.candidates) == 4


def test_no_hints_lists_everything(nadia):
    r = resolve(CaseMemory(), nadia)
    assert r.status == "no_hints"
    assert r.claim is None
    assert len(r.candidates) == 4


def test_intent_alone_is_not_a_hint(nadia):
    assert resolve(mem(intent="status_inquiry"), nadia).status == "no_hints"


def test_single_claim_party_resolves_from_status(store):
    r = resolve(mem(status="denied"), store.claims_for_party("PH-4033"))
    assert r.status == "resolved"
    assert r.claim.case_id == "CLM-7915"


def test_wrong_party_claims_cannot_leak(store):
    # Hints that describe Nadia's claim, applied to Irene Bauer's claims.
    r = resolve(mem(case_id="CLM-7710"), store.claims_for_party("PH-4033"))
    assert r.status == "none"


def test_resolution_is_frozen(nadia):
    r = resolve(mem(month=7), nadia)
    with pytest.raises(AttributeError):
        r.status = "none"
