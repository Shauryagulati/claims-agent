"""FastAPI surface. Thin: every route calls the agent or reads a session."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import config
from app.agent import GREETING, Agent
from app.retrieval import render
from app.session import Session, SessionRegistry
from app.store import Store

STATIC = Path(__file__).parent / "static"


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def _state_view(s: Session) -> dict:
    st = s.state
    return {
        "phase": st.phase.value,
        "sub_state": st.sub_state,
        "scenario": st.scenario,
        "turn": st.turn,
        "counters": {
            "verify_attempts": st.verify_attempts,
            "refusals": st.refusals,
            "oos_streak": st.oos_streak,
            "consent_polls": st.consent_polls,
        },
        "consent_status": st.consent_status,
        "awaiting_reconfirm": st.awaiting_reconfirm,
        "memory": st.memory.model_dump(mode="json"),
        "last_extraction": s.last_extraction.model_dump(mode="json") if s.last_extraction else None,
        "last_directives": [{"kind": d.kind, "args": d.args} for d in (s.last_plan.directives if s.last_plan else ())],
        "facts": render(list(s.last_plan.facts)) if s.last_plan else "",
        "effects": [{"kind": e.kind, "args": e.args} for e in s.last_effects],
        "email_log": [
            {"to": r.to, "subject": r.subject, "body": r.body, "sent_on": r.sent_on.isoformat(), "turn": r.turn}
            for r in s.email_log
        ],
        "transcript": list(s.transcript),
    }


def create_app(agent: Agent | None = None, registry: SessionRegistry | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if agent is None or registry is None:
            from app.llm import LLM  # only the real app imports the client

            config.require_api_key()
            store = Store()
            app.state.agent = Agent(store, LLM())
            app.state.registry = SessionRegistry(store)
        else:
            app.state.agent = agent
            app.state.registry = registry
        yield

    app = FastAPI(title=f"{config.BRAND_NAME} claims SOP agent", lifespan=lifespan)

    def _get(session_id: str) -> Session:
        session = app.state.registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "extractor_model": config.EXTRACTOR_MODEL, "responder_model": config.RESPONDER_MODEL}

    @app.get("/")
    def index() -> HTMLResponse:
        # The brand is the one thing the page cannot hardcode, since it is
        # configurable. Substituted on the way out rather than templated.
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{BRAND}}", config.BRAND_NAME))

    @app.post("/api/session")
    def create_session(scenario: Literal["default", "timeout"] = Query("default")) -> dict:
        session = app.state.registry.create(scenario)
        return {"session_id": session.id, "greeting": GREETING, "phase": session.state.phase.value}

    @app.post("/api/session/{session_id}/message")
    def post_message(session_id: str, body: MessageIn) -> dict:
        session = _get(session_id)
        reply = app.state.agent.handle_turn(session, body.text.strip())
        return {
            "reply": reply,
            "phase": session.state.phase.value,
            "sub_state": session.state.sub_state,
            "turn": session.state.turn,
        }

    @app.get("/api/session/{session_id}/state")
    def get_state(session_id: str) -> dict:
        return _state_view(_get(session_id))

    return app


app = create_app()
