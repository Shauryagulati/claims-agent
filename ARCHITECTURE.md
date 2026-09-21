# ARCHITECTURE.md

Design spec for the SOP-guided insurance claims support agent. Decisions and
their rationale are in NOTES.md. Scope is REQUIREMENTS.md as narrowed by
CLAUDE.md. This document says what gets built and how the pieces fit.

## 1. Goal in one paragraph

A text chat agent that walks a caller through four fixed phases, VERIFY_ID,
RESOLVE_INTENT, PROCESS_CASE, and POST_PROCESS, while sounding like a person.
Phase order, safety gates, and allowed actions are enforced by Python. The
LLM is used for exactly two jobs per turn: reading the user's message into a
typed structure, and turning a Python-chosen set of directives and facts into
natural language. The LLM never sees claim data before verification and never
chooses the phase.

## 2. The turn loop

Every user message goes through the same five steps, in every phase.

1. Extract. One LLM call with a Pydantic-validated structured output reads the
   message into an `Extraction` object: identity fields, caller role,
   representative details, claim hints, intent, scope flag, emotion, and a
   handful of yes-or-no signals (wants a human, wants to wrap up, email
   decision, refuses verification).
2. Merge. `CaseMemory.merge(extraction)` adds new facts. It never clears a
   field because a later message omitted it. Each field records the turn it
   was first seen.
3. Step. `fsm.step(state)` is a pure function. It takes the current session
   state and the merged memory and returns three things without mutating
   anything: the proposed next state (phase, sub-state, counters, memory),
   a `TurnPlan` (directives plus the facts block the responder may use), and
   a list of `Effect`s, which are tool side effects that have not happened
   yet. Tools are never called inside `step()`. Where the plan needs a tool
   outcome to phrase the reply, the tool exposes a pure `peek` that computes
   the outcome from state (the consent scenario is a fixed sequence indexed
   by poll count, so the result of the next poll is known in advance).
4. Respond. One LLM call whose system prompt is the fixed persona plus a
   phase block, and whose final user turn is the TurnPlan rendered as text.
   The conversation history is included so the reply reads as a continuation.
   Output is plain text.
5. Commit. Only after the responder returns: the proposed state replaces the
   session state, each `Effect` is executed (consent poll counter advance,
   email send and log), and the reply is appended to the transcript. The
   state, TurnPlan, extraction, and effects are exposed on the debug endpoint.

Steps 2 and 3 are deterministic and have no network access. Steps 1 and 4 are
the only places the model is called.

If the extractor call fails or returns something that does not validate,
the turn proceeds with an empty extraction. If the responder call fails,
nothing from step 3 is committed: no phase change, no counter change, no
memory change, and no tool effect. A fixed apology is returned and the caller
can resend. A transient API error therefore never advances a phase, burns a
consent poll, or sends an email.

## 3. Modules

All under `app/`. Each deterministic module has a matching test file under
`tests/`.

| Module | Responsibility | Depends on | LLM |
|---|---|---|---|
| `config.py` | Constants: `DEMO_NOW`, model ID, thresholds, data path | none | no |
| `store.py` | Loads the six JSON files once. Lookups: policyholder by policy number or name, claims by party, representative by names. Read only. | config | no |
| `normalize.py` | Pure helpers: name, date, phone, email, last-four normalisation | none | no |
| `memory.py` | `CaseMemory` and `Extraction` Pydantic models, `merge` | none | no |
| `gates.py` | `verify_identity`, `verify_representative`, `should_escalate_out_of_scope`, `refusal_ladder_step`. All pure. | normalize, store | no |
| `resolver.py` | Picks the claim from memory hints. Returns one claim, a candidate list, or nothing. | store, config | no |
| `retrieval.py` | Selects guideline entries for an intent and question, fills placeholders, returns a facts block | store | no |
| `tools.py` | `ConsentTool` with pure `peek(state)` and effectful `advance(session)`; `EmailTool.send` (mock, appends to the session email log). Effects are only executed by `agent.commit`. | store | no |
| `fsm.py` | `Phase` enum, allowed transition table, pure `step()`, `TurnPlan`, `Directive`, and `Effect` types | gates, resolver, retrieval, tools, memory | no |
| `llm.py` | Anthropic client wrapper: `extract(history, text) -> Extraction` on the extractor model, `respond(system, history, plan_text) -> str` on the responder model. Owns retries and fallbacks. | config | yes |
| `prompts.py` | Persona text, per-phase prompt blocks, directive renderers, extraction instructions | none | no |
| `agent.py` | `handle_turn(session, text) -> reply`. The five steps above. | everything | via llm |
| `session.py` | In-memory session registry keyed by UUID. Holds transcript, memory, phase, counters, scenario, email log. | memory | no |
| `main.py` | FastAPI app: routes and static file | agent, session | no |
| `static/index.html` | Chat UI with debug panel | none | no |

