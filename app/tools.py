"""Simulated external services.

ConsentTool is pure: the outcome of poll N is a function of the scenario and
N, so the FSM can peek at it and the commit step advances the counter
(NOTES D12). EmailTool is the mock provider from CLAUDE.md scope: it logs the
send and returns a record for the session's email log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app import config
from app.store import Store

log = logging.getLogger(__name__)

ConsentStatus = Literal["pending", "approved", "timeout"]


class ConsentTool:
    def __init__(self, store: Store, scenario: str) -> None:
        self.scenario = scenario
        self.sequence: tuple[str, ...] = tuple(store.consent_sequence(scenario))

    def peek(self, polls_done: int) -> ConsentStatus:
        if polls_done < len(self.sequence):
            return self.sequence[polls_done]  # type: ignore[return-value]
        return "approved" if "approved" in self.sequence else "timeout"


@dataclass(frozen=True)
class EmailRecord:
    to: str
    subject: str
    body: str
    sent_on: date
    turn: int


class EmailTool:
    def send(self, to: str, subject: str, body: str, turn: int) -> EmailRecord:
        rec = EmailRecord(to=to, subject=subject, body=body, sent_on=config.DEMO_NOW, turn=turn)
        log.info("mock email sent to=%s subject=%r turn=%d", to, subject, turn)
        return rec
