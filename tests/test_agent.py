import pytest

from app.agent import FALLBACK_REPLY, GREETING, Agent
from app.email_summary import required_values
from app.fsm import Phase
from app.memory import Extraction
from app.session import SessionRegistry
from tests.fakes import FakeLLM

NADIA_FULL = Extraction(
    caller_role="policyholder", claimed_name="Nadia Okonkwo", policy_number="POL-3318",
    dob="1987-06-09", id_last4="2907", case_type="healthcare", status="denied", month=7,
    case_hint_text="denied healthcare claim from July", intent="denial_question",
)


@pytest.fixture
def registry(store):
    return SessionRegistry(store)


def make_agent(store, llm):
    return Agent(store, llm)


def test_registry_creates_and_finds_sessions(registry):
    s = registry.create("default")
    assert registry.get(s.id) is s
    assert registry.get("nope") is None
    assert s.state.scenario == "default"
    assert s.transcript == []
    with pytest.raises(ValueError):
        registry.create("bogus")


def test_greeting_is_fixed_text():
    from app import config
    assert GREETING.startswith(f"Hi, this is {config.AGENT_NAME}")
    assert "policy number" in GREETING.lower()
    assert "\u2014" not in GREETING


def test_happy_turn_commits_state_and_transcript(store, registry):
    llm = FakeLLM([NADIA_FULL], replies=["Thanks Nadia, you're verified..."])
    agent = make_agent(store, llm)
    session = registry.create("default")
    reply = agent.handle_turn(session, "I'm the policyholder. My name is Nadia Okonkwo...")
    assert reply.startswith("Thanks Nadia")
    assert session.state.phase is Phase.PROCESS_CASE
    assert session.state.turn == 1
    assert [t["role"] for t in session.transcript] == ["user", "assistant"]
    assert session.transcript[1]["text"] == reply
    assert session.last_extraction == NADIA_FULL
    assert session.last_plan is not None and session.last_plan.phase is Phase.PROCESS_CASE
    assert session.email_log == []


def test_responder_uses_next_phase_and_clean_history(store, registry):
    llm = FakeLLM([NADIA_FULL, Extraction(question_text="how long will it take?", intent="next_steps")])
    agent = make_agent(store, llm)
    session = registry.create("default")
    agent.handle_turn(session, "first message")
    agent.handle_turn(session, "how long will it take?")
    respond_calls = [kw for name, kw in llm.calls if name == "respond"]
    assert respond_calls[0]["phase"] is Phase.PROCESS_CASE  # verified and resolved in the same turn
    assert "Instructions for this reply" in respond_calls[0]["plan_text"]
    assert "VERIFIED" in respond_calls[0]["log_context"]
    # history carries only user text and assistant replies, never plan text
    hist = respond_calls[1]["history"]
    assert hist == [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "[reply in PROCESS_CASE]"},
    ]


def test_responder_failure_commits_nothing(store, registry):
    llm = FakeLLM([NADIA_FULL], fail_respond=True)
    agent = make_agent(store, llm)
    session = registry.create("default")
    before_state = session.state
    reply = agent.handle_turn(session, "I'm Nadia...")
    assert reply == FALLBACK_REPLY
    assert session.state == before_state
    assert session.transcript == []
    assert session.email_log == []
    assert session.last_plan is None


def test_extractor_failure_is_an_empty_turn(store, registry):
    class Boom(FakeLLM):
        def extract(self, phase, history, text):
            raise RuntimeError("network")
    llm = Boom([])
    agent = make_agent(store, llm)
    session = registry.create("default")
    reply = agent.handle_turn(session, "hello?")
    assert reply == "[reply in VERIFY_ID]"
    assert session.state.turn == 1
    assert session.last_extraction == Extraction()


def test_consent_effect_is_applied_in_commit(store, registry):
    rep = Extraction(caller_role="representative", claimed_name="Julian Okonkwo", rep_name="Julian Okonkwo",
                     rep_relationship="son", policyholder_name="Nadia Okonkwo", policy_number="POL-3318",
                     dob="1987-06-09", phone="4155550182")
    llm = FakeLLM([rep])
    agent = make_agent(store, llm)
    session = registry.create("default")
    agent.handle_turn(session, "calling for my mother")
    assert session.state.consent_polls == 1
    assert session.state.consent_status == "pending"
    assert session.last_effects[0].kind == "consent_advance"


def _run_to_email(store, registry, llm):
    agent = make_agent(store, llm)
    session = registry.create("default")
    agent.handle_turn(session, "I'm Nadia...")
    agent.handle_turn(session, "that's all, thanks")
    agent.handle_turn(session, "yes please")
    return session


def test_email_yes_sends_template_when_smoothing_is_rejected(store, registry):
    llm = FakeLLM(
        [NADIA_FULL, Extraction(wants_to_wrap_up=True), Extraction(email_decision="yes")],
        smoothed="What we discussed\nYou will get 1680.00 USD by October 20, 2026.\n\nNext steps\nNothing.",
    )
    session = _run_to_email(store, registry, llm)
    assert session.state.phase is Phase.CLOSED
    assert len(session.email_log) == 1
    rec = session.email_log[0]
    assert rec.to == "nadia@email.com"
    assert rec.subject == "Summary of your claim CLM-7710"
    claim = store.claim_by_id("CLM-7710")
    for value in required_values(claim):
        assert value in rec.body
    assert "1680.00 USD by March 20" not in rec.body
    assert "- Why was my claim denied?" in rec.body  # template prose kept
    assert any(name == "smooth" for name, _ in llm.calls)


def test_email_yes_uses_grounded_smoothing(store, registry):
    llm = FakeLLM(
        [NADIA_FULL, Extraction(wants_to_wrap_up=True), Extraction(email_decision="yes")],
        smoothed="What we discussed\nWe talked through why your claim was denied.\n\nNext steps\nSend both documents through the portal.",
    )
    session = _run_to_email(store, registry, llm)
    body = session.email_log[0].body
    assert "We talked through why your claim was denied." in body
    assert "Claim status and outcome" in body
    assert "Amount paid on this claim (net pay): 0.00 USD" in body


def test_email_yes_survives_smoothing_failure(store, registry):
    llm = FakeLLM(
        [NADIA_FULL, Extraction(wants_to_wrap_up=True), Extraction(email_decision="yes")],
        fail_smooth=True,
    )
    session = _run_to_email(store, registry, llm)
    assert len(session.email_log) == 1
    assert "- Why was my claim denied?" in session.email_log[0].body


def test_email_no_sends_nothing(store, registry):
    llm = FakeLLM([NADIA_FULL, Extraction(wants_to_wrap_up=True), Extraction(email_decision="no")])
    agent = make_agent(store, llm)
    session = registry.create("default")
    for text in ("a", "b", "c"):
        agent.handle_turn(session, text)
    assert session.state.phase is Phase.CLOSED
    assert session.email_log == []
    assert not any(name == "smooth" for name, _ in llm.calls)