The transport boundary is `agent.handle_turn`. A voice pipeline could call
it with transcribed text and get text back; nothing in it knows about HTTP.

## 4. Data model

### CaseMemory

```
identity:
  claimed_name, dob, phone, email, id_last4, policy_number   (str | None each)
caller:
  role: "policyholder" | "representative" | "unknown"
  rep_name, rep_relationship, policyholder_name              (for representatives)
case_hints:
  case_id, case_type, status, month, year                    (str | int | None)
  free_text: list[str]                                       (verbatim hints, for the summary)
intent: one of the five labels or None
verified_party_id: str | None
resolved_case_id: str | None
invalidated_fields: set[field_name]     (identity fields discarded after a mismatch)
provenance: dict[field_name, turn_index]     (turn first seen)
last_changed: dict[field_name, turn_index]   (turn the stored value last changed)
```

Merge rule: a field is written only when the extraction supplies a non-null
value. An explicit correction ("sorry, my DOB is actually...") is just a
later non-null value and wins. Merge itself never sets a field back to None.

The one path that clears an identity field is the verification gate. When a
supplied value does not match the record, `step()` returns a next state in
which that field is None and its name is in `invalidated_fields`. The next
non-null value for that field from a later extraction fills it and removes
it from the set. Without this, a single wrong value would sit in memory
forever, every later gate run would report a mismatch, and the caller could
never verify. Matched fields are kept, so a caller who re-supplies all their
details verifies immediately.

### Extraction (the structured-output schema)

Every field optional except the booleans and enums, which default to their
neutral value. Enums are closed lists so the FSM can switch on them.

```
identity fields as above
caller_role, rep_name, rep_relationship, policyholder_name
case_id, case_type ("healthcare"|"dental"|"auto"|null), status ("denied"|"open"|"closed"|null),
month (1-12|null), year (int|null), case_hint_text
intent: "status_inquiry"|"denial_question"|"document_submission"|"next_steps"|"general_claim_question"|null
question_text: the user's actual question, normalised, or null
in_scope: bool
emotion: "neutral"|"frustrated"|"angry"|"anxious"|"confused"|"refusing"
refuses_verification: bool
wants_human: bool
wants_to_wrap_up: bool
email_decision: "yes"|"no"|null
switching_claim: bool
```

`Extraction.has_signal()` is true when any identity field, caller detail,
case hint, intent, or decision flag (`wants_human`, `wants_to_wrap_up`,
`email_decision`, `switching_claim`) is present. The out-of-scope ladder
consults it, see section 5.

The extractor sees the conversation history and the current phase name so it
can resolve references like "the July one", but the prompt tells it to
report only what the user said, never to infer identity fields.

### Session

```
id, created_at, scenario ("default"|"timeout")
phase: Phase
sub_state: None | "awaiting_consent" | "awaiting_email_decision"
memory: CaseMemory
transcript: list[{role, text}]
counters: {verify_attempts, refusals, out_of_scope_streak, consent_polls}
consent_status: None | "pending" | "approved" | "timeout"
email_log: list[{to, subject, body, sent_at}]
last_plan: TurnPlan | None
last_extraction: Extraction | None
```

## 5. The FSM

### Phases

```
VERIFY_ID -> RESOLVE_INTENT -> PROCESS_CASE -> POST_PROCESS -> CLOSED
any phase -> ESCALATED   (human handoff; entered only on explicit wants_human)
POST_PROCESS -> PROCESS_CASE   (new claim question before the email decision)
PROCESS_CASE -> RESOLVE_INTENT (caller switches claim)
CLOSED -> PROCESS_CASE         (verified caller asks another claim question)
CLOSED -> RESOLVE_INTENT       (same, when no claim was resolved before closing)
RESOLVE_INTENT -> POST_PROCESS (caller wraps up before any claim was discussed)
ESCALATED -> any non-terminal  (substantive message resumes the prior phase)
```

