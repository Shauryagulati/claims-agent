# NOTES.md

Decision log for the SOP-guided insurance claims agent. Each entry records
what was decided, why, and why it holds. Newest entries at the bottom.

## D1. The LLM never owns phase state

What: A pure-Python finite state machine holds the current phase and decides
every transition. The LLM is called twice per turn, once to extract structured
facts from the user's message and once to phrase a reply. Neither call can
change the phase.

Why: The core design problem is being strict where the SOP demands it and
flexible where reasoning is useful. A prompt-enforced rule like "do not
advance until verified" fails under adversarial input, long contexts, and
model drift. A gate that is a Python function with a unit test does not.

Why it holds: The responder in VERIFY_ID is never given claim data. It cannot
leak what it does not have. This is the difference between asking the model
to behave and making misbehaviour structurally impossible. Anyone can read
`gates.py` and `fsm.py` and reason about safety without reading a prompt.

## D2. Extractor runs on every turn, in every phase

What: One extraction call per user turn, regardless of phase, writing into a
single shared CaseMemory. The merge only ever adds or overwrites with a
non-null value; a later message that omits a field never clears it. The one
exception is the verification gate, which may discard a field that failed to
match (see D3).

Why: This is how "I'm calling about my denied healthcare claim from July"
said during verification survives until RESOLVE_INTENT. Cross-phase memory is
a first-class requirement, not an optimisation.

Why it holds: Memory is a data structure, not a prompt trick. The FSM in
RESOLVE_INTENT reads the stored hints and calls the deterministic case
resolver before asking the user anything. If the hints already narrow the
claims to one, the agent confirms rather than asks.

## D3. Identity verification rule

What: A caller is verified when at least three PII fields match the
policyholder record and zero supplied fields mismatch. The five PII fields
are full name, date of birth, phone, email, and ID last four.

The policy number does not count toward the three. It is a locator that
selects which record to compare against, not an authenticator. It is printed
on every letter the insurer has ever mailed and is the easiest field for a
third party to obtain. If no policy number is given, or the one given
matches no record, candidates are found by name; the policy number is not
invalidatable, so a typo there must never block a caller whose PII is right.

Full name does count toward the three, even when it was also the lookup key.
It is one of the five accepted fields. When the policy number is the
locator, the name is a genuine independent check against `name` and
`name_aliases`. When the name is the locator, the record trivially matches
its own name, so counting it means the caller still needs two non-name
fields, which is the customary contact-centre bar. The cost is that a wrong
name yields "no record found", which reveals only that no policy exists
under that name; that is unavoidable for any name-based lookup and is why
the agent asks for the policy number first.

Matching is normalised: names are case and whitespace insensitive and honour
`name_aliases`; dates accept ISO, US slash or dash, and written-month forms,
with two-digit years resolved to an adult birth date; phones compare the
last ten digits; emails are lowercased and honour `email_aliases`; ID last
four compares against `id_last4` whatever the `id_type` label is.

On a mismatch the gate discards the mismatched values from memory and marks
those fields as invalidated. A later message that re-supplies the field
fills it and clears the mark. Without this, the zero-mismatch rule and the
never-clear rule in D2 would combine into a permanent deadlock after one
typo. Matched fields are kept, so a caller who re-enters everything verifies
at once.

After a mismatch the gate does not run again until the caller supplies at
least one identity field. Without this, the retained matched fields would
clear the three-match bar on the very next turn regardless of what the
caller said, so the wrong field would simply vanish instead of being
corrected, and the caller would be told "your details do not match" and then
"you are verified" with nothing in between. The FSM tracks this as
`awaiting_reconfirm`; a turn without identity fields repeats the
confirmation request and costs no attempt.

The agent says the details do not match its records and asks the caller to
confirm their verification details again. It never says which field was
wrong, in the directive or in the reply. Naming the field is a
field-enumeration oracle: an attacker could confirm a date of birth, then a
phone, one at a time.

After three failed attempts the agent offers a human transfer on every
verification turn, but keeps evaluating what the caller supplies: a
corrected value still verifies. Nothing locks, because a lock would be the
unrecoverable state D11 forbids. It does not escalate on its own.

Why: The SOP names exactly five PII fields and a threshold of three. The
zero-mismatch rule closes the hole where a caller supplies three correct
fields plus one wrong one and still passes. The invalidation rule keeps that
strictness from becoming a trap.

Why it holds: The threshold, the mismatch rule, and the invalidation are
constants and one function in `gates.py`, each with tests, including a
regression test for the deadlock. The `id_type` field in the fixtures is why
the agent asks for "the last four of your SSN or national ID" rather than
assuming SSN.

