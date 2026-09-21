# What this is

A text-based insurance claims support agent that has to follow a fixed
business procedure without sounding like it is reading from one.

The problem it solves is a specific tension. A claims agent must not discuss
a claim with someone whose identity has not been established, must not invent
a denial reason, and must not skip the consent step when a family member
calls on a policyholder's behalf. Those are hard constraints. At the same
time the caller types like a person: partially, out of order, sometimes
angrily, often answering a question two steps ahead of the one they were
asked. A system that is rigid enough to be safe usually stops being usable,
and a system built on prompt instructions alone is neither.

## The approach

Split the problem so the language model never owns a decision that matters.

A deterministic finite state machine owns the workflow and moves through four
phases: VERIFY_ID, RESOLVE_INTENT, PROCESS_CASE, POST_PROCESS. It is ordinary
Python with unit tests. The model cannot advance it, skip it, or be argued out
of it, because it is never asked.

The model does two jobs per turn, both narrow. It reads the caller's message
into a typed structure, and it phrases a reply from facts and instructions the
state machine has already chosen. Before verification it is not given claim
data at all, so it cannot leak what it does not hold. That is a structural
guarantee rather than an instruction.

Everything the caller reveals is extracted on every turn regardless of which
phase is active, and written to one shared memory. This is why saying "I'm
calling about my denied claim from July" during identity verification still
works after verification finishes: the hint was stored when it was said, not
when it became relevant.

Grounded answers come from deterministic retrieval over a guideline file.
The retrieval picks the entry, fills the template, and hands the model filled
text to rephrase. The model is not asked what the policy is.

## What is worth looking at

- `app/fsm.py` and `app/gates.py`: the parts that make misbehaviour
  impossible rather than merely discouraged. Verification requires at least
  three matching PII fields and zero mismatches, as a pure function.
- `app/memory.py`: cross-phase memory, including what happens when a caller
  corrects a detail they got wrong.
- `app/retrieval.py`: how a reply gets its facts, and how every sentence in
  a reply can be traced back to one.
- The debug panel in the running app, which shows phase, memory, directives
  and the exact facts each reply was built from. It exists so the behaviour
  can be checked rather than believed.

## Status and scope

The data in `data/` is synthetic and exists to exercise edge cases: alias
matching, two claims that are ambiguous until a year is inferred, a national
ID holder, a representative whose consent request never arrives, a missing
document with no guidance entry on file. The email step is mocked and logged
rather than sent. There is no database; the fixtures are read-only JSON.

`ARCHITECTURE.md` describes how the pieces fit. `NOTES.md` records the
decisions and the reasoning behind them, including the ones that were wrong
the first time.
