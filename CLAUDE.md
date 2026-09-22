# CLAUDE.md

## Project
SOP-guided insurance claims support agent. FastAPI backend, single-file HTML chat UI,
shipped as a Docker image. Text agent, not voice.

## Scope
All four phases, representative/consent path, consent timeout, out-of-scope guard
with escalation, emotional de-escalation, scripted eval, Dockerfile, README.
The email send is mocked and logged; there is no real email provider.

## Non-negotiable architecture
- Deterministic FSM owns phase transitions: VERIFY_ID -> RESOLVE_INTENT ->
  PROCESS_CASE -> POST_PROCESS. The LLM NEVER decides which phase we are in.
- The extractor runs on EVERY user turn regardless of phase and writes to one
  shared CaseMemory. This is how cross-phase memory works.
- Gates are pure Python functions with unit tests. Verification requires >= 3
  matched PII fields. Never prompt-enforced.
- Grounded answers come from deterministic retrieval over
  required_document_guideline.json. The LLM rephrases filled templates only.
- Intent enum is exactly the five intent_hints labels in that file.
- DEMO_NOW = 2026-09-18 in config. Never the real system date.

## Conventions
- Every deterministic module ships with pytest tests in tests/.
- data/ holds synthetic fixtures. Changing one means updating tests and eval
  scenarios with it.
- Tests run with `python -m pytest`; the live eval runs with `python -m eval.run`.
- requirements.txt is hand-maintained. Never regenerate it with pip freeze.
- Prose in docs. No emoji.
- PROJECT.md describes what this system does and why. ARCHITECTURE.md describes
  how the pieces fit. NOTES.md is the decision log.