Nothing else. The table is a literal set of `(from, to)` pairs in `fsm.py`
and `step()` asserts every transition against it.

ESCALATED is entered by exactly one condition: the extraction has
`wants_human == true`. No ladder, counter, or gate escalates on its own. Every
ladder's final rung is "offer a human transfer", repeated on each further
turn until the caller accepts or moves on. This keeps anyone who is
deliberately probing a ladder from losing their session to an automatic
handoff. ESCALATED is not a dead end: the handoff is simulated, so a
substantive in-scope message (identity, hints, a decision, or a question)
resumes the phase the caller was in, sub-state preserved, with a
`RESUME_AFTER_HANDOFF` directive; a bare acknowledgement gets
`SESSION_ENDED`. CLOSED is not terminal either: a verified caller who asks
a claim question after closing reopens into PROCESS_CASE, or RESOLVE_INTENT
if no claim was ever resolved, and the email offer is made again when they
wrap up. Wrapping up is handled before phase dispatch for RESOLVE_INTENT and
PROCESS_CASE; with no resolved claim it closes without a summary offer.

### step() by phase

VERIFY_ID

- If `wants_human`: ESCALATED with directive `HANDOFF_HUMAN`.
- If `refuses_verification` or `emotion == "refusing"`: increment refusals,
  apply the ladder from NOTES D6. Directives `EMPATHIZE`, then
  `EXPLAIN_WHY_VERIFY` with `OFFER_ALT_FIELDS` on the first rung and
  `OFFER_HUMAN` on every rung after. Never escalates by itself.
- If `verify_attempts` has reached `VERIFY_OFFER_HUMAN_AT`: every turn in
  this phase also emits `VERIFY_OFFER_HUMAN`. The gate still runs and a
  corrected value still verifies; nothing locks, because a lock would be the
  unrecoverable state D11 forbids.
- Otherwise run `verify_identity(memory)`:
  - `verified`: set `verified_party_id`. If role is representative, run the
    representative gate. If it passes, enter `awaiting_consent` and peek the
    consent tool for poll index 0; emit `Effect(consent_advance)` and
    directive `CONSENT_REQUESTED`, or `CONSENT_APPROVED` and move to
    RESOLVE_INTENT if the peek is already approved. If the rep gate fails,
    directive `REP_NOT_FOUND` and stay.
  - `insufficient`: directive `ASK_FOR_PII` with the list of fields still
    needed (chosen from the ones not yet supplied, worded using the record's
    `id_type` if the record is already located).
  - `mismatch`: increment `verify_attempts`, set the mismatched fields to
    None in the next state and add them to `invalidated_fields`. Directive
    `PII_MISMATCH_RETRY`, which tells the responder the details did not match
    and to ask the caller to confirm their verification details again. The
    directive carries no field names, and the responder prompt says not to
    guess which one was wrong. Naming the field would let an attacker confirm
    values one at a time.
- In `awaiting_consent`: peek the consent tool at the current poll index and
  emit `Effect(consent_advance)`. `approved` moves to RESOLVE_INTENT with
  `CONSENT_APPROVED`. `pending` stays with `CONSENT_STILL_PENDING`, which
  tells the responder to say consent has not come through yet and to invite
  the caller to ask for a re-check; polling advances only on user turns, so
  the reply must give the caller a natural next move. Once the
  sequence is exhausted with no approval the peek returns `timeout`; the next
  state records `consent_status = "timeout"`, no further effects are emitted,
  and the directive is `CONSENT_NOT_RECEIVED_OFFER_HUMAN`, repeated on later
  turns. No persuasion directives here.
- If the caller asked a claim question in this phase, add directive
  `DEFER_CLAIM_QUESTION` so the reply acknowledges it was noted for after
  verification. The facts block is empty in this phase by construction.
- The transition to RESOLVE_INTENT happens in the same turn as verification
  succeeds, and `step()` falls through into the RESOLVE_INTENT logic so the
  reply can confirm identity and confirm the remembered claim in one message.

