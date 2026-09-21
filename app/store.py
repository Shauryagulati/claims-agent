"""Read-only view of the six JSON fixtures in data/. Loaded once per Store.

Every comparison against a record goes through the normalisers on both sides,
so "ANNMARIE KOVAC" matches the alias "Annmarie Kovac" and "(415) 555-0187" matches the
E.164 phone on file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app import config
from app.normalize import norm_email, norm_name, norm_phone, norm_policy_number

FIXTURE_FILES: tuple[str, ...] = (
    "claim_schema.json",
    "claims.json",
    "consent_scenarios.json",
    "policyholders.json",
    "representatives.json",
    "required_document_guideline.json",
)


@dataclass(frozen=True)
class Policyholder:
    party_id: str
    name: str
    policy_number: str
    dob: date
    id_type: str
    id_last4: str
    phone: str
    email: str
    name_aliases: tuple[str, ...] = ()
    phone_aliases: tuple[str, ...] = ()
    email_aliases: tuple[str, ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.name_aliases)

    @property
    def all_phones(self) -> tuple[str, ...]:
        return (self.phone, *(p for p in self.phone_aliases if p != self.phone))

    @property
    def all_emails(self) -> tuple[str, ...]:
        return (self.email, *(e for e in self.email_aliases if e != self.email))

    def matches_name(self, raw: str | None) -> bool:
        key = norm_name(raw)
        return bool(key) and any(norm_name(n) == key for n in self.all_names)

    def matches_email(self, raw: str | None) -> bool:
        key = norm_email(raw)
        return bool(key) and any(norm_email(e) == key for e in self.all_emails)

    def matches_phone(self, raw: str | None) -> bool:
        key = norm_phone(raw)
        return bool(key) and any(norm_phone(p) == key for p in self.all_phones)


@dataclass(frozen=True)
class Claim:
    case_id: str
    party_id: str
    case_type: str
    created_at: date
    status: str
    summary: str
    expected_reimbursement_amount: str
    allowed_max_amount: str
    net_pay: str
    net_fee: str
    denial_reason: str | None = None
    documents_needed: tuple[str, ...] = ()
    appeal_deadline: date | None = None


@dataclass(frozen=True)
class Representative:
    rep_name: str
    relationship: str
    buyer_name: str
    buyer_party_id: str


def _check_fixtures(data_dir: Path) -> None:
    """Fail at construction, naming what is missing, rather than on first use.
    Catches a stray DATA_DIR env var and a broken Docker COPY."""
    if not data_dir.is_dir():
        raise FileNotFoundError(f"fixture directory does not exist: {data_dir}")
    missing = [name for name in FIXTURE_FILES if not (data_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"fixture files missing from {data_dir}: {', '.join(missing)}"
        )


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _policyholder(row: dict[str, Any]) -> Policyholder:
    return Policyholder(
        party_id=row["party_id"],
        name=row["name"],
        policy_number=row["policy_number"],
        dob=date.fromisoformat(row["dob"]),
        id_type=row["id_type"],
        id_last4=row["id_last4"],
        phone=row["phone"],
        email=row["email"],
        name_aliases=tuple(row.get("name_aliases", [])),
        phone_aliases=tuple(row.get("phone_aliases", [])),
        email_aliases=tuple(row.get("email_aliases", [])),
    )


def _claim(row: dict[str, Any]) -> Claim:
    deadline = row.get("appeal_deadline")
    return Claim(
        case_id=row["case_id"],
        party_id=row["party_id"],
        case_type=row["case_type"],
        created_at=date.fromisoformat(row["created_at"]),
        status=row["status"],
        summary=row["summary"],
        expected_reimbursement_amount=row["expected_reimbursement_amount"],
        allowed_max_amount=row["allowed_max_amount"],
        net_pay=row["net_pay"],
        net_fee=row["net_fee"],
        denial_reason=row.get("denial_reason"),
        documents_needed=tuple(row.get("documents_needed", [])),
        appeal_deadline=date.fromisoformat(deadline) if deadline else None,
    )


def _representative(row: dict[str, Any]) -> Representative:
    return Representative(
        rep_name=row["rep_name"],
        relationship=row["relationship"],
        buyer_name=row["buyer_name"],
        buyer_party_id=row["buyer_party_id"],
    )


class Store:
    def __init__(self, data_dir: Path = config.DATA_DIR) -> None:
        _check_fixtures(data_dir)
        self.policyholders: list[Policyholder] = [
            _policyholder(r) for r in _load(data_dir / "policyholders.json")
        ]
        self.claims: list[Claim] = [_claim(r) for r in _load(data_dir / "claims.json")]
        self.representatives: list[Representative] = [
            _representative(r) for r in _load(data_dir / "representatives.json")
        ]
        self.guideline: dict[str, Any] = _load(data_dir / "required_document_guideline.json")
        self.consent_scenarios: dict[str, Any] = _load(data_dir / "consent_scenarios.json")
        self.claim_schema: dict[str, Any] = _load(data_dir / "claim_schema.json")

        self._by_policy = {
            norm_policy_number(p.policy_number): p for p in self.policyholders
        }
        self._by_party = {p.party_id: p for p in self.policyholders}
        self._claims_by_id = {c.case_id: c for c in self.claims}

    # policyholders

    def policyholder_by_policy_number(self, raw: str | None) -> Policyholder | None:
        key = norm_policy_number(raw)
        return self._by_policy.get(key) if key else None

    def policyholders_by_name(self, raw: str | None) -> list[Policyholder]:
        return [p for p in self.policyholders if p.matches_name(raw)]

    def policyholders_by_email(self, raw: str | None) -> list[Policyholder]:
        return [p for p in self.policyholders if p.matches_email(raw)]

    def policyholder_by_party(self, party_id: str) -> Policyholder | None:
        return self._by_party.get(party_id)

    # claims

    def claims_for_party(self, party_id: str) -> list[Claim]:
        return [c for c in self.claims if c.party_id == party_id]

    def claim_by_id(self, case_id: str | None) -> Claim | None:
        return self._claims_by_id.get(case_id) if case_id else None

    # representatives

    def representative_match(
        self,
        rep_name: str | None,
        relationship: str | None,
        buyer_name: str | None,
    ) -> Representative | None:
        rn, bn = norm_name(rep_name), norm_name(buyer_name)
        rel = (relationship or "").strip().lower()
        if not (rn and bn and rel):
            return None
        for r in self.representatives:
            if (
                norm_name(r.rep_name) == rn
                and r.relationship.lower() == rel
                and norm_name(r.buyer_name) == bn
            ):
                return r
        return None

    # consent and schema

    def consent_sequence(self, scenario: str) -> list[str]:
        try:
            return list(self.consent_scenarios[scenario]["status_sequence"])
        except KeyError as exc:
            raise ValueError(f"unknown consent scenario: {scenario!r}") from exc

    def field_description(self, field: str) -> str | None:
        entry = self.claim_schema.get("field_descriptions", {}).get(field)
        return entry.get("description") if entry else None