## D4. Representative path: PII gate, representative gate, then consent

What: When a caller identifies as calling on behalf of someone else, the FSM
requires all of the following, in this order, before leaving VERIFY_ID:

1. Policyholder PII gate. The same three-field rule from D3, applied to the
   policyholder's record, using the policyholder's name as the locator. A
   son is expected to know his mother's date of birth and phone. This gate
   runs first because it is what resolves the `party_id` the next gate needs.
2. Representative gate. Rep name, relationship, and policyholder name must
   match a row in `representatives.json`, and that row's `buyer_party_id`
   must equal the party verified in gate 1.
3. Consent gate. The agent requests consent through the consent tool, which
   walks the `status_sequence` of the scenario chosen at session creation.
   "approved" passes the gate.

Consent polling advances only on user turns, because the tool is polled
inside the turn loop and nothing runs between turns. The pending directive
therefore tells the caller that consent is still pending and invites them to
ask for a re-check, so the conversation has a natural next move rather than
a dead stop.

The scenario is chosen with a query parameter when the session is created,
so the timeout path can be triggered deliberately from the UI.

Consent timeout is a tool-failure path, not a persuasion path. When the
sequence is exhausted with no approval, the agent states plainly that consent
was not received, does not retry, does not push, and offers a human transfer.
Emotional de-escalation logic is scoped to the frustrated-caller case and is
not invoked here.

Why: The fixtures give the representative no PII of his own, so he cannot be
verified like a policyholder. The lighter alternative, where a matching name
and relationship plus a consent poll is enough, would let anyone who knows a
family relationship unlock claim details with no PII at all. That contradicts
the spirit of the three-PII rule that applies to direct callers.

Treating timeout as a tool failure rather than a persuasion situation is a
deliberate separation. Persuasion is for a human who is reluctant to comply
with a step they can complete. A consent request that the policyholder never
answered is not something the caller can fix by being persuaded, so pushing
them is both useless and irritating. The correct behaviour is to report the
outcome and hand off.

Why it holds: Three gates map to three questions a real insurer asks: does
this person know the policyholder's protected details, are they a registered
representative of that specific policyholder, and did the policyholder agree.
The consent fixture ships a "timeout" scenario of five pendings precisely so
the failure branch is exercised rather than assumed. Knowing when to stop
persuading is satisfied here without conflating it with emotional handling.

## D5. Out-of-scope ladder

What: The extractor labels every turn as in scope or out of scope for
insurance customer service. The ladder fires only when the turn is pure
noise: out of scope and carrying no useful signal (no identity field, no
case hint, no intent, no decision flag). A message with PII plus an
off-topic aside is treated as in scope and the aside is simply not answered.

A counter tracks consecutive off-topic turns and resets on any in-scope
turn. First rung: polite decline and redirect. Second rung and every rung
after: decline and offer a human representative. The ladder never escalates
on its own; see D11.

Why: Out-of-scope questions get a polite refusal, and a human is offered if
the caller keeps retrying. A counter with fixed thresholds is testable and predictable. The
signal check stops the ladder from punishing a caller who answers the
verification question and adds a joke.

Why it holds: The thresholds are config constants. The ladder is a pure
function over the counter and the extraction, tested without an LLM.

## D6. Emotional handling is a directive, not a phase

What: The extractor labels the caller's emotional state as one of neutral,
frustrated, angry, anxious, confused, or refusing, and flags an explicit
refusal to verify. A non-neutral label adds an "empathise first" directive to
the responder prompt for that turn. It never changes the phase or relaxes a
gate. A refusal counter drives an alternatives ladder: first refusal, explain
why the step exists and offer other ID fields; every refusal after, explain
again and offer a human transfer. The ladder never escalates on its own; see
D11.

Why: The bonus asks the agent to acknowledge, explain, offer alternatives, and
know when to stop, all without bypassing gates. Keeping emotion as prompt
input rather than state keeps the FSM small and keeps the safety argument in
D1 intact.

Why it holds: The hardest case, "I already told you who I am, just
tell me why my claim was denied," is handled by D1 plus D6. The responder is
told to empathise and explain, and it physically has no denial reason to
leak.

## D7. Email summary is templated, then smoothed

What: In POST_PROCESS the agent offers an email summary and requires an
explicit yes or no. The email is sent to the address on the policyholder
record, never to an address typed during the conversation. The send is
mocked: it is written to the session's email log and the server log, and the
UI displays it.

