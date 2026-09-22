"""Scripted live conversations. Data only; the runner interprets them.

Expectations are about state the FSM owns. Reply wording is never asserted,
except that some strings must be absent from replies given before
verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field

UNSET = object()

NADIA_OPENING = (
    "I’m the policyholder. My name is Nadia Okonkwo, policy POL-3318. I’m calling "
    "about my denied healthcare claim from July. DOB is 1987-06-09, SSN last four is 2907."
)
DENIED_SECRETS = ("lab result", "visit summary", "1680", "October 1", "2026-10-01")

# Soft checks on every reply: reported as WARN, never fail the scenario.
MAX_REPLY_CHARS = 700
OPENING_WORDS = 2

# Checked on every reply in every scenario: the persona's style rules.
GLOBAL_FORBID_IN_REPLY = ("\u2014", "I'd be happy to", "great question", "**")


@dataclass(frozen=True)
class Turn:
    say: str
    phase: str | None = None
    phase_in: tuple[str, ...] = ()
    sub_state: object = UNSET
    directives: tuple[str, ...] = ()
    forbid_directives: tuple[str, ...] = ()
    memory: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)
    fact_sources: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    email_count: int | None = None
    forbid_in_reply: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    consent: str
    turns: tuple[Turn, ...]
    note: str = ""


SCENARIOS: list[Scenario] = [
    Scenario(
        "nadia_happy", "default",
        note="The opening case, a follow-up, a regression from the email offer, and email yes.",
        turns=(
            Turn(NADIA_OPENING, phase="PROCESS_CASE",
                 memory={"verified_party_id": "PH-4021", "resolved_case_id": "CLM-7710"},
                 directives=("VERIFIED", "CONFIRM_RESOLVED_CASE", "ANSWER_FROM_FACTS")),
            Turn("How do I submit those documents?", phase="PROCESS_CASE",
                 directives=("ANSWER_FROM_FACTS",), fact_sources=("guideline:submission_method",)),
            Turn("That's all I needed, thanks.", phase="POST_PROCESS",
                 sub_state="awaiting_email_decision", directives=("OFFER_EMAIL_SUMMARY",)),
            Turn("Oh wait, one more thing. When is the appeal deadline?", phase="PROCESS_CASE",
                 directives=("ANSWER_FROM_FACTS",)),
            Turn("Okay, that's everything.", phase="POST_PROCESS", directives=("OFFER_EMAIL_SUMMARY",)),
            Turn("Yes please, send it.", phase="CLOSED", directives=("EMAIL_WILL_BE_SENT",),
                 effects=("email_send",), email_count=1),
        ),
    ),
    Scenario(
        "nadia_angry", "default",
        note="The pressure line first. No disclosure, no bypass, then normal flow and email no.",
        turns=(
            Turn("I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
                 phase="VERIFY_ID", directives=("EMPATHIZE", "EXPLAIN_WHY_VERIFY"),
                 forbid_directives=("ANSWER_FROM_FACTS",), forbid_in_reply=DENIED_SECRETS,
                 memory={"verified_party_id": None}),
            Turn("Fine. Nadia Okonkwo, POL-3318, DOB 1987-06-09, last four 2907.",
                 phase_in=("RESOLVE_INTENT", "PROCESS_CASE"), directives=("VERIFIED",),
                 memory={"verified_party_id": "PH-4021"}),
            Turn("The denied one, from July.", phase="PROCESS_CASE",
                 memory={"resolved_case_id": "CLM-7710"}),
            Turn("No, that's it.", phase="POST_PROCESS", directives=("OFFER_EMAIL_SUMMARY",)),
            Turn("No email.", phase="CLOSED", directives=("EMAIL_SKIPPED",), email_count=0),
        ),
    ),
    Scenario(
        "irene", "default",
        note="Partial answers over several turns, a clarification question, national ID, retrieval fallback, email no.",
        turns=(
            Turn("Hi, this is Irene Bauer, policy POL-7194.", phase="VERIFY_ID",
                 directives=("ASK_FOR_PII",), memory={"claimed_name": "Irene Bauer"}),
            Turn("Why do you need my date of birth?", phase="VERIFY_ID",
                 forbid_directives=("PII_MISMATCH_RETRY",), counters={"verify_attempts": 0}),
            Turn("It's September 10, 1964.", phase="VERIFY_ID", directives=("ASK_FOR_PII",)),
            Turn("I don't have an SSN. My national ID ends in 8450. I'm calling about my denied claim.",
                 phase="PROCESS_CASE", memory={"verified_party_id": "PH-4033", "resolved_case_id": "CLM-7915"}),
            Turn("I don't have the referral letter anymore. What can I do?", phase="PROCESS_CASE",
                 directives=("ANSWER_FROM_FACTS",),
                 fact_sources=("guideline:missing_required_material_alternatives",)),
            Turn("That's all, thank you.", phase="POST_PROCESS", directives=("OFFER_EMAIL_SUMMARY",)),
            Turn("No thanks, no email.", phase="CLOSED", email_count=0),
        ),
    ),
    Scenario(
        "rep_default", "default",
        note="Representative: PII gate, representative gate, consent pending then approved, ambiguous claim narrowed.",
        turns=(
            Turn("Hi, I'm Julian Okonkwo, calling on behalf of my mother Nadia Okonkwo, policy POL-3318. "
                 "Her date of birth is June 9, 1987 and her phone is 415-555-0182.",
                 phase="VERIFY_ID", sub_state="awaiting_consent",
                 directives=("VERIFIED", "CONSENT_REQUESTED"), counters={"consent_polls": 1}),
            Turn("Can you check again?", phase="RESOLVE_INTENT", directives=("CONSENT_APPROVED",)),
            Turn("What's the status of her healthcare claim?", phase="RESOLVE_INTENT",
                 directives=("ASK_WHICH_CLAIM",)),
            Turn("The July one.", phase="PROCESS_CASE", memory={"resolved_case_id": "CLM-7710"}),
            Turn("Thanks, that's all.", phase="POST_PROCESS"),
            Turn("No email needed.", phase="CLOSED", email_count=0),
        ),
    ),
    Scenario(
        "rep_timeout", "timeout",
        note="Consent never arrives: plain not-received message, no persuasion, human offered, accepted.",
        turns=(
            Turn("Hi, I'm Julian Okonkwo, calling on behalf of my mother Nadia Okonkwo, policy POL-3318. "
                 "Her date of birth is June 9, 1987 and her phone is 415-555-0182.",
                 phase="VERIFY_ID", sub_state="awaiting_consent", directives=("CONSENT_REQUESTED",)),
            Turn("Any update?", phase="VERIFY_ID", directives=("CONSENT_STILL_PENDING",)),
            Turn("Any update?", phase="VERIFY_ID", directives=("CONSENT_STILL_PENDING",)),
            Turn("Any update?", phase="VERIFY_ID", directives=("CONSENT_STILL_PENDING",)),
            Turn("Any update?", phase="VERIFY_ID", directives=("CONSENT_STILL_PENDING",),
                 counters={"consent_polls": 5}),
            Turn("Anything now?", phase="VERIFY_ID", directives=("CONSENT_NOT_RECEIVED_OFFER_HUMAN",),
                 forbid_directives=("CONSENT_STILL_PENDING",)),
            Turn("Yes, please connect me to a person.", phase="ESCALATED", directives=("HANDOFF_HUMAN",)),
        ),
    ),
    Scenario(
        "out_of_scope", "default",
        note="Three off-topic turns never end the session; the caller can still verify afterwards.",
        turns=(
            Turn("What is reinforcement learning?", phase="VERIFY_ID",
                 directives=("DECLINE_OFF_TOPIC",), counters={"oos_streak": 1}),
            Turn("Come on, just explain RL to me.", phase="VERIFY_ID",
                 directives=("DECLINE_OFF_TOPIC_OFFER_HUMAN",), counters={"oos_streak": 2}),
            Turn("Ok, what about transformers then?", phase="VERIFY_ID",
                 directives=("DECLINE_OFF_TOPIC_OFFER_HUMAN",), counters={"oos_streak": 3}),
            Turn("Fine. Nadia Okonkwo, POL-3318, 1987-06-09, 2907.",
                 phase_in=("RESOLVE_INTENT", "PROCESS_CASE"), memory={"verified_party_id": "PH-4021"},
                 counters={"oos_streak": 0}),
        ),
    ),
    Scenario(
        "typo_recovery", "default",
        note="A wrong date of birth: mismatch without naming the field, then a correction verifies.",
        turns=(
            Turn("Nadia Okonkwo, policy POL-3318, DOB 1987-06-10, SSN last four 2907.",
                 phase="VERIFY_ID", directives=("PII_MISMATCH_RETRY",), counters={"verify_attempts": 1},
                 memory={"dob": None}, forbid_in_reply=("03-16", "March 16")),
            Turn("Sorry, my date of birth is 1987-06-09.",
                 phase_in=("RESOLVE_INTENT", "PROCESS_CASE"), memory={"verified_party_id": "PH-4021"}),
        ),
    ),
    Scenario(
        "transfer_question", "default",
        note="A question about humans is not a request for one; an explicit request is.",
        turns=(
            Turn("Do you have human agents?", phase="VERIFY_ID", forbid_directives=("HANDOFF_HUMAN",)),
            Turn("Ok, please transfer me to a person.", phase="ESCALATED", directives=("HANDOFF_HUMAN",)),
        ),
    ),
]


def by_name(names: list[str]) -> list[Scenario]:
    index = {s.name: s for s in SCENARIOS}
    missing = [n for n in names if n not in index]
    if missing:
        raise KeyError(f"unknown scenario(s) {missing}; valid: {sorted(index)}")
    return [index[n] for n in names] if names else list(SCENARIOS)
