# claims-agent

An SOP-guided insurance claims support agent. A fixed four-phase workflow,
VERIFY_ID, RESOLVE_INTENT, PROCESS_CASE, POST_PROCESS, is enforced by a pure
Python state machine. The language model is used for exactly two things per
turn: reading the caller's message into a typed structure, and phrasing a
reply from facts and instructions the state machine chose. The model never
sees claim data before verification and never decides the phase.

What it is and why it is built this way: `PROJECT.md`. Design:
`ARCHITECTURE.md`. Decisions and their reasoning: `NOTES.md`.

## Setup

Python 3.11.

    pip install -r requirements.txt
    cp .env.example .env        # then put your Anthropic API key in .env

`requirements.txt` is hand-maintained. Do not regenerate it with `pip freeze`.

The tests need no key and no network:

    python -m pytest -q

## Run locally

    python -m uvicorn app.main:app --port 8000

Open http://localhost:8000. The left side is the chat. The right side is a
debug panel showing the current phase, what the agent remembers and when it
learned it, the instructions its last reply was built from, and the facts it
was allowed to use. That panel is how you can see the SOP working rather
than take the reply's word for it.

The scenario selector switches the simulated consent service between
`default` (approves on the second check) and `consent timeout` (never
approves). It only matters for the representative path below.

Models default to `claude-sonnet-5` for extraction and `claude-opus-5` for
replies. Override with `EXTRACTOR_MODEL` and `RESPONDER_MODEL` in `.env`.
The insurer name is configurable too, via `BRAND_NAME`; nothing in the logic
depends on it. The agent's notion of today is fixed at 2026-09-18 so the
sample data reads consistently: every claim is in the past and both appeal
deadlines are live.

## Docker

    docker build -t claims-agent .
    docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... claims-agent

Then open http://localhost:8000. The key is passed at run time and is not
in the image; the container fails fast at startup with a readable message
if it is missing. Optional: `-e RESPONDER_MODEL=claude-sonnet-5` to run
cheaper.

`scripts/docker_check.sh` builds the image, runs it, and sends the opening
message through the HTTP API to prove the container answers, not just that
it starts.

## Deployment

The image runs on ECS Fargate in us-east-1, on ARM64. The Terraform that
builds the environment is in `infra/`, and `infra/README.md` is the runbook
for bringing it up from nothing. `DEPLOY_NOTES.md` records the decisions and
the near-misses.

- The image lives in a private ECR repository, tagged with the git SHA it was
  built from. Tags are immutable, so a task definition revision names exactly
  one image, and a rollback is a matter of pointing at the previous tag.
- The Anthropic API key is held in Secrets Manager. Terraform creates the
  secret container and never the version, so the key is in no `.tf` file and
  not in Terraform state. The value is written once with
  `aws secretsmanager put-secret-value`. ECS resolves it at container start
  and injects it as `ANTHROPIC_API_KEY`.
- The task execution role carries the AWS-managed ECS execution policy plus
  an inline statement permitting `secretsmanager:GetSecretValue` on that one
  secret ARN. There is no task role, because the application calls no AWS
  APIs.
- Container output goes to a CloudWatch log group with seven-day retention.
  There is no container health check, so the startup log is the only place a
  missing key or an unrunnable image becomes visible.
- There is no load balancer. The task runs in a public subnet with a public
  IP, which is also its only outbound route to ECR, Secrets Manager and the
  model API, since the deployment has no NAT Gateway.

### Limitations

The shape of this deployment is a deliberate trade against cost. What the
trade actually costs:

- Traffic is plain HTTP. Conversation content crosses the internet in
  cleartext. The fixtures are synthetic so nothing real is exposed, but this
  is the first thing to fix. An ALB with an ACM certificate would also give
  a stable hostname, which this deployment does not have: the endpoint is
  the task's public IP and it changes whenever the task is replaced.
- One task in one availability zone. An AZ event is a total outage.
- Sessions live in process memory. A deploy, a task replacement or a crash
  drops every conversation in flight. Running more than one task would
  require moving session state out of the process first.
- The container runs as root. Nothing in the image requires it.
- Terraform state is a local file with no remote backend and no locking.
  Safe for one operator, wrong for two.

## Try the main path by hand

Type these in order. The right-hand panel shows the phase after each one.

