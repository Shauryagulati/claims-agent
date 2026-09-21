import pytest

from app.agent import Agent
from app.memory import Extraction
from app.session import SessionRegistry
from eval.run import check_turn, run, run_scenario, soft_checks
from eval.scenarios import SCENARIOS, Scenario, Turn, by_name
from tests.fakes import FakeLLM

NADIA = Extraction(
    caller_role="policyholder", claimed_name="Nadia Okonkwo", policy_number="POL-3318",
    dob="1987-06-09", id_last4="2907", case_type="healthcare", status="denied", month=7,
    intent="denial_question",
)


def test_scenario_names_are_unique_and_by_name_validates():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))
    assert len(SCENARIOS) == 8
    assert by_name([]) == SCENARIOS
    assert [s.name for s in by_name(["typo_recovery", "nadia_happy"])] == ["typo_recovery", "nadia_happy"]
    with pytest.raises(KeyError, match="unknown scenario"):
        by_name(["nope"])


def test_every_turn_expectation_uses_valid_names():
    from app.fsm import DIRECTIVE_KINDS, Phase, SessionState
    from app.memory import CaseMemory
    phases = {p.value for p in Phase}
    state_fields = set(SessionState.__dataclass_fields__)
    memory_fields = set(CaseMemory.model_fields)
    for s in SCENARIOS:
        for t in s.turns:
            if t.phase:
                assert t.phase in phases
            assert set(t.phase_in) <= phases
            assert set(t.directives) <= set(DIRECTIVE_KINDS), s.name
            assert set(t.forbid_directives) <= set(DIRECTIVE_KINDS), s.name
            assert set(t.memory) <= memory_fields, s.name
            assert set(t.counters) <= state_fields, s.name


def test_check_turn_reports_each_kind_of_failure(store):
    llm = FakeLLM([NADIA])
    agent = Agent(store, llm)
    session = SessionRegistry(store).create("default")
    reply = agent.handle_turn(session, "hi")
    ok = Turn("hi", phase="PROCESS_CASE", directives=("VERIFIED",), memory={"verified_party_id": "PH-4021"},
              counters={"verify_attempts": 0}, fact_sources=("claim",), email_count=0)
    assert check_turn(session, reply, ok) == []
    bad = Turn("hi", phase="VERIFY_ID", phase_in=("CLOSED",), sub_state="awaiting_consent",
               directives=("HANDOFF_HUMAN",), forbid_directives=("VERIFIED",),
               memory={"verified_party_id": "PH-4007"}, counters={"verify_attempts": 9},
               fact_sources=("guideline:nope",), effects=("email_send",), email_count=3,
               forbid_in_reply=("reply",))
    fails = check_turn(session, reply, bad)
    for needle in ("phase PROCESS_CASE != VERIFY_ID", "not in", "sub_state", "missing directive HANDOFF_HUMAN",
                   "forbidden directive VERIFIED", "memory.verified_party_id", "verify_attempts 0 != 9",
                   "missing fact source", "missing effect email_send", "email_count 0 != 3", "forbidden text"):
        assert any(needle in f for f in fails), needle


def test_global_style_forbids_apply_to_every_reply(store):
    llm = FakeLLM([NADIA], replies=["Thanks, Nadia \u2014 I'd be happy to help. **Great question**"])
    agent = Agent(store, llm)
    session = SessionRegistry(store).create("default")
    reply = agent.handle_turn(session, "hi")
    fails = check_turn(session, reply, Turn("hi"))
    joined = " ".join(fails)
    assert "\u2014" in joined and "I'd be happy to" in joined and "great question" in joined and "**" in joined


def test_soft_checks_warn_on_length_and_repeated_opening():
    assert soft_checks("Short and fine.", None) == []
    long = "word " * 200
    assert any("long reply" in w for w in soft_checks(long, None))
    assert any("same opening" in w for w in soft_checks("Thanks, Nadia. Done.", "Thanks, Nadia. Next."))
    assert soft_checks("Right, Nadia. Done.", "Thanks, Nadia. Next.") == []


def test_warnings_are_reported_not_failed(store):
    llm = FakeLLM([NADIA, Extraction(wants_to_wrap_up=True)],
                  replies=["Thanks, Nadia. " + "detail " * 150, "Thanks, Nadia. Email?"])
    sc = Scenario("t", "default", (Turn("a", phase="PROCESS_CASE"), Turn("b", phase="POST_PROCESS")))
    r = run_scenario(sc, store, llm)
    assert r.ok
    assert any("long reply" in w for w in r.warnings)
    assert any("same opening" in w for w in r.warnings)
    assert "WARN:" in r.transcript_md


def test_failed_turn_does_not_abort_scenario(store):
    llm = FakeLLM([NADIA, Extraction(wants_to_wrap_up=True)])
    sc = Scenario("t", "default", (
        Turn("a", phase="VERIFY_ID"),          # wrong on purpose
        Turn("b", phase="POST_PROCESS"),       # still runs and passes
    ))
    r = run_scenario(sc, store, llm)
    assert r.turns_run == 2
    assert r.failures == ["turn 1: phase PROCESS_CASE != VERIFY_ID"]
    assert r.transcript_md.count("Caller: ") == 2
    assert r.transcript_md.count("Agent: ") == 2
    assert "| FAIL: phase PROCESS_CASE != VERIFY_ID]" in r.transcript_md
    assert "| PASS" in r.transcript_md
    # dialogue comes before its annotation
    assert r.transcript_md.index("Agent: ") < r.transcript_md.index("[PROCESS_CASE")


def test_agent_exception_is_recorded_not_raised(store):
    class Boom(FakeLLM):
        def extract(self, *a, **k):
            raise RuntimeError("down")
        def respond(self, *a, **k):
            raise RuntimeError("down")
    sc = Scenario("t", "default", (Turn("a", phase="VERIFY_ID"),))
    r = run_scenario(sc, store, Boom([]))
    assert r.turns_run == 0
    assert r.failures and "agent raised" in r.failures[0]


def test_run_writes_transcripts_continues_and_returns_exit_code(store, tmp_path, capsys):
    llm = FakeLLM([NADIA] * 4)  # shared across scenarios; each consumes one
    two = [Scenario("one", "default", (Turn("a", phase="PROCESS_CASE"),)),
           Scenario("two", "default", (Turn("a", phase="CLOSED"),))]
    code = run([], store, llm, out_dir=tmp_path, scenarios=two)
    assert code == 1
    assert (tmp_path / "one.md").exists() and (tmp_path / "two.md").exists()
    out = capsys.readouterr().out
    assert "1/2 scenarios passed" in out
    assert "PASS" in out and "FAIL (1)" in out
