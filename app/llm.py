"""The only module that talks to the model. Everything else takes an LLM
instance so tests can pass a fake.

Both calls use client.messages.create with adaptive thinking and low effort.
The extractor asks for JSON matching the Extraction schema; the responder
returns prose. Neither call can change the phase (NOTES D1, D13).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import ValidationError

from app import config
from app.fsm import Phase
from app.memory import Extraction
from app.prompts import extractor_system, responder_system

log = logging.getLogger(__name__)

_COMMON: dict[str, Any] = {"thinking": {"type": "adaptive"}}

SMOOTH_SYSTEM = """You tidy the wording of a short customer email. Rewrite the text you are given so it reads warmly and clearly in plain prose. Keep every heading line exactly as it is. Do not add, remove, or change any fact, number, date, name, or amount. Do not add new sentences with information that is not already there. Return only the rewritten text."""


class LLMError(Exception):
    pass


PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, resp_usage: Any) -> None:
        self.calls += 1
        self.input_tokens += int(getattr(resp_usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(resp_usage, "output_tokens", 0) or 0)

    def cost_usd(self, model: str) -> float:
        inp, out = PRICES_PER_MTOK.get(model, PRICES_PER_MTOK["claude-opus-5"])
        return (self.input_tokens * inp + self.output_tokens * out) / 1_000_000


def _normalize(node: Any) -> Any:
    """Make a pydantic-generated schema acceptable to the API's json_schema
    output format.

    Drops "title" and "default". Rewrites any node whose "type" is a list
    containing "null" (for example {"type": ["string", "null"], "enum": [...]})
    into {"anyOf": [{"type": "string", "enum": [...]}, {"type": "null"}]},
    because the API rejects an enum whose declared type is a list. Applied
    recursively, so it also covers nested anyOf branches.
    """
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    if not isinstance(node, dict):
        return node
    node = {k: _normalize(v) for k, v in node.items() if k not in ("title", "default")}
    types = node.get("type")
    if isinstance(types, list):
        rest = {k: v for k, v in node.items() if k != "type"}
        branches = []
        for t in types:
            if t == "null":
                branches.append({"type": "null"})
            else:
                branch = {"type": t, **rest}
                if "enum" in branch:
                    branch["enum"] = [e for e in branch["enum"] if e is not None]
                branches.append(branch)
        return {"anyOf": branches}
    if "enum" in node and any(e is None for e in node["enum"]):
        node["enum"] = [e for e in node["enum"] if e is not None]
    return node


_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
MAX_UNION_PARAMS = 16  # API limit on union-typed top-level parameters


def extraction_schema() -> dict[str, Any]:
    """Schema sent to the API. Nullable strings are sent as plain strings with
    "" meaning "not stated", because the API allows at most 16 union-typed
    parameters and Extraction has 18 nullable fields. parse_extraction maps
    "" back to None, so the Pydantic contract is unchanged."""
    schema = _normalize(Extraction.model_json_schema())
    for name, prop in schema["properties"].items():
        if prop == _NULLABLE_STRING:
            schema["properties"][name] = {
                "type": "string",
                "description": "Empty string when the user did not state it.",
            }
    schema["required"] = sorted(schema["properties"])
    schema["additionalProperties"] = False
    return schema


def string_fields_sent_as_plain() -> list[str]:
    return [n for n, p in extraction_schema()["properties"].items() if p.get("type") == "string" and "enum" not in p]


def parse_extraction(text: str) -> Extraction:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = {k: (None if isinstance(v, str) and v.strip() == "" else v) for k, v in data.items()}
        return Extraction.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        log.warning("extraction did not validate, using empty extraction: %s", exc)
        return Extraction()


def history_messages(transcript: list[dict], limit: int = 20) -> list[dict]:
    recent = [t for t in transcript if t.get("role") in ("user", "assistant")][-limit:]
    return [{"role": t["role"], "content": t["text"]} for t in recent]


def _text_of(resp: anthropic.types.Message) -> str:
    return "".join(b.text for b in resp.content if b.type == "text").strip()


class LLM:
    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self.client = client or anthropic.Anthropic()
        self.usage: dict[str, Usage] = {}

    def _track(self, model: str, resp: Any) -> None:
        self.usage.setdefault(model, Usage()).add(getattr(resp, "usage", None))

    def reset_usage(self) -> None:
        self.usage = {}

    def total_cost_usd(self) -> float:
        return sum(u.cost_usd(m) for m, u in self.usage.items())

    def extract(self, phase: Phase, history: list[dict], text: str) -> Extraction:
        try:
            resp = self.client.messages.create(
                model=config.EXTRACTOR_MODEL,
                max_tokens=1024,
                system=extractor_system(phase),
                messages=history + [{"role": "user", "content": text}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": extraction_schema()},
                },
                **_COMMON,
            )
        except anthropic.APIError as exc:
            log.warning("extractor call failed, using empty extraction: %s", exc)
            return Extraction()
        self._track(config.EXTRACTOR_MODEL, resp)
        if resp.stop_reason != "end_turn":
            log.warning("extractor stop_reason=%s, using empty extraction", resp.stop_reason)
            return Extraction()
        return parse_extraction(_text_of(resp))

    def respond(
        self, phase: Phase, history: list[dict], text: str, plan_text: str, log_context: str = ""
    ) -> str:
        """One attempt on the responder model; if it refuses, one retry on the
        extractor model; then LLMError. Each refusal is logged with the model,
        phase, and directives so a pattern can be seen rather than guessed."""
        content = f"Customer says: {text}\n\n{plan_text}"
        messages = history + [{"role": "user", "content": content}]
        models = (config.RESPONDER_MODEL, config.EXTRACTOR_MODEL)
        for attempt, model in enumerate(models):
            try:
                resp = self.client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=responder_system(phase),
                    messages=messages,
                    output_config={"effort": "low"},
                    **_COMMON,
                )
            except anthropic.APIError as exc:
                raise LLMError(f"responder call failed: {exc}") from exc
            self._track(model, resp)
            if resp.stop_reason == "refusal":
                log.warning(
                    "responder refusal model=%s phase=%s directives=[%s] attempt=%d%s",
                    model, phase.value, log_context, attempt + 1,
                    "; retrying on " + models[1] if attempt == 0 else "; giving up",
                )
                continue
            reply = _text_of(resp)
            if not reply:
                raise LLMError("responder returned no text")
            return reply
        raise LLMError("responder refused twice")

    def smooth(self, text: str) -> str:
        try:
            resp = self.client.messages.create(
                model=config.RESPONDER_MODEL,
                max_tokens=1024,
                system=SMOOTH_SYSTEM,
                messages=[{"role": "user", "content": text}],
                output_config={"effort": "low"},
                **_COMMON,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"smoothing call failed: {exc}") from exc
        self._track(config.RESPONDER_MODEL, resp)
        out = _text_of(resp)
        if resp.stop_reason != "end_turn" or not out:
            raise LLMError("smoothing produced no usable text")
        return out
