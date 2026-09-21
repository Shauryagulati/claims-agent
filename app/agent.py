"""The turn loop: extract, step, respond, commit (ARCHITECTURE section 2).

Nothing is committed unless the responder succeeds. Tool effects run inside
commit. handle_turn is the transport boundary; a voice pipeline could call
it with transcribed text.
"""

from __future__ import annotations

import logging

from app import config
from app.email_summary import (
    assemble,
    build_draft,
    check_body,
    next_steps_for,
    smoothable_text,
)
from app.fsm import Effect, StepResult, apply_effects, step
from app.llm import LLMError, history_messages
from app.memory import Extraction
from app.prompts import render_plan
from app.session import Session
from app.store import Store
from app.tools import EmailTool

log = logging.getLogger(__name__)

GREETING = (
    f"Hi, this is {config.AGENT_NAME} at {config.BRAND_NAME} claims. What can I do for you today? "
    "Before I get into anything on the account I'll need to check a couple of details "
    "with you, so your full name and policy number are a good place to start."
)

FALLBACK_REPLY = (
    "I'm sorry, something went wrong on my side just now. Could you send that again?"
)


class Agent:
    def __init__(self, store: Store, llm) -> None:
        self.store = store
        self.llm = llm
        self.email_tool = EmailTool()

    def handle_turn(self, session: Session, text: str) -> str:
        history = history_messages(session.transcript)
        try:
            ex = self.llm.extract(session.state.phase, history, text)
        except Exception as exc:  # the extractor is best-effort by design
            log.warning("extractor raised, using empty extraction: %s", exc)
            ex = Extraction()

        result = step(session.state, ex, self.store, session.consent)
        plan_text = render_plan(result.plan)
        kinds = ", ".join(d.kind for d in result.plan.directives)
        try:
            reply = self.llm.respond(result.plan.phase, history, text, plan_text, log_context=kinds)
        except LLMError as exc:
            log.error("responder failed, nothing committed: %s", exc)
            return FALLBACK_REPLY

        self.commit(session, text, ex, result, reply)
        return reply

    def commit(self, session: Session, text: str, ex: Extraction, result: StepResult, reply: str) -> None:
        state = apply_effects(result.state, result.effects)
        for effect in result.effects:
            if effect.kind == "email_send":
                self._send_email(session, effect, state.turn)
        session.state = state
        session.transcript.append({"role": "user", "text": text, "turn": state.turn})
        session.transcript.append({"role": "assistant", "text": reply, "turn": state.turn})
        session.last_extraction = ex
        session.last_plan = result.plan
        session.last_effects = result.effects

    def _send_email(self, session: Session, effect: Effect, turn: int) -> None:
        a = effect.args
        claim = self.store.claim_by_id(a["case_id"])
        draft = build_draft(
            recipient_name=a["recipient_name"],
            claim=claim,
            discussed=a["discussed"],
            next_steps=next_steps_for(claim, self.store),
            store=self.store,
        )
        smoothed = None
        try:
            candidate = self.llm.smooth(smoothable_text(draft))
            problems = check_body(draft, candidate)
            if problems:
                log.warning("smoothed email rejected, sending template: %s", problems)
            else:
                smoothed = candidate
        except LLMError as exc:
            log.warning("smoothing failed, sending template: %s", exc)
        body = assemble(draft, smoothed)
        record = self.email_tool.send(to=a["to"], subject=draft.subject, body=body, turn=turn)
        session.email_log.append(record)
