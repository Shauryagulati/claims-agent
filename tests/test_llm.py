import json

import pytest

from app.llm import (
    MAX_UNION_PARAMS,
    _normalize,
    extraction_schema,
    history_messages,
    parse_extraction,
    string_fields_sent_as_plain,
)
from app.memory import INTENTS, Extraction


def _nodes(node):
    """Yield every dict node in a schema."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _nodes(v)
    elif isinstance(node, list):
        for v in node:
            yield from _nodes(v)


def test_extraction_schema_top_level_shape():
    s = extraction_schema()
    assert s["type"] == "object"
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    assert "claimed_name" in s["properties"]
    json.dumps(s)


def test_extraction_schema_has_no_title_default_or_type_lists():
    for node in _nodes(extraction_schema()):
        assert "title" not in node
        assert "default" not in node
        assert not isinstance(node.get("type"), list), node


def test_every_enum_has_a_single_string_type_and_no_null_member():
    # This is the rule the API enforced in the smoke test: an enum's declared
    # type must be a single type, and null is expressed as an anyOf branch.
    for node in _nodes(extraction_schema()):
        if "enum" in node:
            assert node.get("type") == "string", node
            assert None not in node["enum"], node


def test_nullable_enum_is_anyof_string_enum_or_null():
    intent = extraction_schema()["properties"]["intent"]
    assert intent == {
        "anyOf": [
            {"type": "string", "enum": list(INTENTS)},
            {"type": "null"},
        ]
    }


def test_union_parameter_count_is_within_api_limit():
    # The API rejected 18 union-typed parameters (limit 16). Nullable strings
    # are sent as plain strings, leaving only enums and ints as unions.
    props = extraction_schema()["properties"]
    unions = [n for n, p in props.items() if "anyOf" in p or isinstance(p.get("type"), list)]
    assert len(unions) <= MAX_UNION_PARAMS
    assert set(unions) == {"case_type", "status", "month", "year", "intent", "email_decision"}


def test_nullable_strings_are_sent_as_plain_strings():
    plain = set(string_fields_sent_as_plain())
    assert plain == {
        "claimed_name", "dob", "phone", "email", "id_last4", "policy_number",
        "rep_name", "rep_relationship", "policyholder_name",
        "case_id", "case_hint_text", "question_text",
    }
    for name in plain:
        assert "Empty string" in extraction_schema()["properties"][name]["description"]


def test_parse_extraction_maps_empty_strings_to_none():
    ex = parse_extraction(json.dumps({"claimed_name": "", "dob": "  ", "phone": "4155550182", "question_text": ""}))
    assert ex.claimed_name is None
    assert ex.dob is None
    assert ex.phone == "4155550182"
    assert ex.question_text is None
    assert ex.has_signal() is True


def test_non_nullable_enum_stays_plain():
    emotion = extraction_schema()["properties"]["emotion"]
    assert emotion["type"] == "string"
    assert set(emotion["enum"]) == {"neutral", "frustrated", "angry", "anxious", "confused", "refusing"}


def test_normalize_rewrites_type_list_with_enum():
    raw = {"type": ["string", "null"], "enum": ["a", "b", None], "title": "X", "default": None}
    assert _normalize(raw) == {"anyOf": [{"type": "string", "enum": ["a", "b"]}, {"type": "null"}]}


def test_normalize_rewrites_nested_type_lists():
    raw = {"properties": {"month": {"type": ["integer", "null"], "title": "Month"}}}
    assert _normalize(raw) == {"properties": {"month": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}


def test_parse_extraction_valid():
    ex = parse_extraction(json.dumps({"claimed_name": "Nadia Okonkwo", "intent": "denial_question", "in_scope": True}))
    assert ex.claimed_name == "Nadia Okonkwo"
    assert ex.intent == "denial_question"


def test_parse_extraction_bad_json_is_empty(caplog):
    assert parse_extraction("not json") == Extraction()
    assert "extraction" in caplog.text.lower()


def test_parse_extraction_bad_enum_is_empty():
    assert parse_extraction(json.dumps({"intent": "chit_chat"})) == Extraction()


def test_parse_extraction_unknown_field_is_empty():
    assert parse_extraction(json.dumps({"surprise": 1})) == Extraction()


def test_history_messages_bounded_and_roles_only():
    transcript = [{"role": "user", "text": f"u{i}", "turn": i} for i in range(30)]
    transcript = [t if i % 2 == 0 else {**t, "role": "assistant"} for i, t in enumerate(transcript)]
    msgs = history_messages(transcript, limit=6)
    assert len(msgs) == 6
    assert msgs[0] == {"role": "user", "content": "u24"}
    assert all(set(m) == {"role", "content"} for m in msgs)
    assert history_messages([], limit=6) == []


from types import SimpleNamespace

from app import config
from app.fsm import Phase
from app.llm import LLM, LLMError, Usage


class _StubMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _resp(text, stop="end_turn", inp=100, out=20):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
        model="stub",
    )


def _llm(*responses):
    client = SimpleNamespace(messages=_StubMessages(responses))
    return LLM(client=client), client.messages


def test_extract_sends_schema_and_parses(monkeypatch):
    llm, msgs = _llm(_resp('{"claimed_name": "Nadia Okonkwo", "intent": "denial_question", "dob": ""}'))
    ex = llm.extract(Phase.VERIFY_ID, [], "hi")
    assert ex.claimed_name == "Nadia Okonkwo" and ex.dob is None
    kw = msgs.calls[0]
    assert kw["model"] == config.EXTRACTOR_MODEL
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["messages"][-1] == {"role": "user", "content": "hi"}


def test_extract_failure_modes_yield_empty():
    import anthropic
    import httpx2
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    llm, _ = _llm(err)
    assert llm.extract(Phase.VERIFY_ID, [], "x") == Extraction()
    llm, _ = _llm(_resp("{}", stop="max_tokens"))
    assert llm.extract(Phase.VERIFY_ID, [], "x") == Extraction()


def test_respond_builds_content_and_raises_on_refusal_or_empty():
    llm, msgs = _llm(_resp("Hello Nadia."))
    out = llm.respond(Phase.PROCESS_CASE, [{"role": "user", "content": "a"}], "hi", "PLAN")
    assert out == "Hello Nadia."
    kw = msgs.calls[0]
    assert kw["model"] == config.RESPONDER_MODEL
    assert kw["messages"][-1]["content"] == "Customer says: hi\n\nPLAN"
    assert kw["messages"][0] == {"role": "user", "content": "a"}
    llm, _ = _llm(_resp("x", stop="refusal"), _resp("x", stop="refusal"))
    with pytest.raises(LLMError):
        llm.respond(Phase.VERIFY_ID, [], "hi", "PLAN")
    llm, _ = _llm(_resp("   "))
    with pytest.raises(LLMError):
        llm.respond(Phase.VERIFY_ID, [], "hi", "PLAN")


def test_respond_retries_once_on_refusal_and_logs_context(caplog):
    import logging
    llm, msgs = _llm(_resp("x", stop="refusal"), _resp("Recovered reply."))
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        out = llm.respond(Phase.VERIFY_ID, [], "why do you need my DOB?", "PLAN", log_context="ASK_FOR_PII")
    assert out == "Recovered reply."
    assert msgs.calls[0]["model"] == config.RESPONDER_MODEL
    assert msgs.calls[1]["model"] == config.EXTRACTOR_MODEL
    assert "refusal" in caplog.text and config.RESPONDER_MODEL in caplog.text
    assert "VERIFY_ID" in caplog.text and "ASK_FOR_PII" in caplog.text
    assert llm.usage[config.RESPONDER_MODEL].calls == 1
    assert llm.usage[config.EXTRACTOR_MODEL].calls == 1


def test_respond_gives_up_after_second_refusal(caplog):
    import logging
    llm, msgs = _llm(_resp("x", stop="refusal"), _resp("y", stop="refusal"))
    with caplog.at_level(logging.WARNING, logger="app.llm"), pytest.raises(LLMError, match="twice"):
        llm.respond(Phase.VERIFY_ID, [], "hi", "PLAN")
    assert len(msgs.calls) == 2
    assert "giving up" in caplog.text


def test_smooth_returns_text_or_raises():
    llm, _ = _llm(_resp("smoothed"))
    assert llm.smooth("draft") == "smoothed"
    llm, _ = _llm(_resp("", stop="end_turn"))
    with pytest.raises(LLMError):
        llm.smooth("draft")


def test_usage_accumulates_per_model_and_resets():
    llm, _ = _llm(_resp("{}", inp=1000, out=100), _resp("reply", inp=2000, out=300))
    llm.extract(Phase.VERIFY_ID, [], "x")
    llm.respond(Phase.VERIFY_ID, [], "x", "p")
    ext = llm.usage[config.EXTRACTOR_MODEL]
    rsp = llm.usage[config.RESPONDER_MODEL]
    assert (ext.calls, ext.input_tokens, ext.output_tokens) == (1, 1000, 100)
    assert (rsp.calls, rsp.input_tokens, rsp.output_tokens) == (1, 2000, 300)
    assert llm.total_cost_usd() > 0
    llm.reset_usage()
    assert llm.usage == {}


def test_usage_cost_uses_price_table():
    u = Usage(calls=1, input_tokens=1_000_000, output_tokens=1_000_000)
    assert u.cost_usd("claude-sonnet-5") == pytest.approx(12.0)
    assert u.cost_usd("claude-opus-5") == pytest.approx(30.0)
    assert u.cost_usd("something-else") == pytest.approx(30.0)  # falls back to Opus
