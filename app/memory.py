"""The two models the turn loop passes around.

Extraction is what the extractor returns for one user message.
CaseMemory is what accumulates across the whole session. Both are plain data.
merge and invalidate return new objects; nothing here mutates in place.
"""

from __future__ import annotations

from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CallerRole = Literal["policyholder", "representative", "unknown"]
CaseType = Literal["healthcare", "dental", "auto"]
ClaimStatus = Literal["denied", "open", "closed"]
Intent = Literal[
    "status_inquiry",
    "denial_question",
    "document_submission",
    "next_steps",
    "general_claim_question",
]
Emotion = Literal["neutral", "frustrated", "angry", "anxious", "confused", "refusing"]
EmailDecision = Literal["yes", "no"]

IDENTITY_FIELDS: tuple[str, ...] = ("claimed_name", "dob", "phone", "email", "id_last4")
INTENTS: tuple[str, ...] = (
    "status_inquiry",
    "denial_question",
    "document_submission",
    "next_steps",
    "general_claim_question",
)

# Fields the verification gate may clear after a mismatch. policyholder_name
# is included because it is the name PII field on the representative path.
INVALIDATABLE_FIELDS: tuple[str, ...] = (*IDENTITY_FIELDS, "policyholder_name")

# Fields copied from Extraction into CaseMemory when non-null.
_MERGE_FIELDS: tuple[str, ...] = (
    *IDENTITY_FIELDS,
    "policy_number",
    "rep_name",
    "rep_relationship",
    "policyholder_name",
    "case_id",
    "case_type",
    "status",
    "month",
    "year",
    "intent",
)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # identity
    claimed_name: str | None = None
    dob: str | None = None
    phone: str | None = None
    email: str | None = None
    id_last4: str | None = None
    policy_number: str | None = None

    # caller
    caller_role: CallerRole = "unknown"
    rep_name: str | None = None
    rep_relationship: str | None = None
    policyholder_name: str | None = None

    # case hints
    case_id: str | None = None
    case_type: CaseType | None = None
    status: ClaimStatus | None = None
    month: int | None = None
    year: int | None = None
    case_hint_text: str | None = None

    # intent and question
    intent: Intent | None = None
    question_text: str | None = None

    # per-turn signals, never copied into memory
    in_scope: bool = True
    emotion: Emotion = "neutral"
    refuses_verification: bool = False
    wants_human: bool = False
    wants_to_wrap_up: bool = False
    email_decision: EmailDecision | None = None
    switching_claim: bool = False

    @field_validator("month")
    @classmethod
    def _month_in_range(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 12:
            raise ValueError("month must be 1..12")
        return v

    def has_signal(self) -> bool:
        """True when the turn carried anything worth remembering or acting on.
        The out-of-scope ladder only fires when this is False (NOTES D5)."""
        if any(getattr(self, f) is not None for f in _MERGE_FIELDS):
            return True
        if self.caller_role != "unknown" or self.case_hint_text:
            return True
        return bool(
            self.wants_human
            or self.wants_to_wrap_up
            or self.email_decision is not None
            or self.switching_claim
        )


class CaseMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # identity
    claimed_name: str | None = None
    dob: str | None = None
    phone: str | None = None
    email: str | None = None
    id_last4: str | None = None
    policy_number: str | None = None

    # caller
    caller_role: CallerRole = "unknown"
    rep_name: str | None = None
    rep_relationship: str | None = None
    policyholder_name: str | None = None

    # case hints
    case_id: str | None = None
    case_type: CaseType | None = None
    status: ClaimStatus | None = None
    month: int | None = None
    year: int | None = None
    free_text: list[str] = Field(default_factory=list)

    intent: Intent | None = None
    verified_party_id: str | None = None
    resolved_case_id: str | None = None
    invalidated_fields: set[str] = Field(default_factory=set)
    # provenance: turn a field was first seen. last_changed: turn its stored
    # value last changed. They differ after a correction.
    provenance: dict[str, int] = Field(default_factory=dict)
    last_changed: dict[str, int] = Field(default_factory=dict)

    def _set(self, f: str, v: object, turn: int) -> None:
        if getattr(self, f) != v:
            self.last_changed[f] = turn
        setattr(self, f, v)
        self.provenance.setdefault(f, turn)

    def merge(self, ex: Extraction, turn: int) -> "CaseMemory":
        """Add every non-null fact from one turn. Never clears a field."""
        m = self.model_copy(deep=True)
        for f in _MERGE_FIELDS:
            v = getattr(ex, f)
            if v is None:
                continue
            m._set(f, v, turn)
            m.invalidated_fields.discard(f)
        if ex.caller_role != "unknown":
            m._set("caller_role", ex.caller_role, turn)
        if ex.case_hint_text and ex.case_hint_text not in m.free_text:
            m.free_text.append(ex.case_hint_text)
        return m

    def invalidate(self, fields: Iterable[str]) -> "CaseMemory":
        """Discard identity values that failed to match the record (NOTES D3).
        The only path that sets a memory field back to None."""
        m = self.model_copy(deep=True)
        for f in fields:
            if f not in INVALIDATABLE_FIELDS:
                raise ValueError(f"only identity fields can be invalidated, not {f!r}")
            setattr(m, f, None)
            m.invalidated_fields.add(f)
        return m

    def supplied_identity(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in IDENTITY_FIELDS if getattr(self, f) is not None}

    def missing_identity(self) -> list[str]:
        return [f for f in IDENTITY_FIELDS if getattr(self, f) is None]