RESOLVE_INTENT

- Run `resolver.resolve(memory, claims_for_party)`.
  - One claim: set `resolved_case_id`, move to PROCESS_CASE, directive
    `CONFIRM_RESOLVED_CASE` with the claim's type, status, and date, and fall
    through into PROCESS_CASE so the first question is answered in the same
    reply if one was asked.
  - Several claims: directive `ASK_WHICH_CLAIM` with the candidate list.
    Listing a verified caller's own claims is allowed here.
  - None: directive `NO_MATCHING_CLAIM` with the list of claims that do exist.
- If no intent has been extracted yet and no hints exist, directive
  `ASK_HOW_CAN_I_HELP`.

PROCESS_CASE

- If `switching_claim`: clear `resolved_case_id`, go to RESOLVE_INTENT.
- If `wants_to_wrap_up`: go to POST_PROCESS, sub_state
  `awaiting_email_decision`, directive `OFFER_EMAIL_SUMMARY`.
- Otherwise build the facts block: the resolved claim record rendered as
  labelled lines, plus `retrieval.select(intent, question_text, claim)`.
  Directive `ANSWER_FROM_FACTS`. If retrieval matched nothing and the
  question is not answerable from the record, add `NOT_COVERED_OFFER_HUMAN`
  and include the guideline fallback text.
- After the first answer, the responder is asked to close with a short
  "anything else" so the caller can trigger wrap-up naturally.

POST_PROCESS

- `email_decision == "yes"`: emit `Effect(email_send)` carrying the record
  address, directive `EMAIL_WILL_BE_SENT` with the address, move to CLOSED.
  The summary is generated and the send is logged in `commit`, after the
  responder has succeeded, so a failed reply never produces a phantom email.
- `email_decision == "no"`: directive `EMAIL_SKIPPED`, move to CLOSED.
- Any in-scope claim question: go back to PROCESS_CASE.
- Otherwise directive `REPEAT_EMAIL_OFFER`.

CLOSED

- `wants_human`: ESCALATED.
- An in-scope claim question from a verified caller: reopen into
  PROCESS_CASE and answer it.
- Anything else: directive `SESSION_CLOSED_INVITE_MORE`, a short note that
  the conversation is wrapped up and they can ask another claim question.

All phases

- Out-of-scope handling runs before the phase logic and only when the turn
  is pure noise: `in_scope == false` and `extraction.has_signal() == false`.
  A message that carries PII plus an off-topic aside, or a claim question
  plus a joke, is treated as in scope and the aside is simply not answered.
  When the ladder fires, the streak increments and picks `DECLINE_OFF_TOPIC`
  on the first rung and `DECLINE_OFF_TOPIC_OFFER_HUMAN` on every rung after;
  phase logic is skipped for that turn. An in-scope turn resets the streak.
- A non-neutral emotion prepends `EMPATHIZE` with the label, so the responder
  acknowledges before it does anything else.
- ESCALATED is terminal. Further messages get a fixed directive
  `SESSION_ENDED` explaining a human has the case.

## 6. Gates

`verify_identity(memory, store) -> VerificationResult`

```
VerificationResult:
  status: "verified" | "insufficient" | "mismatch" | "no_record"
  party_id: str | None
  matched: list[field]
  mismatched: list[field]
  missing: list[field]        (fields not yet supplied, for the ask)
  id_type: str | None         (so the ask can say SSN or national ID)
```

Algorithm: locate the record by policy number if present and matching,
else by normalised name against `name` and `name_aliases`. A policy number
that matches nothing falls back to the name, because the policy number is
not invalidatable and a typo there must not block verification. When the caller role is
representative, the name used for lookup and for the name PII comparison is
`policyholder_name`, not `claimed_name`; the representative's own name is
only checked by the representative gate. If nothing is located,
status is `no_record` and the reply asks for the policy number or full name.
Compare every supplied field against the record with the normalisers.
`verified` requires `len(matched) >= 3` and `len(mismatched) == 0`.
Any mismatch is `mismatch`. Otherwise `insufficient`.

What counts toward the three:

- Full name, date of birth, phone, email, and ID last four. These are the
  five accepted fields.