1. `I'm the policyholder. My name is Nadia Okonkwo, policy POL-3318. I'm calling about my denied healthcare claim from July. DOB is 1987-06-09, SSN last four is 2907.`

   Three matching details verify her; the policy number locates the record
   but does not count toward the three. The remembered hint resolves the
   July 2026 denied healthcare claim without asking, and the reply gives
   the denial reason from the record in the same breath. Phase:
   PROCESS_CASE.

   The reply from the deployed instance, 2026-09-22:

   > You're verified, Nadia. I've got claim CLM-7710, your healthcare claim
   > filed July 14, 2026, showing as denied.
   >
   > It was denied because the review file didn't include the lab result
   > letter or the visit summary from your treating clinician. So it's a
   > missing documents decision rather than anything about the treatment
   > itself. There's an appeal route open if you want me to go into that.

2. `How do I submit those documents?`

   Answered from the guideline data. The panel's facts box shows the
   guideline entry that was retrieved and the document requirements.

3. `That's all I needed, thanks.`

   The agent offers to email a summary to the masked address on file.
   Phase: POST_PROCESS.

4. `Oh wait, one more thing. When is the appeal deadline?`

   Back to PROCESS_CASE; the deadline is stated with days remaining.

5. `Okay, that's everything.` then `Yes please, send it.`

   The email is "sent": the panel's email log shows the full body. The
   status block, including every amount, is written from the record; the
   prose is model-smoothed only if it passes a grounding check, otherwise
   the template text is sent.

## Other things to try

- Pressure before verifying: `I already told you who I am. This is
  ridiculous. Just tell me why my claim was denied.` The agent acknowledges,
  explains why verification is required, offers the accepted details, notes
  the question for after verification, and discloses nothing. It never
  escalates on its own; say `connect me to a person` to hand off.
- A representative: `Hi, I'm Julian Okonkwo, calling on behalf of my mother
  Nadia Okonkwo, policy POL-3318. Her date of birth is June 9, 1987 and
  her phone is 415-555-0182.` Her details verify, his relationship is
  checked against the account, and a consent request goes to her. Then
  `Can you check again?` With the `consent timeout` scenario selected, ask
  for an update six times to see the plain not-received message and the
  human offer, with no persuasion.
- Off topic: ask `What is reinforcement learning?` three times. The session
  survives, a human is offered from the second time, and you can still
  verify afterwards.
- A typo: `Nadia Okonkwo, policy POL-3318, DOB 1987-06-10, SSN last four
  2907.` The agent says the details do not match without saying which, and
  `Sorry, my date of birth is 1987-06-09.` verifies.
- Irene Bauer (`POL-7194`, born 1961-04-22, national ID ending 8450) is a
  second complete path with a different ID type and a missing document that
  has no specific guidance on file.
- `Do you have human agents?` is answered as a question. `Please transfer
  me to a person` is the request.

## Scripted live eval

Eight scripted conversations with assertions on phase, memory, directives,
and facts after every turn. Wording is never asserted, except that certain
strings must be absent from replies given before verification. Needs the
key. A full pass costs well under a dollar.

    python -m eval.run                 # all eight
    python -m eval.run rep_timeout     # any subset by name
    python -m eval.run --list

A failing scenario does not stop the run. Token usage and an estimated cost
are printed per scenario, and a transcript per scenario is written to
`eval/transcripts/` as plain dialogue with a one-line state annotation after
each reply.

## Layout

    app/        the agent: config, store, normalize, memory, gates, resolver,
                retrieval, tools, email_summary, fsm, prompts, llm, agent,
                session, main, static/index.html
    data/       synthetic fixtures: policyholders, claims, guidelines
    eval/       scripted live scenarios and the runner
    tests/      pytest suite, no network
    scripts/    docker_check.sh, smoke_models.py

## The fixtures and what they drive

All six files in `data/` are synthetic. No real person, policy, or claim is
represented. They are shaped to exercise specific branches rather than to
look like a production export.

| File | Used by | For |
|---|---|---|
| `policyholders.json` | `store`, `gates` | Identity verification: five PII fields, aliases, and `id_type` (SSN vs national ID) |
| `claims.json` | `store`, `resolver`, `retrieval`, `email_summary` | Claim lookup, disambiguation by type, status, and date, the facts block, the email status block |
| `required_document_guideline.json` | `retrieval` | Grounded follow-up answers: intent and keyword rules, document guidance, alternatives, fallback |
| `representatives.json` | `store`, `gates`, `fsm` | The representative path: who may call on whose behalf |
| `consent_scenarios.json` | `tools`, `fsm` | The simulated consent service, including the timeout branch |
| `claim_schema.json` | `store`, `retrieval` | Descriptions attached to money fields so amounts are explained, not just read |

The edge cases the fixtures deliberately carry: a policyholder with four
claims and another with none; two claims of the same type in the same month
of different years, so month-only hints have to be disambiguated; a national
ID holder alongside SSN holders; a name and email that only match through
aliases; a representative with a consent step that can time out; and a
denied claim whose missing document has no specific guidance entry, so the
fallback path is reachable.
