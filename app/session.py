"""Process-local sessions. One per browser tab; nothing persists."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.fsm import Effect, SessionState, TurnPlan
from app.memory import Extraction
from app.store import Store
from app.tools import ConsentTool, EmailRecord


@dataclass
class Session:
    id: str
    state: SessionState
    consent: ConsentTool
    transcript: list[dict] = field(default_factory=list)
    email_log: list[EmailRecord] = field(default_factory=list)
    last_extraction: Extraction | None = None
    last_plan: TurnPlan | None = None
    last_effects: tuple[Effect, ...] = ()


class SessionRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._sessions: dict[str, Session] = {}

    def create(self, scenario: str) -> Session:
        consent = ConsentTool(self.store, scenario)  # raises ValueError if unknown
        session = Session(id=uuid.uuid4().hex, state=SessionState(scenario=scenario), consent=consent)
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)
