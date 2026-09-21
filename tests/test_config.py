import json
from datetime import date

import pytest

from app import config


def test_demo_now_is_fixed():
    assert config.DEMO_NOW == date(2026, 9, 18)


def test_demo_now_sits_between_newest_claim_and_earliest_deadline():
    # Guards NOTES D9: every fixture claim must be in the past and every
    # appeal deadline must still be live under DEMO_NOW.
    claims = json.loads((config.DATA_DIR / "claims.json").read_text())
    created = [date.fromisoformat(c["created_at"]) for c in claims]
    deadlines = [
        date.fromisoformat(c["appeal_deadline"])
        for c in claims
        if "appeal_deadline" in c
    ]
    assert max(created) < config.DEMO_NOW
    assert config.DEMO_NOW < min(deadlines)


def test_data_dir_points_at_fixtures():
    assert config.DATA_DIR.is_dir()
    assert (config.DATA_DIR / "policyholders.json").is_file()


def test_model_defaults():
    assert config.EXTRACTOR_MODEL == "claude-sonnet-5"
    assert config.RESPONDER_MODEL == "claude-opus-5"


def test_thresholds():
    assert config.VERIFY_MIN_MATCHES == 3
    assert config.VERIFY_OFFER_HUMAN_AT == 3
    assert config.OOS_OFFER_HUMAN_AT == 2
    assert config.REFUSAL_OFFER_HUMAN_AT == 2
    assert not hasattr(config, "OOS_ESCALATE_AT")
    assert not hasattr(config, "REFUSAL_ESCALATE_AT")


def test_require_api_key_raises_when_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        config.require_api_key()


def test_require_api_key_returns_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert config.require_api_key() == "test-key"