- The policy number does not count. It is a locator that selects which
  record to compare against, not an authenticator. It appears on every
  letter the insurer has ever mailed and is the easiest field to obtain.
- Full name does count, even when it was also the lookup key. The SOP
  lists it among the five, and once a record is located the name is compared
  against `name` and `name_aliases` like any other field. When the policy
  number is the locator, the name is a genuine independent check. When the
  name is the locator, counting it means the caller still needs two
  non-name fields, which is the customary bar. A wrong name used as locator
  yields `no_record`, which reveals only that no policy exists under that
  name; that is unavoidable for any name-based lookup and is why the agent
  asks for the policy number first.

On `mismatch` the gate returns the mismatched field names to `step()` for
invalidation only. They are never placed in a directive or shown to the
responder.

A supplied value that fails to normalise (for example a day-first date such
as `15/03/1985`, which `norm_date` returns as `None`) is treated as missing,
not mismatched. It does not count as a failed attempt, the field is not
invalidated, and the `ASK_FOR_PII` directive for that field carries a format
hint so the responder can ask for it as month, day, year. Only a value that
parses and then differs from the record is a mismatch.

`verify_representative(memory, store) -> bool` matches normalised rep name,
relationship, and policyholder name against `representatives.json` and
checks the row's `buyer_party_id` equals the verified party.

Normalisers, each with tests: `norm_name` (lowercase, collapse whitespace,
strip punctuation), `norm_date` (ISO, `MM/DD/YYYY`, `March 15 1985`,
`15 March 1985`, `3/15/85` with a century rule), `norm_phone` (digits only,
compare last ten), `norm_email` (lowercase, strip), `norm_last4` (digits
only, exactly four).

## 7. Resolver and retrieval

`resolver.resolve(memory, claims)`: start with all claims for the verified
party. Filter by `case_id` if given (exact match wins outright). Then filter
by `case_type`, then `status`, then `year`, then `month`. "January" with no
year means the most recent January on or before `DEMO_NOW`. Filters are
applied only when the hint is present; the result is whatever survives.

Claim records are not uniform. CLM-7412, CLM-7203, and CLM-7802 have no
`denial_reason`, `documents_needed`, or `appeal_deadline` keys. `store.py`
exposes claims as a dataclass with those three as optional fields, and the
resolver, retrieval, and facts rendering treat a missing value as "not
applicable" rather than an error. A closed claim renders with its status,
dates, and amounts only.

`retrieval.select(intent, question_text, claim) -> FactsBlock`:

- Include every `claim_followup_guidance` entry whose `intent_hints` contains
  the intent, and, if it has `match_any`, at least one keyword appears in the
  lowercased question. Skip entries with `requires_documents` when the claim
  has no `documents_needed`.
- The `missing_required_material_alternatives` entry has no `match_any` and
  lists all five intents, so under the rule above it would fire on every
  question about a claim with missing documents, including a plain status
  check. It is gated with a keyword list held in `retrieval.py`: "don't
  have", "do not have", "can't get", "cannot get", "lost", "alternative",
  "instead", "substitute", "unavailable", "missing", "no longer". The entry
  is included only when one of these appears in the question.
- Fill `{case_id}`, `{documents}` (joined list), and
  `{average_processing_time_after_submission}`.
- When the facts block includes any of `expected_reimbursement_amount`,
  `allowed_max_amount`, `net_pay`, or `net_fee`, append that field's
  description from `claim_schema.json` so the responder can explain what the
  number means instead of reading a label aloud. Amounts stay as the decimal
  strings in the fixture.
- For each document in `documents_needed`, include `document_guidance` and
  `document_alternative_guidance` entries whose key contains the document
  name or vice versa, plus `case_type_guidance` and `default_guidance`.
- If nothing matched and the intent is a follow-up type, include
  `claim_followup_fallback`.

The facts block is a list of `(source_label, text)` pairs. It is rendered
into the responder prompt and shown in the debug panel.

## 8. Prompts

Persona (fixed, cached): a claims support specialist for a fictional insurer,
warm, concise, plain language, never invents facts, never mentions internal
phases or directives by name.

