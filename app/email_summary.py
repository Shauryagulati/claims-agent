"""The email summary is templated from facts; the LLM only smooths the two
prose sections (NOTES D7).

The "Claim status and outcome" block, including every monetary sentence, is
written by the template and never shown to the smoothing model. check_body
then enforces two things on the final email: every required value is
present, and the prose sections contain no invented number or date and no
amount at all (an amount in prose can only be invented or misattributed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.retrieval import Fact, claim_facts, fmt_date
from app.store import Claim, Store

_DATE_WORDS = re.compile(r"\b[A-Z][a-z]+ \d{1,2}, \d{4}\b")
_DATE_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")
_AMOUNT = re.compile(r"(?<![\d.])\d+\.\d{2}(?![\d.])")


@dataclass(frozen=True)
class Draft:
    subject: str
    greeting: str
    intro: str
    discussed: tuple[str, ...]
    status_block: str
    next_steps: tuple[str, ...]
    closing: str
    claim: Claim


def required_values(claim: Claim) -> list[str]:
    values = [claim.case_id, claim.status, *claim.documents_needed]
    if claim.appeal_deadline:
        values.append(fmt_date(claim.appeal_deadline))
    return values


def _status_block(claim: Claim, store: Store) -> str:
    record_lines = [
        f.text for f in claim_facts(claim, store)
        if f.source == "claim" and not f.text.split(":")[0].endswith(("amount", "pay", "fee"))
    ]
    money_lines = [
        f"Amount paid on this claim (net pay): {claim.net_pay} USD.",
        f"Allowed maximum for the covered service: {claim.allowed_max_amount} USD.",
        f"Expected reimbursement: {claim.expected_reimbursement_amount} USD.",
    ]
    return "\n".join(
        ["Claim status and outcome", *(f"- {line}" for line in record_lines), *(f"- {line}" for line in money_lines)]
    )


def build_draft(
    *,
    recipient_name: str,
    claim: Claim,
    discussed: list[str],
    next_steps: list[str],
    store: Store | None = None,
) -> Draft:
    store = store or Store()
    return Draft(
        subject=f"Summary of your claim {claim.case_id}",
        greeting=f"Hello {recipient_name},",
        intro=f"Here is a summary of today's conversation about claim {claim.case_id}.",
        discussed=tuple(discussed) or ("Your claim and its current status",),
        status_block=_status_block(claim, store),
        next_steps=tuple(next_steps) or ("No further action is needed from you right now.",),
        closing="If anything here looks wrong, reply to this email or call us and we will put it right.",
        claim=claim,
    )


def _prose_sections(draft: Draft) -> tuple[str, str]:
    discussed = "\n".join(["What we discussed", *(f"- {d}" for d in draft.discussed)])
    steps = "\n".join(["Next steps", *(f"- {s}" for s in draft.next_steps)])
    return discussed, steps


def smoothable_text(draft: Draft) -> str:
    """Exactly what the smoothing model is given: the two prose sections."""
    discussed, steps = _prose_sections(draft)
    return f"{discussed}\n\n{steps}"


def _split_smoothed(smoothed: str) -> tuple[str, str]:
    """Split the model's output back into the two sections by their headings.
    If the headings are missing, treat the whole text as 'discussed'."""
    marker = "\nNext steps"
    idx = smoothed.find(marker)
    if idx == -1:
        return smoothed.strip(), "Next steps"
    return smoothed[:idx].strip(), smoothed[idx + 1:].strip()


def assemble(draft: Draft, smoothed: str | None) -> str:
    if smoothed is None:
        discussed, steps = _prose_sections(draft)
    else:
        discussed, steps = _split_smoothed(smoothed)
    return "\n\n".join(
        [draft.greeting, draft.intro, discussed, draft.status_block, steps, draft.closing]
    )


def extract_tokens(text: str) -> set[str]:
    tokens: set[str] = set(_DATE_WORDS.findall(text))
    tokens |= set(_DATE_ISO.findall(text))
    stripped = _DATE_WORDS.sub(" ", _DATE_ISO.sub(" ", text))
    tokens |= set(_NUMBER.findall(stripped))
    return tokens


def allowed_text(draft_body: str, facts: list[Fact]) -> str:
    from app.retrieval import render

    return draft_body + "\n" + render(facts)


def is_grounded(smoothed: str, allowed: str) -> bool:
    """One-directional: every number or date in smoothed appears in allowed.
    Pair with prose_amounts, which closes the real-figure-false-claim gap."""
    return extract_tokens(smoothed) <= extract_tokens(allowed)


def prose_amounts(text: str) -> list[str]:
    return _AMOUNT.findall(text)


def check_body(draft: Draft, smoothed: str | None) -> list[str]:
    """Problems with the final email. Empty means safe to send."""
    problems: list[str] = []
    body = assemble(draft, smoothed)
    for value in required_values(draft.claim):
        if value not in body:
            problems.append(f"required value missing from email: {value!r}")
    if smoothed is not None:
        template = assemble(draft, None)
        invented = extract_tokens(smoothed) - extract_tokens(template)
        for token in sorted(invented):
            problems.append(f"prose contains a number or date not in the facts: {token!r}")
        for amount in prose_amounts(smoothed):
            problems.append(f"prose contains an amount; amounts belong only in the status block: {amount!r}")
    return problems


def next_steps_for(claim: Claim, store: Store) -> list[str]:
    """Next steps derived from the record alone, for the email's third section."""
    if claim.status == "denied" and claim.documents_needed:
        docs = ", ".join(claim.documents_needed)
        by = f" before {fmt_date(claim.appeal_deadline)}" if claim.appeal_deadline else ""
        settings = store.guideline.get("claim_followup_settings", {})
        avg = settings.get("average_processing_time_after_submission", {}).get("en", "a short time")
        return [
            f"Send the {docs} for claim {claim.case_id}{by}, ideally through the member portal so they stay attached to the file.",
            f"Once the documents are received the review restarts; it takes {avg}.",
        ]
    if claim.status == "open":
        return ["Your claim is in review. No action is needed from you unless we contact you."]
    return ["This claim is closed. No further action is needed."]
