"""Pure gates. Each takes memory plus the store and returns data.

Nothing here builds user-facing text. Mismatched field names go back to the
FSM for invalidation only (NOTES D3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app import config
from app.memory import CaseMemory, Extraction
from app.normalize import norm_date, norm_email, norm_last4, norm_name, norm_phone
from app.store import Policyholder, Store

VerificationStatus = Literal["verified", "insufficient", "mismatch", "no_record"]


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    party_id: str | None
    matched: tuple[str, ...]
    mismatched: tuple[str, ...]
    unparseable: tuple[str, ...]
    missing: tuple[str, ...]
    id_type: str | None


def _name_field(memory: CaseMemory) -> str:
    return "policyholder_name" if memory.caller_role == "representative" else "claimed_name"


def _locate(memory: CaseMemory, store: Store) -> Policyholder | None:
    """Policy number first, then the name. A policy number that matches
    nothing falls back to the name: policy_number is not invalidatable, so a
    typo there must not block verification. A name that matches more than
    one record locates nothing; the caller is asked for the policy number
    (NOTES D14)."""
    if memory.policy_number:
        by_policy = store.policyholder_by_policy_number(memory.policy_number)
        if by_policy is not None:
            return by_policy
    candidates = store.policyholders_by_name(getattr(memory, _name_field(memory)))
    return candidates[0] if len(candidates) == 1 else None


def verify_identity(memory: CaseMemory, store: Store) -> VerificationResult:
    name_field = _name_field(memory)
    record = _locate(memory, store)
    if record is None:
        return VerificationResult(
            status="no_record",
            party_id=None,
            matched=(),
            mismatched=(),
            unparseable=(),
            missing=("policy_number", name_field),
            id_type=None,
        )

    matched: list[str] = []
    mismatched: list[str] = []
    unparseable: list[str] = []
    missing: list[str] = []

    def judge(field: str, raw: str | None, parsed: object, ok: bool) -> None:
        if raw is None:
            missing.append(field)
        elif parsed is None:
            unparseable.append(field)
            missing.append(field)
        elif ok:
            matched.append(field)
        else:
            mismatched.append(field)

    name_raw = getattr(memory, name_field)
    judge(name_field, name_raw, norm_name(name_raw), record.matches_name(name_raw))

    dob = norm_date(memory.dob)
    judge("dob", memory.dob, dob, dob == record.dob)

    phone = norm_phone(memory.phone)
    judge("phone", memory.phone, phone, record.matches_phone(memory.phone))

    email = norm_email(memory.email)
    judge("email", memory.email, email, record.matches_email(memory.email))

    last4 = norm_last4(memory.id_last4)
    judge("id_last4", memory.id_last4, last4, last4 == record.id_last4)

    if mismatched:
        status: VerificationStatus = "mismatch"
    elif len(matched) >= config.VERIFY_MIN_MATCHES:
        status = "verified"
    else:
        status = "insufficient"

    return VerificationResult(
        status=status,
        party_id=record.party_id,
        matched=tuple(matched),
        mismatched=tuple(mismatched),
        unparseable=tuple(unparseable),
        missing=tuple(missing),
        id_type=record.id_type,
    )


def verify_representative(memory: CaseMemory, store: Store, party_id: str) -> bool:
    rep = store.representative_match(
        memory.rep_name, memory.rep_relationship, memory.policyholder_name
    )
    return rep is not None and rep.buyer_party_id == party_id


# ladders (NOTES D5, D6, D11): the last rung repeats; nothing escalates here.

def is_pure_noise(ex: Extraction) -> bool:
    return not ex.in_scope and not ex.has_signal()


def out_of_scope_rung(streak: int) -> Literal["decline", "decline_offer_human"]:
    return "decline" if streak < config.OOS_OFFER_HUMAN_AT else "decline_offer_human"


def refusal_rung(count: int) -> Literal["explain_offer_alt_fields", "explain_offer_human"]:
    return "explain_offer_alt_fields" if count < config.REFUSAL_OFFER_HUMAN_AT else "explain_offer_human"


def verify_offer_human(attempts: int) -> bool:
    """After this many failed attempts every VERIFY_ID turn also offers a
    human. Corrections are still evaluated; nothing locks (NOTES D11)."""
    return attempts >= config.VERIFY_OFFER_HUMAN_AT
