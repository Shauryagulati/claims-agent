"""Pure string-to-canonical helpers. Every function returns None on bad input
and never raises."""

from __future__ import annotations

import re
from datetime import date, datetime

from app import config

_WS = re.compile(r"\s+")
_NON_ALNUM_SPACE = re.compile(r"[^a-z0-9 ]")
_NON_DIGIT = re.compile(r"\D")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ISO, US slash (four- and two-digit year), US dash, written month.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m/%d/%y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
)


def norm_name(s: str | None) -> str | None:
    """Lowercase, drop punctuation, collapse whitespace.

    Handles typed variation only: case, commas, hyphens, apostrophes, extra
    spaces. Spelling variants such as "Annmarie Kovac" for "Ann Marie Kovac" are not a
    normalisation problem; the store matches those through name_aliases.
    """
    if not s:
        return None
    t = s.lower().replace("-", " ").replace(",", " ")
    t = _NON_ALNUM_SPACE.sub("", t)
    t = _WS.sub(" ", t).strip()
    return t or None


def norm_email(s: str | None) -> str | None:
    if not s:
        return None
    t = s.strip().lower()
    return t if _EMAIL.match(t) else None


def norm_phone(s: str | None) -> str | None:
    """Last ten digits, so E.164, US-formatted, and bare forms compare equal."""
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    return digits[-10:] if len(digits) >= 10 else None


def norm_last4(s: str | None) -> str | None:
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    return digits if len(digits) == 4 else None


def norm_policy_number(s: str | None) -> str | None:
    if not s:
        return None
    t = _NON_ALNUM.sub("", s.upper())
    return t or None


def norm_date(s: str | None) -> date | None:
    if not s:
        return None
    t = _WS.sub(" ", s.replace(",", " ")).strip()
    if not t:
        return None
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(t, fmt).date()
        except ValueError:
            continue
        if fmt == "%m/%d/%y":
            d = _adult_century(d)
        return d
    return None


def _adult_century(d: date) -> date:
    """Resolve a two-digit year so the date is a plausible adult's birth date.

    Two-digit years only reach this module as dates of birth, and strptime's
    own pivot (1969) would turn "10" into 2010, a minor. Prefer 20xx when that
    makes the person at least 18 as of DEMO_NOW, otherwise use 19xx.
    """
    yy = d.year % 100
    now = config.DEMO_NOW
    age_if_2000s = now.year - (2000 + yy)
    is_adult_in_2000s = age_if_2000s > 18 or (
        age_if_2000s == 18 and (now.month, now.day) >= (d.month, d.day)
    )
    return d.replace(year=(2000 + yy) if is_adult_in_2000s else (1900 + yy))
