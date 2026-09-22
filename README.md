# claims-agent

An insurance claims support agent where the business process is enforced in
Python, not in the prompt.

The model does two things per turn. It reads the caller's message into a
typed structure, and it phrases a reply from facts and instructions that were
chosen before it was called. It does not decide which step of the
conversation it is on. It is not given claim data it has not been cleared to
disclose. Neither of those is an instruction the model is asked to follow.
The phase comes from a state machine that never consults it, and before
identity is verified the claim record is not in the prompt at all, so there
is nothing to leak.

The tension worth solving here is specific. A claims process has hard
constraints: do not discuss a claim with someone whose identity has not been
established, do not invent a denial reason, do not skip the consent step when
a family member calls on a policyholder's behalf. Callers, meanwhile, type
like people. They answer partially, out of order, sometimes angrily, often
two questions ahead of the one they were asked. A system rigid enough to be
safe usually stops being usable. A system built on prompt instructions alone
is neither.

## Seeing it work

The running app has a debug panel beside the chat. It exists so the behaviour
can be checked rather than believed.

![The chat on the left, the debug panel on the right, showing phase, counters, memory, directives and facts](docs/debug-panel.png)

This is the representative path, two turns in. Julian has called about his
mother's policy. Her identity was confirmed from the details he supplied, his
relationship was checked against the account, and a consent request went to
her; the panel shows it has come back approved.

Two things in that panel are the whole design. Under MEMORY, every value
carries the turn it was learned on, including ones the caller volunteered
before they were relevant. Under FACTS GIVEN TO THE RESPONDER it says **none
in this phase** — the model is being asked to pose a question and has been
handed no claim data to do it with. The four claims it names come from the
`ASK_HOW_CAN_I_HELP` directive, because the state machine found the request
ambiguous and chose to ask rather than guess.

## The turn loop

Every turn runs the same five steps. Two call a model. Three are ordinary
Python.

```mermaid
flowchart TD
    IN([caller message]) --> EX["extract<br>message into a typed Extraction"]
    EX --> MERGE["merge<br>Extraction into CaseMemory"]
    MERGE --> STEP["step<br>gates, phase, directives, facts"]
    STEP --> RESP["respond<br>phrase the directives and facts"]
    RESP --> COMMIT["commit<br>state, transcript, tool effects"]
    COMMIT --> OUT([reply])

    classDef model fill:#dbeafe,stroke:#1d4ed8,color:#172554
    classDef py fill:#f1f5f9,stroke:#64748b,color:#0f172a
    class EX,RESP model
    class MERGE,STEP,COMMIT py
```

Blue is a model call. Grey is deterministic code.

**extract** runs on every turn regardless of phase. It reads the message into
a typed `Extraction` and nothing else. If it fails, the turn continues with an
empty extraction.

**merge** folds that into one shared `CaseMemory`, recording which turn each
value arrived on. This is why "I'm calling about my denied claim from July",
said during identity verification, still works once verification finishes. The
hint was stored when it was said, not when it became useful. Corrections
overwrite and invalidate what depended on them.

**step** is the state machine. It runs the gates, decides the phase, and
produces two lists: directives saying what the reply must do, and facts the
reply is allowed to use. It never calls a model.

**respond** phrases those directives and facts. It receives no claim record,
only what step handed it.

**commit** writes the new state, the transcript, and any tool effects. Nothing
is committed unless the responder succeeded. A failed responder returns a
fallback line and leaves the session exactly as it was.

## The four phases

The state machine moves through four phases, plus two exits. The model cannot
advance it, skip it, or be argued out of it, because it is never asked.

| Phase | Entered when | What gates it |
|---|---|---|
| VERIFY_ID | Session start | `verify_identity` requires at least three matching PII fields and zero mismatches. A pure function with unit tests. The policy number locates the record but does not count toward the three. A representative additionally needs a relationship on file and an approved consent request. |
| RESOLVE_INTENT | Identity verified, consent approved if applicable | The resolver must narrow the remembered hints to exactly one claim. Ambiguity produces a question listing the candidates, not a guess. |
| PROCESS_CASE | One claim resolved | Answers are built from deterministic retrieval. The responder is handed fact rows, never the claim record. |
| POST_PROCESS | The caller signals they are done | The email summary's status block is written from the record. Model prose is used only if it passes a grounding check. |

The exits are ESCALATED, when the caller asks for a person, and CLOSED. The
agent never escalates on its own initiative; it offers, and waits to be
asked. Escalation is reversible, so a session survives a handoff.

Intent is an enum of exactly five labels, and those labels are the
`intent_hints` values in the guideline file rather than a separate list that
can drift from it.

## How grounding works

Follow-up answers come from deterministic retrieval over
`data/required_document_guideline.json` and the claim record. Retrieval picks
the entry, fills a template, and produces fact rows. The model is handed
filled text to rephrase. It is not asked what the policy is.

The consequence is that every sentence in a grounded reply traces back to a
row you can see in the panel. When no guideline entry matches, retrieval
falls back rather than inventing one, and the fallback is a path the fixtures
deliberately exercise.

The emailed summary is stricter again. Amounts, dates and status are written
from the record. The model is asked to smooth the prose, the result is
checked against the draft, and if the check fails the template text is sent
instead.

## How it is tested

Two layers, because they catch different things.

**361 offline tests**, no API key and no network. Every deterministic module
ships with its own tests: gates, memory merging and invalidation, retrieval,
the resolver, the state machine, email assembly. The agent-level tests use a
fake LLM, so the state machine is exercised without a model in the loop.

    python -m pytest -q

