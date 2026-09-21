"""Live eval runner.

    .venv/bin/python -m eval.run                 # all scenarios
    .venv/bin/python -m eval.run rep_timeout     # one or more by name
    .venv/bin/python -m eval.run --list

A failing turn is recorded and the scenario continues; a failing scenario is
recorded and the run continues. Exit code is 1 if anything failed. One
transcript per scenario is written to eval/transcripts/<name>.md.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from app import config
from app.agent import Agent
from app.session import Session, SessionRegistry
from app.store import Store
from eval.scenarios import (
    GLOBAL_FORBID_IN_REPLY,
    MAX_REPLY_CHARS,
    OPENING_WORDS,
    SCENARIOS,
    UNSET,
    Scenario,
    Turn,
    by_name,
)

TRANSCRIPTS = Path(__file__).parent / "transcripts"
FENCE = "```"


def soft_checks(reply: str, previous_reply: str | None) -> list[str]:
    """Voice warnings: too long for a chat turn, or the same opening as the
    reply before. Surfaced, never failing."""
    warns: list[str] = []
    if len(reply) > MAX_REPLY_CHARS:
        warns.append(f"long reply ({len(reply)} chars)")
    if previous_reply:
        def opening(text: str) -> list[str]:
            return [w.strip(".,!?;:").lower() for w in text.split()[:OPENING_WORDS]]
        a, b = opening(reply), opening(previous_reply)
        if a and a == b:
            warns.append(f"same opening as previous reply ({' '.join(a)!r})")
    return warns


@dataclass
class ScenarioResult:
    name: str
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    turns_run: int = 0
    usage: dict = field(default_factory=dict)  # model -> Usage
    cost_usd: float = 0.0
    transcript_md: str = ""

    @property
    def ok(self) -> bool:
        return not self.failures


def check_turn(session: Session, reply: str, turn: Turn) -> list[str]:
    st = session.state
    kinds = [d.kind for d in session.last_plan.directives] if session.last_plan else []
    sources = [f.source for f in session.last_plan.facts] if session.last_plan else []
    fails: list[str] = []
    if turn.phase and st.phase.value != turn.phase:
        fails.append(f"phase {st.phase.value} != {turn.phase}")
    if turn.phase_in and st.phase.value not in turn.phase_in:
        fails.append(f"phase {st.phase.value} not in {turn.phase_in}")
    if turn.sub_state is not UNSET and st.sub_state != turn.sub_state:
        fails.append(f"sub_state {st.sub_state!r} != {turn.sub_state!r}")
    for k in turn.directives:
        if k not in kinds:
            fails.append(f"missing directive {k} (got {kinds})")
    for k in turn.forbid_directives:
        if k in kinds:
            fails.append(f"forbidden directive {k} present")
    for f, want in turn.memory.items():
        got = getattr(st.memory, f)
        if got != want:
            fails.append(f"memory.{f} {got!r} != {want!r}")
    for c, want in turn.counters.items():
        got = getattr(st, c)
        if got != want:
            fails.append(f"{c} {got} != {want}")
    for s in turn.fact_sources:
        if s not in sources:
            fails.append(f"missing fact source {s}")
    got_effects = [e.kind for e in session.last_effects]
    for e in turn.effects:
        if e not in got_effects:
            fails.append(f"missing effect {e}")
    if turn.email_count is not None and len(session.email_log) != turn.email_count:
        fails.append(f"email_count {len(session.email_log)} != {turn.email_count}")
    low = reply.lower()
    for s in (*GLOBAL_FORBID_IN_REPLY, *turn.forbid_in_reply):
        if s.lower() in low:
            fails.append(f"reply contains forbidden text {s!r}")
    return fails


def run_scenario(scenario: Scenario, store: Store, llm) -> ScenarioResult:
    result = ScenarioResult(scenario.name)
    lines = [f"# {scenario.name}", "", f"{scenario.note} Consent scenario: {scenario.consent}.", "",
             "Dialogue first; the bracketed line after each agent reply is the phase, the",
             "directives the reply was built from, and the check result.", ""]
    if hasattr(llm, "reset_usage"):
        llm.reset_usage()
    try:
        registry = SessionRegistry(store)
        session = registry.create(scenario.consent)
        agent = Agent(store, llm)
        previous_reply: str | None = None
        for i, turn in enumerate(scenario.turns, start=1):
            try:
                reply = agent.handle_turn(session, turn.say)
            except Exception as exc:  # never abort the run
                result.failures.append(f"turn {i}: agent raised {exc!r}")
                lines += [f"Caller: {turn.say}", "", f"Agent: (error: {exc!r})", ""]
                traceback.print_exc()
                continue
            result.turns_run += 1
            fails = check_turn(session, reply, turn)
            warns = soft_checks(reply, previous_reply)
            previous_reply = reply
            result.failures += [f"turn {i}: {f}" for f in fails]
            result.warnings += [f"turn {i}: {w}" for w in warns]
            st = session.state
            kinds = [d.kind for d in session.last_plan.directives] if session.last_plan else []
            where = st.phase.value + (f"/{st.sub_state}" if st.sub_state else "")
            verdict = "PASS" if not fails else "FAIL: " + "; ".join(fails)
            if warns:
                verdict += " | WARN: " + "; ".join(warns)
            lines += [
                f"Caller: {turn.say}", "",
                f"Agent: {reply}", "",
                f"    [{where} | {', '.join(kinds)} | {verdict}]", "",
            ]
        if session.email_log:
            lines += ["Email sent:", "", FENCE, session.email_log[-1].body, FENCE, ""]
    except Exception as exc:
        result.failures.append(f"scenario setup raised {exc!r}")
        traceback.print_exc()
    if hasattr(llm, "usage"):
        result.usage = dict(llm.usage)
        result.cost_usd = llm.total_cost_usd()
        for model, u in result.usage.items():
            lines.append(f"usage {model}: {u.calls} calls, {u.input_tokens} in, {u.output_tokens} out")
        lines.append(f"estimated cost: ${result.cost_usd:.4f}")
    result.transcript_md = "\n".join(lines) + "\n"
    return result


def run(names: list[str], store: Store, llm, out_dir: Path = TRANSCRIPTS,
        scenarios: list[Scenario] | None = None) -> int:
    scenarios = by_name(names) if scenarios is None else scenarios
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ScenarioResult] = []
    for sc in scenarios:
        print(f"=== {sc.name} ({len(sc.turns)} turns) ===", flush=True)
        r = run_scenario(sc, store, llm)
        (out_dir / f"{sc.name}.md").write_text(r.transcript_md)
        status = "PASS" if r.ok else f"FAIL ({len(r.failures)})"
        warn = f"  warnings={len(r.warnings)}" if r.warnings else ""
        print(f"  {status}  turns={r.turns_run}/{len(sc.turns)}  cost=${r.cost_usd:.4f}{warn}")
        for f in r.failures:
            print(f"    - {f}")
        for w in r.warnings:
            print(f"    ~ {w}")
        results.append(r)
    total = sum(r.cost_usd for r in results)
    passed = sum(1 for r in results if r.ok)
    print()
    print(f"{passed}/{len(results)} scenarios passed, estimated total cost ${total:.4f}")
    print(f"transcripts in {out_dir}")
    return 0 if passed == len(results) else 1


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for s in SCENARIOS:
            print(f"{s.name:20s} {len(s.turns)} turns  {s.note}")
        return 0
    names = [a for a in argv if not a.startswith("--")]
    try:
        by_name(names)
    except KeyError as exc:
        print(exc)
        return 2
    from app.llm import LLM  # real client only here

    config.require_api_key()
    return run(names, Store(), LLM())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
