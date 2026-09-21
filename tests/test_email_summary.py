import pytest

from app import config
from app.email_summary import (
    Draft,
    assemble,
    build_draft,
    check_body,
    extract_tokens,
    is_grounded,
    prose_amounts,
    required_values,
    smoothable_text,
)
from app.retrieval import claim_facts, fmt_date


@pytest.fixture
def denied(store):
    return store.claim_by_id("CLM-7710")


@pytest.fixture
def closed(store):
    return store.claim_by_id("CLM-7412")


@pytest.fixture
def draft(denied, store):
    return build_draft(
        recipient_name="Nadia Okonkwo",
        claim=denied,
        discussed=["Why claim CLM-7710 was denied", "How to submit the missing documents"],
        next_steps=["Upload the lab result letter and visit summary via the member portal"],
        store=store,
    )


# required values are derived from the fixture, never literals

def test_required_values_derive_from_claim(denied):
    assert required_values(denied) == [
        denied.case_id,
        denied.status,
        *denied.documents_needed,
        fmt_date(denied.appeal_deadline),
    ]


def test_required_values_for_closed_claim(closed):
    assert required_values(closed) == [closed.case_id, closed.status]


# the draft

def test_draft_sections_and_required_values(draft, denied):
    assert isinstance(draft, Draft)
    assert draft.subject == f"Summary of your claim {denied.case_id}"
    body = assemble(draft, None)
    assert body.startswith("Hello Nadia Okonkwo,")
    assert body.index("What we discussed") < body.index("Claim status and outcome") < body.index("Next steps")
    assert "- Why claim CLM-7710 was denied" in body
    assert "- Upload the lab result letter" in body
    for value in required_values(denied):
        assert value in body


def test_status_block_is_the_template_and_writes_the_money_itself(draft, denied):
    days = (denied.appeal_deadline - config.DEMO_NOW).days
    block = draft.status_block
    assert f"Status: {denied.status}" in block
    assert f"Documents needed: {', '.join(denied.documents_needed)}" in block
    assert f"Appeal deadline: {fmt_date(denied.appeal_deadline)} ({days} days from today)" in block
    # Money is stated by the template with its meaning attached, never by the LLM.
    assert f"Amount paid on this claim (net pay): {denied.net_pay} USD" in block
    assert f"Allowed maximum for the covered service: {denied.allowed_max_amount} USD" in block
    assert f"Expected reimbursement: {denied.expected_reimbursement_amount} USD" in block


def test_closed_draft_omits_denial_lines_and_has_default_next_step(closed, store):
    d = build_draft(recipient_name="Nadia Okonkwo", claim=closed, discussed=["Status of CLM-7412"], next_steps=[], store=store)
    body = assemble(d, None)
    assert "Status: closed" in body
    assert "Denial reason" not in body
    assert "Appeal deadline" not in body
    assert f"Amount paid on this claim (net pay): {closed.net_pay} USD" in body
    assert "No further action is needed" in body


# what the LLM sees and how it is put back

def test_smoothable_text_excludes_the_status_block(draft):
    text = smoothable_text(draft)
    assert "What we discussed" in text
    assert "Next steps" in text
    assert "Claim status and outcome" not in text
    assert "USD" not in text


def test_assemble_keeps_status_block_verbatim_around_smoothed_prose(draft):
    smoothed = (
        "What we discussed\nWe went over why your claim was denied and how to send the missing documents.\n\n"
        "Next steps\nPlease upload the lab result letter and visit summary through the member portal."
    )
    body = assemble(draft, smoothed)
    assert draft.status_block in body
    assert "We went over why your claim was denied" in body
    assert body.index("What we discussed") < body.index(draft.status_block) < body.index("Next steps")
    assert "- Why claim CLM-7710 was denied" not in body  # template bullets replaced


# grounding

def test_extract_tokens_numbers_and_dates():
    text = "Claim CLM-7710 was filed on July 14, 2026 and 1680.00 USD is allowed; deadline 2026-10-01, 13 days."
    tokens = extract_tokens(text)
    assert {"7710", "July 14, 2026", "1680.00", "2026-10-01", "13"} <= tokens
    assert "20" not in tokens


def test_is_grounded_accepts_reworded_facts(draft, denied, store):
    allowed = assemble(draft, None)
    days = (denied.appeal_deadline - config.DEMO_NOW).days
    smoothed = (
        f"Your claim {denied.case_id} was denied because two documents were missing. "
        f"You have until {fmt_date(denied.appeal_deadline)}, which is {days} days from today."
    )
    assert is_grounded(smoothed, allowed) is True


def test_is_grounded_rejects_invented_date_and_number(draft):
    allowed = assemble(draft, None)
    assert is_grounded("Please submit by October 20, 2026.", allowed) is False
    assert is_grounded("Please submit by 2026-10-25.", allowed) is False
    assert is_grounded("You will receive 1500.00 USD.", allowed) is False


def test_is_grounded_with_no_tokens_is_true():
    assert is_grounded("Thank you for calling today.", "anything") is True


# the one-directional gap: a real figure on a false claim

def test_prose_amounts_catches_real_figure_on_false_claim(draft):
    # 1680.00 is in the facts as the allowed maximum; net pay is 0.00. This
    # sentence is grounded token-wise and still wrong. Amounts belong only to
    # the fixed status block, so any amount in prose is rejected outright.
    smoothed = "What we discussed\nYour reimbursement was 1680.00 USD.\n\nNext steps\nNothing further."
    assert is_grounded(smoothed, assemble(draft, None)) is True
    assert prose_amounts(smoothed) == ["1680.00"]
    problems = check_body(draft, smoothed)
    assert any("amount" in p for p in problems)


def test_check_body_passes_for_template_and_good_prose(draft):
    assert check_body(draft, None) == []
    good = "What we discussed\nWhy the claim was denied.\n\nNext steps\nUpload both documents to the portal."
    assert check_body(draft, good) == []


def test_check_body_reports_invented_token(draft):
    bad = "What we discussed\nYou have until October 20, 2026.\n\nNext steps\nNone."
    problems = check_body(draft, bad)
    assert any("October 20, 2026" in p for p in problems)


def test_check_body_reports_missing_required_value(draft, denied):
    # Simulate a draft whose status block lost a required value. The prose
    # must not mention it either, or the check would rightly pass.
    broken = Draft(
        subject=draft.subject,
        greeting=draft.greeting,
        intro=draft.intro,
        discussed=("Why the claim was denied",),
        status_block=draft.status_block.replace("visit summary", "office memo"),
        next_steps=("Upload the documents to the portal",),
        closing=draft.closing,
        claim=draft.claim,
    )
    problems = check_body(broken, None)
    assert any("visit summary" in p for p in problems)
    assert check_body(draft, None) == []  # the real draft has it in the status block


from app.email_summary import next_steps_for


def test_next_steps_for_denied_claim_names_documents_and_deadline(denied, store):
    steps = next_steps_for(denied, store)
    assert len(steps) == 2
    assert "lab result letter, visit summary" in steps[0]
    assert fmt_date(denied.appeal_deadline) in steps[0]
    assert "member portal" in steps[0]
    assert "usually less than a week" in steps[1]


def test_next_steps_for_open_and_closed_claims(store):
    open_steps = next_steps_for(store.claim_by_id("CLM-7802"), store)
    assert open_steps == ["Your claim is in review. No action is needed from you unless we contact you."]
    closed_steps = next_steps_for(store.claim_by_id("CLM-7412"), store)
    assert closed_steps == ["This claim is closed. No further action is needed."]