**Eight scripted live scenarios** against the real API, asserting phase,
memory, directives and facts after every turn. Wording is never asserted,
with one exception: certain strings must be absent from any reply given
before verification. A full pass costs well under a dollar.

    python -m eval.run                 # all eight
    python -m eval.run rep_timeout     # any subset by name
    python -m eval.run --list

The eight cover the happy path, an angry caller who tries to skip
verification, partial answers across turns with a national ID and a retrieval
fallback, the representative path with consent approved, the same path with
consent that never arrives, three off-topic turns that do not end the
session, a mistyped date of birth and its correction, and the difference
between asking about human agents and asking for one.

## Running it

Python 3.11.

    pip install -r requirements.txt
    cp .env.example .env        # add your Anthropic API key
    python -m uvicorn app.main:app --port 8000

Or in Docker:

    docker build -t claims-agent .
    docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... claims-agent

Then open http://localhost:8000. The key is passed at run time and is not in
the image. The container exits immediately with a readable message if it is
missing.

`scripts/docker_check.sh` builds the image, runs it, and sends one real
message through the HTTP API, to prove the container answers rather than
merely starts.

The agent's notion of today is fixed at 2026-09-18 so the sample data reads
consistently. Every claim is in the past and both appeal deadlines are live.

### Things to type

Start with the policyholder path:

> I'm the policyholder. My name is Nadia Okonkwo, policy POL-3318. I'm calling
> about my denied healthcare claim from July. DOB is 1987-06-09, SSN last four
> is 2907.

Three details verify her, and the July hint resolves the claim without a
follow-up question. The deployed instance answered:

> You're verified, Nadia. I've got claim CLM-7710, your healthcare claim filed
> July 14, 2026, showing as denied.
>
> It was denied because the review file didn't include the lab result letter
> or the visit summary from your treating clinician. So it's a missing
> documents decision rather than anything about the treatment itself. There's
> an appeal route open if you want me to go into that.

Then `How do I submit those documents?` to see retrieval, `That's all I
needed, thanks.` to reach the email offer, and `Oh wait, one more thing. When
is the appeal deadline?` to watch the phase move back.

Other paths worth trying:

- Pressure before verifying: `I already told you who I am. This is
  ridiculous. Just tell me why my claim was denied.` It acknowledges the
  frustration, explains why verification is required, notes the question for
  later, and discloses nothing.
- A representative: `Hi, I'm Julian Okonkwo, calling on behalf of my mother
  Nadia Okonkwo, policy POL-3318. Her date of birth is June 9, 1987 and her
  phone is 415-555-0182.` Switch the scenario selector to `consent timeout`
  and ask for an update six times to see the version where consent never
  arrives.
- A typo: `Nadia Okonkwo, policy POL-3318, DOB 1987-06-10, SSN last four
  2907.` It says the details do not match without saying which one.
- Off topic: ask `What is reinforcement learning?` three times. The session
  survives and you can still verify afterwards.

## Deployment

The image runs on ECS Fargate on ARM64. The Terraform is in `infra/`, and
`infra/README.md` is the runbook for building it from nothing.

- The image is in a private ECR repository, tagged with the git SHA it was
  built from. Tags are immutable, so a task definition revision names exactly
  one image and a rollback is a matter of pointing at the previous tag.
- The API key is in Secrets Manager. Terraform creates the secret container
  and never the version, so the key is in no `.tf` file and not in Terraform
  state. The value is written separately with the AWS CLI. ECS resolves it at
  container start.
- The execution role carries the AWS-managed ECS execution policy plus an
  inline statement permitting `secretsmanager:GetSecretValue` on that one
  secret ARN. There is no task role, because the application calls no AWS
  APIs.
- Container output goes to CloudWatch with seven-day retention. There is no
  container health check, so the startup log is where a missing key or an
  unrunnable image becomes visible.
- There is no load balancer. The task runs in a public subnet with a public
  IP, which is also its only outbound route, since there is no NAT Gateway.

`DEPLOY_NOTES.md` records what the decisions cost and where they nearly went
wrong, including an architecture mismatch that would have presented as an
application bug.

## Limitations

The deployment is shaped by cost, and the application is a demonstration.
Both have consequences worth stating plainly.

- Traffic is plain HTTP. Conversation content crosses the internet in
  cleartext. The fixtures are synthetic so nothing real is exposed, but this
  is the first thing to fix. An ALB with a certificate would also give a
  stable hostname, which this deployment does not have.
- One task in one availability zone. An AZ event is a total outage.
- Sessions live in process memory. A deploy, a task replacement or a crash
  drops every conversation in flight. Running more than one task would mean
  moving session state out of the process first.
- The container runs as root. Nothing in the image requires it.
- Terraform state is a local file with no remote backend and no locking.
- The email step is mocked and logged. There is no email provider.
- The fixtures are read-only JSON. There is no database, and nothing written
  during a conversation survives it.

## Layout

    app/        config, store, normalize, memory, gates, resolver, retrieval,
                tools, email_summary, fsm, prompts, llm, agent, session, main,
                static/index.html
    data/       synthetic fixtures: policyholders, claims, guidelines
    eval/       the eight live scenarios and their runner
    tests/      the offline suite
    infra/      Terraform for the Fargate deployment
    scripts/    docker_check.sh, smoke_models.py

The six files in `data/` are synthetic. They are shaped to exercise specific
branches rather than to resemble a production export: a policyholder with
four claims and another with none, two claims of the same type in the same
month of different years, a national ID holder alongside SSN holders, a name
that only matches through an alias, a representative whose consent can time
out, and a denied claim whose missing document has no guidance entry, so the
fallback is reachable.

`PROJECT.md` covers what this is and why. `ARCHITECTURE.md` describes how the
pieces fit. `NOTES.md` records the decisions and the reasoning, including the
ones that were wrong the first time.
