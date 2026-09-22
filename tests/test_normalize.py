from datetime import date

import pytest

from app.normalize import (
    norm_date,
    norm_email,
    norm_last4,
    norm_name,
    norm_phone,
    norm_policy_number,
)


# names

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Nadia Okonkwo", "nadia okonkwo"),
        ("  NADIA   OKONKWO ", "nadia okonkwo"),
        ("Okonkwo, Nadia", "okonkwo nadia"),
        ("Ann-Marie Kovac", "ann marie kovac"),
        ("O'Brien", "obrien"),
        ("", None),
        (None, None),
    ],
)
def test_norm_name(raw, expected):
    assert norm_name(raw) == expected


# emails

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Nadia@Email.com", "nadia@email.com"),
        ("  a.kovac@example.net ", "a.kovac@example.net"),
        ("not an email", None),
        ("", None),
        (None, None),
    ],
)
def test_norm_email(raw, expected):
    assert norm_email(raw) == expected


# phones

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+14155550182", "4155550182"),
        ("(415) 555-0182", "4155550182"),
        ("415-555-0182", "4155550182"),
        ("415 555 0182", "4155550182"),
        ("1 415 555 0182", "4155550182"),
        ("521-2836", None),
        ("", None),
        (None, None),
    ],
)
def test_norm_phone(raw, expected):
    assert norm_phone(raw) == expected


# last four

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2907", "2907"),
        (" 2907 ", "2907"),
        ("29-07", "2907"),
        ("ending in 2907", "2907"),
        ("29071", None),
        ("447", None),
        ("", None),
        (None, None),
    ],
)
def test_norm_last4(raw, expected):
    assert norm_last4(raw) == expected


# policy numbers

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("POL-3318", "POL3318"),
        ("pol 3318", "POL3318"),
        ("pol3318", "POL3318"),
        ("", None),
        (None, None),
    ],
)
def test_norm_policy_number(raw, expected):
    assert norm_policy_number(raw) == expected


# dates: ISO, US slash (four- and two-digit year), US dash, written month

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1987-06-09", date(1987, 6, 9)),
        ("1987/06/09", date(1987, 6, 9)),
        ("06/09/1987", date(1987, 6, 9)),
        ("6/9/1987", date(1987, 6, 9)),
        ("6/9/87", date(1987, 6, 9)),
        ("6/9/05", date(2005, 6, 9)),
        ("06-09-1987", date(1987, 6, 9)),
        ("June 9, 1987", date(1987, 6, 9)),
        ("June 9 1987", date(1987, 6, 9)),
        ("june 9 1987", date(1987, 6, 9)),
        ("Jun 9 1987", date(1987, 6, 9)),
        ("9 June 1987", date(1987, 6, 9)),
        ("  1987-06-09  ", date(1987, 6, 9)),
        ("2/30/1987", None),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_norm_date(raw, expected):
    assert norm_date(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("6/9/26", date(1926, 6, 9)),    # 2026 would be a newborn
        ("6/9/10", date(1910, 6, 9)),    # 2010 would be a minor
        ("9/28/08", date(1908, 9, 28)),  # turns 18 ten days after DEMO_NOW
        ("9/14/08", date(2008, 9, 14)),  # turned 18 four days before DEMO_NOW
        ("6/9/05", date(2005, 6, 9)),
        ("1/1/00", date(2000, 1, 1)),
        ("6/9/87", date(1987, 6, 9)),
    ],
)
def test_two_digit_year_resolves_to_adult_birth_date(raw, expected):
    # DEMO_NOW is 2026-09-18. A two-digit year is only ever a date of birth,
    # so pick the century that makes the caller at least 18 on that day.
    assert norm_date(raw) == expected