Phase blocks describe what the agent may and may not do in that phase in
prose. The VERIFY_ID block says claim details are unavailable until identity
is confirmed and lists the five accepted fields. The PROCESS_CASE block says
to answer only from the facts provided and to say plainly when something is
not in them.

Directives render as short imperative lines, for example:
"Acknowledge that the caller sounds frustrated before anything else."
"Identity is not yet verified. Ask for two more of: date of birth, phone
number, email, last four of national ID."
"Do not discuss the claim yet. Mention that you have noted their question
about the denied claim and will get to it right after verification."

Both calls go through `client.messages.create` with
`thinking={"type": "adaptive"}` and `output_config={"effort": "low"}`.
Effort is a top-level key of `output_config`, not a field of `thinking`.

The extractor adds `output_config["format"] = {"type": "json_schema",
"schema": <schema>}` where the schema is generated from the `Extraction`
Pydantic model with `extra = "forbid"` so it carries
`additionalProperties: false`. The first text block is parsed with
`json.loads` and validated with `Extraction.model_validate`. The deprecated
`output_format` alias is not used. Its system prompt lists the enums and says
to report only what the user stated.

Email summary: built in `commit` when the `email_send` effect runs. Python
fills a template with three headed sections, what was discussed, claim
status and outcome, next steps, from the resolved claim record, every facts
block shown to the responder during the session, and the directive log
(which records what was asked and what guidance was given). The responder
model is then asked to smooth that draft into readable prose without adding,
removing, or changing any fact. A post-check extracts every date and every
number from the smoothed text and verifies each appears in the facts blocks
or the claim record; if any does not, the Python draft is sent unchanged.
The address comes from the policyholder record.

Models: the extractor runs on `EXTRACTOR_MODEL`, default `claude-sonnet-5`,
and the responder and email summary run on `RESPONDER_MODEL`, default
`claude-opus-5`. Both are env-configurable. Extraction is a bounded
classification task where Sonnet is fast and accurate; the responder is
where tone and judgement show, so it gets the stronger model.

## 9. HTTP API and UI

```
POST /api/session?scenario=default|timeout   -> {session_id, greeting}
POST /api/session/{id}/message {text}         -> {reply, phase, sub_state}
GET  /api/session/{id}/state                  -> full session for the debug panel
GET  /                                        -> static/index.html
GET  /health                                  -> {ok, model}
```

Sessions live in a process-local dict. This is a demo; there is one worker.

`index.html` is one file with inline CSS and JS. Left two-thirds: the chat.
Right third: a debug panel with the current phase and sub-state, the
CaseMemory as a key-value list, the counters, the last extraction, the last
directives, the facts block, and the email log. A scenario selector and a
"new session" button sit at the top. The panel is what lets you see
the SOP working rather than take our word for it.

## 10. Configuration and secrets

`.env` holds `ANTHROPIC_API_KEY` and optionally `EXTRACTOR_MODEL` and
`RESPONDER_MODEL`. `config.py` reads them with python-dotenv. Startup fails
fast with a readable message if the key is missing. `DEMO_NOW = date(2026, 3,
5)`, which is after the newest fixture claim (2026-09-14) and before the
earliest appeal deadline (2026-10-01). Thresholds: `VERIFY_MIN_MATCHES = 3`, `VERIFY_OFFER_HUMAN_AT = 3`,
`OOS_OFFER_HUMAN_AT = 2`, `REFUSAL_OFFER_HUMAN_AT = 2`. There are no
escalate-at thresholds because nothing escalates without `wants_human`.

## 11. Testing

Unit tests, no network, run with `.venv/bin/pytest`:

- `test_normalize.py`: every normaliser, including the alias cases from the
  fixtures and the date formats listed above.
- `test_gates.py`: verified with exactly three fields, insufficient with two,
  mismatch with three right and one wrong, policy number plus two fields is
  insufficient, lookup by name without policy number, national ID caller,
  representative gate pass and fail, attempt counter and lock, out-of-scope
  ladder including the "PII plus aside is not penalised" case, refusal
  ladder ending at a repeated human offer.
- Deadlock regression in `test_fsm.py`: wrong DOB then corrected DOB
  verifies on the second attempt, and the mismatch directive carries no
  field name.
