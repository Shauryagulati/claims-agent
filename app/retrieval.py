"""Deterministic retrieval over required_document_guideline.json and the
claim record. Produces Fact rows; the responder may only phrase these.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app import config
from app.store import Claim, Store

FOLLOWUP_INTENTS: tuple[str, ...] = ("document_submission", "next_steps", "general_claim_question")

# The missing_required_material_alternatives entry has no match_any and lists
# every intent, so it is gated here (NOTES D10).
ALTERNATIVE_KEYWORDS: tuple[str, ...] = (
    "don't have", "do not have", "dont have",
    "can't get", "cannot get", "cant get",
    "lost", "alternative", "instead", "substitute", "unavailable",
    "missing", "no longer",
)

DOCUMENT_GUIDANCE_INTENTS: tuple[str, ...] = ("document_submission", "next_steps")
MONEY_KEYWORDS: tuple[str, ...] = (
    "pay", "paid", "amount", "money", "reimburs", "cost", "owe", "$", "cover", "bill", "dollar",
)

_MONEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("expected_reimbursement_amount", "Expected reimbursement amount"),
    ("allowed_max_amount", "Allowed max amount"),
    ("net_pay", "Net pay"),
    ("net_fee", "Net fee"),
)


@dataclass(frozen=True)
class Fact:
    source: str
    text: str


def fmt_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _deadline_line(deadline: date) -> str:
    days = (deadline - config.DEMO_NOW).days
    if days < 0:
        return f"Appeal deadline: {fmt_date(deadline)} (passed)"
    return f"Appeal deadline: {fmt_date(deadline)} ({days} days from today)"


def claim_facts(claim: Claim, store: Store, include_money: bool = True) -> list[Fact]:
    lines = [
        f"Claim ID: {claim.case_id}",
        f"Type: {claim.case_type}",
        f"Status: {claim.status}",
        f"Filed: {fmt_date(claim.created_at)}",
        f"Summary: {claim.summary}",
    ]
    if claim.denial_reason:
        lines.append(f"Denial reason: {claim.denial_reason}")
    if claim.documents_needed:
        lines.append(f"Documents needed: {', '.join(claim.documents_needed)}")
    if claim.appeal_deadline:
        lines.append(_deadline_line(claim.appeal_deadline))
    if include_money:
        for field, label in _MONEY_FIELDS:
            lines.append(f"{label}: {getattr(claim, field)} USD")

    facts = [Fact("claim", line) for line in lines]
    if include_money:
        for field, _label in _MONEY_FIELDS:
            desc = store.field_description(field)
            if desc:
                facts.append(Fact(f"schema:{field}", desc))
    return facts


def wants_money(intent: str | None, question: str | None) -> bool:
    """Money facts are included only when asked about, or on a status check.
    Given unasked, the responder recites them and infers liability."""
    q = (question or "").lower()
    return intent == "status_inquiry" or any(k in q for k in MONEY_KEYWORDS)


def _fill(template: str, claim: Claim, store: Store) -> str:
    settings = store.guideline.get("claim_followup_settings", {})
    avg = settings.get("average_processing_time_after_submission", {}).get("en", "")
    return (
        template.replace("{case_id}", claim.case_id)
        .replace("{documents}", ", ".join(claim.documents_needed))
        .replace("{average_processing_time_after_submission}", avg)
    )


def _key_matches_document(key: str, doc: str) -> bool:
    k, d = key.lower(), doc.lower()
    return k in d or d in k


def guidance_facts(
    intent: str | None, question: str | None, claim: Claim, store: Store
) -> list[Fact]:
    intent = intent or "general_claim_question"
    q = (question or "").lower()
    g = store.guideline
    docs = claim.documents_needed
    wants_alternatives = any(k in q for k in ALTERNATIVE_KEYWORDS)
    facts: list[Fact] = []

    matched_any = False
    for entry in g.get("claim_followup_guidance", []):
        if intent not in entry.get("intent_hints", []):
            continue
        if entry.get("requires_documents") and not docs:
            continue
        keywords = entry.get("match_any")
        if keywords:
            if not any(k in q for k in keywords):
                continue
        elif entry["topic"] == "missing_required_material_alternatives" and not wants_alternatives:
            continue
        facts.append(Fact(f"guideline:{entry['topic']}", _fill(entry["en"], claim, store)))
        matched_any = True

    if docs:
        mentioned = [d for d in docs if d.lower() in q]
        # Format requirements only when the caller is asking about submitting
        # or what to do next; on a denial or status question they bury the answer.
        if intent in DOCUMENT_GUIDANCE_INTENTS:
            for doc in docs:
                for key, entry in g.get("document_guidance", {}).items():
                    if _key_matches_document(key, doc):
                        facts.append(Fact(f"document:{doc}", entry["en"]))
        if wants_alternatives:
            for doc in mentioned or list(docs):
                for key, entry in g.get("document_alternative_guidance", {}).items():
                    if key != "default" and _key_matches_document(key, doc):
                        facts.append(Fact(f"document_alternative:{doc}", entry["en"]))
        if intent in DOCUMENT_GUIDANCE_INTENTS:
            facts.append(Fact("guidance:default", g["default_guidance"]["en"]))
            case_guidance = g.get("case_type_guidance", {}).get(claim.case_type)
            if case_guidance:
                facts.append(Fact(f"guidance:{claim.case_type}", case_guidance["en"]))

    if docs and not matched_any and intent in FOLLOWUP_INTENTS:
        facts.append(Fact("guideline:fallback", g["claim_followup_fallback"]["en"]))

    return facts


def select(intent: str | None, question: str | None, claim: Claim, store: Store) -> list[Fact]:
    money = wants_money(intent, question)
    return claim_facts(claim, store, include_money=money) + guidance_facts(intent, question, claim, store)


def render(facts: list[Fact]) -> str:
    return "\n".join(f"[{f.source}] {f.text}" for f in facts)
