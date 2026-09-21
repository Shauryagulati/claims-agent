import logging

import pytest

from app import config
from app.tools import ConsentTool, EmailRecord, EmailTool


def test_default_scenario_pending_then_approved(store):
    tool = ConsentTool(store, "default")
    assert tool.sequence == ("pending", "approved")
    assert tool.peek(0) == "pending"
    assert tool.peek(1) == "approved"


def test_approval_is_sticky_after_sequence(store):
    tool = ConsentTool(store, "default")
    assert tool.peek(2) == "approved"
    assert tool.peek(10) == "approved"


def test_timeout_scenario_five_pendings_then_timeout(store):
    tool = ConsentTool(store, "timeout")
    assert [tool.peek(i) for i in range(5)] == ["pending"] * 5
    assert tool.peek(5) == "timeout"
    assert tool.peek(6) == "timeout"


def test_peek_is_pure(store):
    tool = ConsentTool(store, "default")
    assert tool.peek(0) == "pending"
    assert tool.peek(0) == "pending"  # no hidden counter


def test_unknown_scenario_rejected(store):
    with pytest.raises(ValueError):
        ConsentTool(store, "nope")


def test_email_send_returns_record_and_logs(caplog):
    tool = EmailTool()
    with caplog.at_level(logging.INFO, logger="app.tools"):
        rec = tool.send(
            to="nadia@email.com",
            subject="Summary of your claim CLM-7710",
            body="Hello Nadia,\n...",
            turn=7,
        )
    assert isinstance(rec, EmailRecord)
    assert rec.to == "nadia@email.com"
    assert rec.sent_on == config.DEMO_NOW
    assert rec.turn == 7
    assert "nadia@email.com" in caplog.text
    assert "CLM-7710" in caplog.text
    with pytest.raises(AttributeError):
        rec.to = "x"