- Effects in `test_fsm.py`: `step()` emits a consent effect without changing
  the poll counter, and a simulated responder failure in `test_agent.py`
  leaves state, counters, and the email log untouched.
- Reopen in `test_fsm.py`: CLOSED plus a claim question lands in
  PROCESS_CASE; CLOSED plus an off-topic message stays CLOSED.
- `test_resolver.py`: the Nadia hint resolves to CLM-7710, "healthcare"
  alone yields two candidates, "dental" yields one, an unknown case ID yields
  none, "January" without year picks 2026 under `DEMO_NOW`.
- `test_retrieval.py`: intent plus keyword selection, placeholder filling,
  document name substring matching, the alternatives entry gated by its
  keyword list, monetary field descriptions attached, fallback inclusion,
  and CLM-7915's "referral letter" falling through to default guidance with
  no document-specific entry. Closed claims with no denial fields render
  without error.
- Email in `test_email.py`: the template renders from a session with and
  without documents needed, and the post-check rejects a smoothed body that
  contains a date or number absent from the facts.
- `test_tools.py`: consent default approves on the second poll, timeout never
  approves and reports timeout after the sequence, email tool logs the send.
- `test_fsm.py`: drives `step()` with hand-built extractions through the
  happy path, the angry-caller path, the representative path in both
  scenarios, the out-of-scope ladder, and the email yes and no branches.
  Asserts phases, directives, and that the facts block is empty in
  VERIFY_ID.
- `test_memory.py`: merge never clears, corrections win, provenance recorded.

Live eval, needs the API key, run with `.venv/bin/python -m eval.run`:
`eval/scenarios.py` holds scripted conversations with assertions on phase,
memory, and gate outcomes after each turn, plus a few string-level checks
that the reply in VERIFY_ID contains none of the claim's denial reason,
document names, or amounts. Wording is never asserted. Each scenario prints
its transcript so the README can quote real output. Scenarios:

1. Nadia happy path: the opening message, denial question, a
   document follow-up, wrap-up, then one more claim question after the
   email offer (asserting POST_PROCESS returns to PROCESS_CASE and the
   offer is repeated), then email yes.
2. Nadia angry caller: the bonus example first, then verification, then
   the same flow.
3. Irene Bauer (PH-4033 / CLM-7915): identity supplied over several turns. Name and
   policy number first, then "why do you need my date of birth?" (asserting
   the agent explains and stays in VERIFY_ID), then the date of birth, then
   the national ID last four after the caller says they have no SSN.
   Denied healthcare claim resolved from "my denied claim", a question
   about the missing referral letter that hits the retrieval fallback,
   email no.
4. Representative, default scenario: Julian Okonkwo for Nadia, PII gate,
   representative gate, one pending turn, approved, then "what's the status
   of her healthcare claim", which matches two claims and must produce
   `ASK_WHICH_CLAIM`, then "the July one", resolving to CLM-7710.
5. Representative, timeout scenario: same opening, five pending turns, the
   plain not-received message, offer of human, caller accepts, ESCALATED.
6. Out-of-scope ladder: three off-topic turns from an unverified caller,
   asserting the session is still in VERIFY_ID after all three and that the
   caller can then verify normally.
7. Typo recovery: Nadia with a wrong date of birth, the no-field-named
   mismatch reply, corrected date, verified.

## 12. Packaging

`Dockerfile` on `python:3.11-slim`, copies `app/`, `data/`, `requirements.txt`,
installs, runs uvicorn on 8000. `docker run -e ANTHROPIC_API_KEY=... -p
8000:8000 <image>`. README covers local run, Docker run, the scenario switch,
the six scripted scenarios with what to type, and a short architecture
summary pointing here.

## 13. Build order

Each phase ends green before the next starts.

1. `config`, `store`, `normalize`, `memory` with tests.
2. `gates`, `resolver`, `retrieval`, `tools` with tests.
3. `fsm` and `prompts` with the FSM test driving hand-built extractions.
4. `llm`, `agent`, `session`, `main`, and the UI. First live conversation.
5. `eval` scenarios against the live API; fix what they expose.
6. Dockerfile, README, final pass through the debug panel on every scenario.

Phases 1 through 3 need no API key and are where most of the correctness
lives. Phase 4 is where the agent starts to feel like a person.