The summary body has five parts: greeting, intro, "What we discussed",
"Claim status and outcome", "Next steps", and a closing. The status block is
written entirely by the template from the claim record, including three
labelled money sentences (amount paid, allowed maximum, expected
reimbursement), and is never shown to the smoothing model. Only the two
prose sections are sent to the LLM, and the final email is reassembled in
Python with the status block verbatim. `check_body` then rejects the
smoothed prose if any of three things is true: a required value (case ID,
status, each needed document, formatted deadline) is missing from the
final email; the prose contains a number or date that is not in the
template; or the prose contains any amount at all. If it is rejected, the
template prose is sent instead.

The third rule exists because a token check is one-directional: "your
reimbursement was 1680.00 USD" passes token grounding, since 1680.00 is the
allowed maximum in the facts, while the actual net pay is 0.00. Amounts are
stated once, with their meaning attached, in the block the model cannot
touch, so an amount in prose can only be invented or misattributed.

Why: Sending claim details to a caller-supplied address is an exfiltration
path. The customer must be able to choose, and yes or no is the choice. Mocking the provider is explicitly in scope per CLAUDE.md.

The earlier rationale, that the summary could be freely generated because
every fact had already been disclosed to a verified caller, addressed
leakage but not fabrication. A hallucinated appeal deadline or reimbursement
figure in a written email is worse than in chat: it is durable, it will be
relied on, and it cannot be corrected by the next turn. Templating the facts
and confining the LLM to prose removes the fabrication surface, and the
post-check catches anything that slips through.

The summary covers the final claim only. Each discussed question is stored
with the case ID it was asked about, and the email effect carries only the
questions for the claim that was current when the caller wrapped up. A
caller who switched claims mid-conversation gets a summary of the last one;
a two-claim summary would need two status blocks and two sets of next steps
and is not worth the complexity for this demo.

Why it holds: The email is the one artefact the caller keeps. It is built
the same way every PROCESS_CASE answer is built: facts from Python, wording
from the model.

## D8. Phase transitions

What: Forward order is fixed: VERIFY_ID, RESOLVE_INTENT, PROCESS_CASE,
POST_PROCESS, CLOSED. Wrapping up is a cross-phase signal in the two
verified phases: from PROCESS_CASE it offers the email summary; from
RESOLVE_INTENT with no claim discussed it closes without one (RESOLVE_INTENT
to POST_PROCESS to CLOSED in one turn). Backward moves allowed: POST_PROCESS
to PROCESS_CASE if the caller asks a new in-scope claim question before
deciding on the email; PROCESS_CASE to RESOLVE_INTENT if the caller switches
to a different claim; CLOSED to PROCESS_CASE, or to RESOLVE_INTENT if no
claim was resolved, when a verified caller asks another claim question after
wrapping up. Any phase may move to ESCALATED, but only on an explicit
request for a human, and ESCALATED can resume the phase it came from (D11).
No path ever re-enters VERIFY_ID from a later phase, and no backward move
happens from VERIFY_ID.

Why: A caller who remembers one more question after the agent starts wrapping
up, or after it has closed, is normal. Refusing to answer would feel like a
phone tree. Verification is monotonic because identity does not become
unverified.

Why it holds: The allowed transitions are an explicit table in `fsm.py`.
Anything not in the table is rejected.

## D9. Fixed demo date

What: `DEMO_NOW` is 2026-09-18 and is the only "today" the agent knows. "My
claim from January" resolves to the most recent January not in the future,
which is January 2026.

Why: The fixtures have a denied healthcare claim from July 2026 and a
closed healthcare claim from July 2025 for the same policyholder. Without
a fixed date the resolver's answer depends on when the demo is run. The date
has to sit after every claim and before every appeal deadline, or the fixtures
stop making sense: a claim created in the future cannot be asked about, and a
deadline already passed removes the appeal path the denied claims exist to
demonstrate. 2026-09-18 puts every claim in the past, keeps both deadlines
(2026-10-01 and 2026-10-29) live so the agent can say "you have until", and
still resolves "January" to 2026.

Why it holds: Deterministic tests need a deterministic clock, and the clock
has to sit after the newest fixture and before the earliest deadline.

## D10. Grounded follow-ups are retrieved, not generated

What: In PROCESS_CASE the FSM builds a facts block from the resolved claim
record plus guideline entries selected by deterministic rules: the entry's
`intent_hints` must contain the current intent, and if the entry has
`match_any` keywords at least one must appear in the lowercased user message.
Placeholders are filled in Python. The responder is instructed to answer
only from the facts block and to say when something is not covered.

