"""Pick one claim from a verified party's claims using the hints in memory.

Only ever called with the verified party's own claims, so it cannot select
another customer's record no matter what the hints say.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app import config
from app.memory import CaseMemory
from app.normalize import norm_policy_number
from app.store import Claim

ResolutionStatus = Literal["resolved", "ambiguous", "none", "no_hints"]


@dataclass(frozen=True)
class Resolution:
    status: ResolutionStatus
    claim: Claim | None
    candidates: tuple[Claim, ...]


def year_for_month(month: int) -> int:
    """'July' with no year means the most recent July on or before
    DEMO_NOW (NOTES D9)."""
    now = config.DEMO_NOW
    return now.year if month <= now.month else now.year - 1


def _has_hints(memory: CaseMemory) -> bool:
    return any(
        getattr(memory, f) is not None
        for f in ("case_id", "case_type", "status", "month", "year")
    )


def resolve(memory: CaseMemory, claims: list[Claim]) -> Resolution:
    everything = tuple(claims)
    if not _has_hints(memory):
        return Resolution("no_hints", None, everything)

    if memory.case_id:
        key = norm_policy_number(memory.case_id)  # same shape: letters, dash, digits
        for c in claims:
            if norm_policy_number(c.case_id) == key:
                return Resolution("resolved", c, (c,))
        return Resolution("none", None, everything)

    survivors = list(claims)
    if memory.case_type:
        survivors = [c for c in survivors if c.case_type == memory.case_type]
    if memory.status:
        survivors = [c for c in survivors if c.status == memory.status]
    if memory.year is not None:
        survivors = [c for c in survivors if c.created_at.year == memory.year]
    if memory.month is not None:
        year = memory.year if memory.year is not None else year_for_month(memory.month)
        survivors = [
            c for c in survivors
            if c.created_at.month == memory.month and c.created_at.year == year
        ]

    if len(survivors) == 1:
        return Resolution("resolved", survivors[0], (survivors[0],))
    if survivors:
        return Resolution("ambiguous", None, tuple(survivors))
    return Resolution("none", None, everything)
