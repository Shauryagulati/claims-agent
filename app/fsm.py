"""The state machine. Owns phase order, gates, and allowed actions.

step() is pure: it takes a SessionState and one Extraction and returns a
proposed next state, a TurnPlan for the responder, and the tool effects the
commit step must run. Nothing here does I/O (NOTES D1, D12).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Literal

from app.gates import (
    is_pure_noise,
    out_of_scope_rung,
    refusal_rung,
    verify_identity,
    verify_offer_human,
    verify_representative,
)
from app.memory import IDENTITY_FIELDS, CaseMemory, Extraction
from app.resolver import resolve
from app.retrieval import FOLLOWUP_INTENTS, Fact, fmt_date, select
from app.store import Claim, Store
from app.tools import ConsentStatus, ConsentTool


class Phase(str, Enum):
    VERIFY_ID = "VERIFY_ID"
    RESOLVE_INTENT = "RESOLVE_INTENT"
    PROCESS_CASE = "PROCESS_CASE"
    POST_PROCESS = "POST_PROCESS"
    CLOSED = "CLOSED"
    ESCALATED = "ESCALATED"


SubState = Literal["awaiting_consent", "awaiting_email_decision"]

TRANSITIONS: frozenset[tuple[Phase, Phase]] = frozenset(
    {
        (Phase.VERIFY_ID, Phase.RESOLVE_INTENT),
        (Phase.RESOLVE_INTENT, Phase.PROCESS_CASE),
        (Phase.PROCESS_CASE, Phase.POST_PROCESS),
        (Phase.POST_PROCESS, Phase.CLOSED),
        (Phase.POST_PROCESS, Phase.PROCESS_CASE),
        (Phase.PROCESS_CASE, Phase.RESOLVE_INTENT),
        (Phase.CLOSED, Phase.PROCESS_CASE),
        (Phase.CLOSED, Phase.RESOLVE_INTENT),
        (Phase.RESOLVE_INTENT, Phase.POST_PROCESS),
    }
    | {(p, Phase.ESCALATED) for p in Phase if p is not Phase.ESCALATED}
    # A handoff here is simulated, so a substantive message after escalation
    # resumes the phase the caller was in (NOTES D11).
    | {(Phase.ESCALATED, p) for p in Phase if p is not Phase.ESCALATED}
)


class IllegalTransition(Exception):
    pass


def assert_transition(a: Phase, b: Phase) -> None:
    if (a, b) not in TRANSITIONS:
        raise IllegalTransition(f"{a.value} -> {b.value}")


DIRECTIVE_KINDS: tuple[str, ...] = (
    "EMPATHIZE",
    "HANDOFF_HUMAN",
    "SESSION_ENDED",
    "DECLINE_OFF_TOPIC",
    "DECLINE_OFF_TOPIC_OFFER_HUMAN",
    "ASK_FOR_PII",
    "PII_MISMATCH_RETRY",
    "VERIFY_OFFER_HUMAN",
    "EXPLAIN_WHY_VERIFY",
    "OFFER_ALT_FIELDS",
    "OFFER_HUMAN",
    "DEFER_CLAIM_QUESTION",
    "VERIFIED",
    "REP_NOT_FOUND",
    "CONSENT_REQUESTED",
    "CONSENT_STILL_PENDING",
    "CONSENT_APPROVED",
    "CONSENT_NOT_RECEIVED_OFFER_HUMAN",
    "ASK_HOW_CAN_I_HELP",
    "CONFIRM_RESOLVED_CASE",
    "ASK_WHICH_CLAIM",
    "NO_MATCHING_CLAIM",
    "ANSWER_FROM_FACTS",
    "NOT_COVERED_OFFER_HUMAN",
    "OFFER_ANYTHING_ELSE",
    "OFFER_EMAIL_SUMMARY",
    "REPEAT_EMAIL_OFFER",
    "EMAIL_WILL_BE_SENT",
    "EMAIL_SKIPPED",
    "SESSION_CLOSED_INVITE_MORE",
    "RESUME_AFTER_HANDOFF",
    "CLOSE_WITHOUT_SUMMARY",
)


@dataclass(frozen=True)
class Directive:
    kind: str
    args: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in DIRECTIVE_KINDS:
            raise ValueError(f"undeclared directive kind: {self.kind!r}")


@dataclass(frozen=True)
class Effect:
    kind: Literal["consent_advance", "email_send"]
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionState:
    scenario: str = "default"
    phase: Phase = Phase.VERIFY_ID
    sub_state: SubState | None = None
    memory: CaseMemory = field(default_factory=CaseMemory)
    turn: int = 0
    verify_attempts: int = 0
    refusals: int = 0
    oos_streak: int = 0
    consent_polls: int = 0
    consent_status: ConsentStatus | None = None
    awaiting_reconfirm: bool = False
    discussed: tuple[tuple[str, str], ...] = ()  # (case_id, question)
    escalated_from: Phase | None = None
    handoffs: int = 0


@dataclass(frozen=True)
class TurnPlan:
    phase: Phase
    directives: tuple[Directive, ...]
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class StepResult:
    state: SessionState
    plan: TurnPlan
    effects: tuple[Effect, ...]


_DEFAULT_QUESTION = {
    "status_inquiry": "What is the status of my claim?",
    "denial_question": "Why was my claim denied?",
    "document_submission": "What documents do I need to submit, and how?",
    "next_steps": "What should I do next?",
    "general_claim_question": "Tell me about my claim.",
    None: "Tell me about my claim.",
}


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    # middle dots, not asterisks: the responder escapes asterisks as markdown
    return f"{local[:1]}{'·' * max(1, len(local) - 1)}@{domain}"


def _claim_summary(c: Claim) -> dict[str, str]:
    return {
        "case_id": c.case_id,
        "case_type": c.case_type,
        "status": c.status,
        "filed": fmt_date(c.created_at),
    }


def _has_question(ex: Extraction) -> bool:
    return bool(ex.question_text or ex.intent or ex.switching_claim)


def _supplied_identity(ex: Extraction) -> bool:
    return any(getattr(ex, f) is not None for f in IDENTITY_FIELDS) or bool(
        ex.policy_number or ex.policyholder_name
    )


def _reset_case_hints(memory: CaseMemory, ex: Extraction, turn: int) -> CaseMemory:
    cleared = memory.model_copy(
        update={"case_id": None, "case_type": None, "status": None, "month": None,
                "year": None, "resolved_case_id": None}
    )
    return cleared.merge(ex, turn)


class _Turn:
    """Mutable scratch for one step() call. Never escapes the function."""

    def __init__(self, state: SessionState, ex: Extraction, store: Store, consent: ConsentTool):
        turn = state.turn + 1
        self.s = replace(state, turn=turn, memory=state.memory.merge(ex, turn))
        self.ex = ex
        self.store = store
        self.consent = consent
        self.directives: list[Directive] = []
        self.effects: list[Effect] = []
        self.facts: list[Fact] = []
        self.switched = False  # guards the switching_claim branch on fall-through

    def say(self, kind: str, **args: Any) -> None:
        self.directives.append(Directive(kind, args))

    def go(self, phase: Phase, sub_state: SubState | None = None) -> None:
        assert_transition(self.s.phase, phase)
        self.s = replace(self.s, phase=phase, sub_state=sub_state)

    def set(self, **changes: Any) -> None:
        self.s = replace(self.s, **changes)

    def mem(self, **changes: Any) -> None:
        self.s = replace(self.s, memory=self.s.memory.model_copy(update=changes))

    def record(self) -> Any:
        return self.store.policyholder_by_party(self.s.memory.verified_party_id)

    def result(self) -> StepResult:
        plan = TurnPlan(self.s.phase, tuple(self.directives), tuple(self.facts))
        return StepResult(self.s, plan, tuple(self.effects))


# phase handlers return True to fall through into the new phase's handler

def _defer_if_asked(t: _Turn) -> None:
    """A claim question is one with an intent or a case hint. A bare
    question_text ("what do you mean?") is a clarification, not a claim
    question, and must not be deferred."""
    ex = t.ex
    if ex.intent or ex.case_hint_text or ex.case_type or ex.case_id or ex.status or ex.month:
        t.say("DEFER_CLAIM_QUESTION")


def _consent(t: _Turn, first: bool = False) -> bool:
    status = t.consent.peek(t.s.consent_polls)
    if status == "approved":
        t.set(consent_status="approved")
        t.say("CONSENT_APPROVED")
        t.go(Phase.RESOLVE_INTENT)
        return True
    if status == "pending":
        t.effects.append(Effect("consent_advance"))
        t.set(consent_status="pending")
        t.say("CONSENT_REQUESTED" if first else "CONSENT_STILL_PENDING")
        return False
    t.set(consent_status="timeout")
    t.say("CONSENT_NOT_RECEIVED_OFFER_HUMAN")
    return False


def _verify(t: _Turn) -> bool:
    ex, s = t.ex, t.s
    if s.sub_state == "awaiting_consent":
        return _consent(t)

    supplied = _supplied_identity(ex)
    refusing = ex.refuses_verification or ex.emotion == "refusing"
    if refusing and not supplied:
        t.set(refusals=s.refusals + 1)
        t.say("EXPLAIN_WHY_VERIFY")
        probe = verify_identity(s.memory, t.store)
        if refusal_rung(t.s.refusals) == "explain_offer_alt_fields":
            t.say("OFFER_ALT_FIELDS", fields=list(probe.missing), id_type=probe.id_type)
        else:
            t.say("OFFER_HUMAN")
        _defer_if_asked(t)
        return False

    if s.awaiting_reconfirm and not supplied:
        t.say("PII_MISMATCH_RETRY")
        if verify_offer_human(s.verify_attempts):
            t.say("VERIFY_OFFER_HUMAN")
        _defer_if_asked(t)
        return False

    res = verify_identity(s.memory, t.store)
    if res.status == "verified":
        t.set(awaiting_reconfirm=False)
        t.mem(verified_party_id=res.party_id)
        t.say("VERIFIED", name=t.record().name)
        if t.s.memory.caller_role == "representative":
            if not verify_representative(t.s.memory, t.store, res.party_id):
                t.say("REP_NOT_FOUND")
                return False
            t.set(sub_state="awaiting_consent")
            return _consent(t, first=True)
        t.go(Phase.RESOLVE_INTENT)
        return True

    if res.status == "mismatch":
        t.set(verify_attempts=s.verify_attempts + 1, awaiting_reconfirm=True)
        t.set(memory=t.s.memory.invalidate(res.mismatched))
        t.say("PII_MISMATCH_RETRY")
    else:
        t.set(awaiting_reconfirm=False)
        t.say(
            "ASK_FOR_PII",
            fields=list(res.missing),
            unparseable=list(res.unparseable),
            id_type=res.id_type,
            located=res.party_id is not None,
        )
    if verify_offer_human(t.s.verify_attempts):
        t.say("VERIFY_OFFER_HUMAN")
    _defer_if_asked(t)
    return False


def _resolve(t: _Turn) -> bool:
    memory = t.s.memory
    claims = t.store.claims_for_party(memory.verified_party_id)
    res = resolve(memory, claims)
    summaries = [_claim_summary(c) for c in res.candidates]
    if res.status == "resolved":
        t.mem(resolved_case_id=res.claim.case_id)
        t.say("CONFIRM_RESOLVED_CASE", **_claim_summary(res.claim))
        t.go(Phase.PROCESS_CASE)
        # Always answer in the same turn: the caller mentioned this claim, so
        # confirming it and then saying nothing reads as broken.
        return True
    if res.status == "ambiguous":
        t.say("ASK_WHICH_CLAIM", candidates=summaries)
    elif res.status == "none":
        t.say("NO_MATCHING_CLAIM", candidates=summaries)
    else:
        t.say("ASK_HOW_CAN_I_HELP", candidates=summaries)
    return False


def _process(t: _Turn) -> bool:
    ex = t.ex
    if ex.switching_claim and not t.switched:
        t.switched = True
        t.set(memory=_reset_case_hints(t.s.memory, ex, t.s.turn))
        t.go(Phase.RESOLVE_INTENT)
        return True
    claim = t.store.claim_by_id(t.s.memory.resolved_case_id)
    intent = ex.intent or t.s.memory.intent
    if intent is None:
        # "calling about my denied claim" with no question still deserves an
        # answer; pick the one the record makes obvious.
        intent = "denial_question" if claim.status == "denied" else "status_inquiry"
    question = ex.question_text or _DEFAULT_QUESTION[intent]
    facts = select(intent, question, claim, t.store)
    t.facts = facts
    t.say("ANSWER_FROM_FACTS", question=question, intent=intent)
    guidance = [f for f in facts if not f.source.startswith(("claim", "schema:"))]
    if intent in FOLLOWUP_INTENTS and not guidance:
        t.say("NOT_COVERED_OFFER_HUMAN")
    t.set(discussed=t.s.discussed + ((claim.case_id, question),))
    t.say("OFFER_ANYTHING_ELSE")
    return False


def _post(t: _Turn) -> bool:
    ex = t.ex
    record = t.record()
    if ex.email_decision == "yes":
        case_id = t.s.memory.resolved_case_id
        t.effects.append(
            Effect(
                "email_send",
                {
                    "to": record.email,
                    "recipient_name": record.name,
                    "case_id": case_id,
                    # the summary covers the final claim only (NOTES D7)
                    "discussed": [q for cid, q in t.s.discussed if cid == case_id],
                },
            )
        )
        t.say("EMAIL_WILL_BE_SENT", email=mask_email(record.email))
        t.go(Phase.CLOSED)
        return False
    if ex.email_decision == "no":
        t.say("EMAIL_SKIPPED")
        t.go(Phase.CLOSED)
        return False
    if _has_question(ex):
        t.go(Phase.PROCESS_CASE)
        return True
    t.say("REPEAT_EMAIL_OFFER", email=mask_email(record.email))
    return False


def _closed(t: _Turn) -> bool:
    if _has_question(t.ex):
        t.go(Phase.PROCESS_CASE if t.s.memory.resolved_case_id else Phase.RESOLVE_INTENT)
        return True
    t.say("SESSION_CLOSED_INVITE_MORE")
    return False


def _wrap_up(t: _Turn) -> StepResult:
    """Cross-phase: the caller is done. Offer the summary if a claim was
    discussed, otherwise close without one."""
    if t.s.memory.resolved_case_id:
        t.go(Phase.POST_PROCESS, sub_state="awaiting_email_decision")
        t.say("OFFER_EMAIL_SUMMARY", email=mask_email(t.record().email))
    else:
        t.go(Phase.POST_PROCESS)
        t.go(Phase.CLOSED)
        t.say("CLOSE_WITHOUT_SUMMARY")
    return t.result()


_HANDLERS = {
    Phase.VERIFY_ID: _verify,
    Phase.RESOLVE_INTENT: _resolve,
    Phase.PROCESS_CASE: _process,
    Phase.POST_PROCESS: _post,
    Phase.CLOSED: _closed,
}


def step(state: SessionState, ex: Extraction, store: Store, consent: ConsentTool) -> StepResult:
    t = _Turn(state, ex, store, consent)

    if state.phase is Phase.ESCALATED:
        substantive = not is_pure_noise(ex) and (ex.has_signal() or bool(ex.question_text))
        if not substantive or ex.wants_human:
            t.say("SESSION_ENDED")
            return t.result()
        t.go(state.escalated_from or Phase.VERIFY_ID, sub_state=state.sub_state)
        t.set(escalated_from=None)
        t.say("RESUME_AFTER_HANDOFF")

    if ex.emotion != "neutral":
        t.say("EMPATHIZE", emotion=ex.emotion)

    if ex.wants_human:
        t.set(escalated_from=t.s.phase, handoffs=state.handoffs + 1)
        t.go(Phase.ESCALATED, sub_state=t.s.sub_state)
        t.say("HANDOFF_HUMAN")
        return t.result()

    if is_pure_noise(ex):
        t.set(oos_streak=state.oos_streak + 1)
        rung = out_of_scope_rung(t.s.oos_streak)
        t.say("DECLINE_OFF_TOPIC" if rung == "decline" else "DECLINE_OFF_TOPIC_OFFER_HUMAN")
        return t.result()
    t.set(oos_streak=0)

    if ex.wants_to_wrap_up and t.s.phase in (Phase.RESOLVE_INTENT, Phase.PROCESS_CASE):
        return _wrap_up(t)

    for _ in range(len(Phase)):  # bounded fall-through
        before = t.s.phase
        keep_going = _HANDLERS[before](t)
        if not keep_going or t.s.phase is before:
            break
    return t.result()


def apply_effects(state: SessionState, effects: tuple[Effect, ...]) -> SessionState:
    """The state-only part of commit: advance counters for effects that have
    a state component. email_send has none."""
    polls = state.consent_polls + sum(1 for e in effects if e.kind == "consent_advance")
    return replace(state, consent_polls=polls)