Two refinements. The `missing_required_material_alternatives` entry has no
`match_any` and lists all five intents, so the plain rule would attach it to
every question about a claim with missing documents, including a status
check. It is gated behind a keyword list in `retrieval.py` ("don't have",
"can't get", "lost", "alternative", "instead", "substitute", and similar) so
it appears only when the caller is actually asking about substitutes. And
whenever the facts block includes a monetary field, the matching description
from `claim_schema.json` is appended so the responder can explain what
"allowed maximum amount" means rather than reading a label aloud.

Why: The guideline fixture is structured exactly for keyword and intent
retrieval. Building it that way is what the authors expect, and it makes the
"only from grounded claim/tool data" requirement auditable.

Why it holds: Show the facts block in the debug panel. Every sentence in the
reply traces to a line in it.

## D11. Nothing escalates without an explicit request

What: ESCALATED is entered by exactly one condition: the extractor reports
`wants_human`. Every ladder (verification lock, out-of-scope, refusal,
consent timeout) ends at "offer a human transfer", repeated on each later
turn until the caller accepts or moves on. No counter reaching a threshold
ends the session by itself.

Why: The first draft escalated automatically at the third rung of each
ladder. Anyone probing the out-of-scope guard three times would have
lost their session and had to start over, which reads as a bug, not a
safety feature. Offering and waiting for a yes is also how a human agent
behaves: they do not hang up on a customer who asked three odd questions.

ESCALATED is not a dead end either. The handoff in this demo is simulated,
so a single extractor false positive on `wants_human` ("do you have human
agents?" read as a request) would otherwise end the session permanently. A
substantive in-scope message after escalation, one carrying identity, case
hints, a decision, or a question, resumes the phase the caller was in with
its sub-state intact, and the reply notes that the transfer request stays
logged. A bare acknowledgement gets the "a representative has this" reply
and the session stays handed off. The extractor prompt also spells out that
questions about whether humans exist or could help are not requests.
Handoffs are counted in state so the debug panel shows they happened.

Why it holds: There are no `ESCALATE_AT` constants in config. The transition
to ESCALATED has one guard, and it is a boolean from the extraction. The
return path exists because the cost of a false positive is a dead session
and the cost of a false negative is one more turn.

## D12. step() is pure; tool effects run after the responder succeeds

What: `fsm.step()` takes state and returns a proposed next state, a
`TurnPlan`, and a list of `Effect`s. It never calls a tool. Where the plan
needs a tool outcome to phrase the reply, the tool exposes a pure `peek`
computed from state (the consent scenario is a fixed sequence indexed by
poll count, so the next result is known in advance). `agent.commit` runs only
after the responder call succeeds: it swaps in the next state, executes each
effect (consent poll advance, email send and log), and appends the reply.

Why: The first draft called tools inside `step()` and rolled the state back
if the responder failed. That rollback would have un-happened a consent poll
and an email send that had in fact happened. Separating "decide" from "do"
makes the rollback honest: a failed reply leaves no trace.

Why it holds: `step()` has no I/O and is tested by asserting on the returned
effects list. The commit path is tested with a responder that raises.

## D13. API shapes and model split

What: Both LLM calls use `client.messages.create` with
`thinking={"type": "adaptive"}` and `output_config={"effort": "low"}`.
Effort lives at the top level of `output_config`, not inside `thinking`. The
extractor adds `output_config["format"] = {"type": "json_schema", "schema":
...}` generated from the `Extraction` Pydantic model with `extra = "forbid"`,
and validates the returned JSON with `model_validate`. The deprecated
`output_format` alias is not used.

The extractor runs on `EXTRACTOR_MODEL` (default `claude-sonnet-5`) and the
responder and email smoothing run on `RESPONDER_MODEL` (default
`claude-opus-5`). Both are env-configurable.

Why: Extraction is a bounded classification task where a fast model is
accurate and latency matters because it runs before the reply can start. The
responder is where tone, empathy, and judgement show, so it gets the stronger
model. A tight budget can point both at Sonnet with two env vars.

Why it holds: The shapes come from the current API reference, not memory.
The split is a one-line config change in either direction.

## D14. An ambiguous name lookup asks for the policy number

What: When no policy number is supplied and the caller's name matches more
than one policyholder record, `verify_identity` returns `no_record` and the
ask names the policy number. The gate never tries each candidate to see
which one the other fields fit.

Why: Trying candidates would turn the gate into a probe: a caller who knows
a common name and one date of birth could learn which of several accounts
that date belongs to. Asking for the policy number costs the honest caller
one question and gives the dishonest one nothing. The fixtures have unique
names, so this branch is dormant today, but the rule is cheap and the
alternative is a real leak the day a second Nadia Okonkwo is added.

Why it holds: the locator is a pure function that returns one record or
none. Disambiguation is the caller's job, using the least sensitive field
they have.

## D15. The responder model refuses one verification turn; retry on the extractor model

What: In both full live passes, the same turn produced a refusal from the
responder model and nothing else did:

    responder refusal model=claude-opus-5 phase=VERIFY_ID directives=[EMPATHIZE, ASK_FOR_PII] attempt=1; retrying on claude-sonnet-5

The caller had asked "Why do you need my date of birth?" during
verification. The reply instructions were to acknowledge the question and
ask again for a date of birth, phone, or ID digits. `LLM.respond` now
retries once on the extractor model when the responder returns
`stop_reason == "refusal"`, logging the model, phase, and directive kinds
each time, and only then falls back to the fixed apology with nothing
committed. The retry answered well both times ("Fair question. I ask
because I need to confirm it's really you before I can talk about anything
on the account").

Why: The refusal is deterministic on this turn and absent everywhere else,
which points at the safety layer reading "reassure the person, then ask for
their personal identifiers" as social engineering rather than a claims
agent doing its job. That is a reasonable classifier to have and a poor
place to be caught by it, because anyone typing the most natural
clarification question in the flow would have seen "something went wrong on
my side" and concluded the agent was broken. A second model with a
different threshold is the cheapest recovery: no beta header, no server-side
fallback parameter, one extra call only when the first is refused, and every
refusal leaves a log line that names the directives so a pattern is visible
rather than guessed.

Why it holds: the fixed-apology path still exists and still commits
nothing; the retry sits in front of it. The test suite proves the retry
order, the logging content, and the give-up after a second refusal with a
stub client. If the pattern widens to other directives, the log will show
which, and the responder prompt for those directives is the next thing to
change.

## Fixture quirks noticed

- `claim_schema.json` calls this an "insurance audio agent demo" and phones are
  in E.164. The deliverable is text. The turn loop takes text in and returns
  text out, so a voice transport could wrap it without changes.
- `documents_needed` uses "lab result letter" but `document_guidance` is keyed
  by "original lab result letter". Retrieval matches document names by
  substring in both directions.
- CLM-7412, CLM-7203, and CLM-7802 have no `denial_reason`, `documents_needed`,
  or `appeal_deadline` keys at all. Every access to those fields uses
  tolerant lookups; a closed or open claim must render without them.
- PH-4033 / CLM-7915 is a second full test path and is in the eval suite: Irene Bauer
  verifies with a national ID last four rather than SSN, the claim is a
  denied healthcare claim, and its `documents_needed` value "diagnosis
  report" has no matching `document_guidance` or
  `document_alternative_guidance` key, so it exercises the default guidance
  and the retrieval fallback rather than a document-specific entry.
- PH-4045 has a `phone_aliases` entry identical to the primary phone. Harmless.
- Representatives have no PII of their own, which is what forces D4.
- Nadia Okonkwo has four claims spanning three types and three statuses, which
  is what makes the "denied healthcare from January" hint a real
  disambiguation test rather than a formality.

## D-future-1: typed-decision models for the extractor, evaluated not adopted

Decision: keep the extractor on a general LLM with a JSON schema. Revisit if
a dedicated typed-decision model proves out.

Context: TypeSafe's Jev, released September 2026, takes unstructured state
and returns a typed decision in a single pass rather than generating text. It
cannot emit a type error, and it is orders of magnitude cheaper and faster
than a frontier LLM on that shape of task. The extractor in `llm.py` is
exactly that shape: message in, typed `Extraction` out, no prose. `llm.py` is
also the only module in the app that talks to a model, so the blast radius of
swapping it is one file.

Why not now: constrained output guarantees a valid type, not a correct value.
The extractor feeds the verification gate, and the gate's safety depends on
the extracted values being right, not merely well-formed. A model that cannot
produce `dob: 12` instead of a date is not thereby a model that reads the
caller's date of birth correctly. Schema enforcement is not the safety
argument here, so "cannot produce a type error" does not settle the question.

What would settle it: running both extractors over the eval transcripts and
comparing extracted field values against hand-labelled ground truth,
particularly on the turns the gates depend on - partial answers, corrections,
and callers who give a detail in an unusual format. Cost and latency are
already in Jev's favour; accuracy on those turns is the open question.

Flagged as future evaluation. Not a planned swap.
