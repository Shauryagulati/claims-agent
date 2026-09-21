"""One-off check that the configured model IDs and request shapes work.

Not part of the test suite. Makes two real API calls and prints the result.
Run: .venv/bin/python scripts/smoke_models.py [--extractor-only]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from app import config  # noqa: E402
from app.llm import extraction_schema, parse_extraction  # noqa: E402

COMMON = {
    "max_tokens": 256,
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "low"},
}

def _text(resp: anthropic.types.Message) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")


def check_responder(client: anthropic.Anthropic) -> bool:
    print(f"[responder] model={config.RESPONDER_MODEL}")
    resp = client.messages.create(
        model=config.RESPONDER_MODEL,
        system="You are a claims support specialist. Reply in one short sentence.",
        messages=[{"role": "user", "content": "Say hello to a caller named Nadia."}],
        **COMMON,
    )
    print(f"  stop_reason={resp.stop_reason} served_by={resp.model}")
    print(f"  usage in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
    print(f"  text={_text(resp)!r}")
    return resp.stop_reason == "end_turn" and bool(_text(resp))


def check_extractor(client: anthropic.Anthropic) -> bool:
    print(f"[extractor] model={config.EXTRACTOR_MODEL}")
    resp = client.messages.create(
        model=config.EXTRACTOR_MODEL,
        system="Extract fields from the caller's message as JSON.",
        messages=[{"role": "user", "content": "Hi, this is Nadia Okonkwo, why was my claim denied?"}],
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": extraction_schema()},
        },
        max_tokens=256,
        thinking={"type": "adaptive"},
    )
    print(f"  stop_reason={resp.stop_reason} served_by={resp.model}")
    print(f"  usage in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
    raw = _text(resp)
    print(f"  text={raw!r}")
    ex = parse_extraction(raw)
    ok = ex.claimed_name == "Nadia Okonkwo" and ex.intent == "denial_question"
    print(f"  parsed claimed_name={ex.claimed_name!r} intent={ex.intent!r} plausible={ok}")
    return resp.stop_reason == "end_turn" and ok


def main() -> int:
    config.require_api_key()
    client = anthropic.Anthropic()
    results = {}
    checks = (("responder", check_responder), ("extractor", check_extractor))
    if "--extractor-only" in sys.argv:
        checks = (("extractor", check_extractor),)
    for name, fn in checks:
        try:
            results[name] = fn(client)
        except anthropic.APIStatusError as exc:
            print(f"  API error {exc.status_code}: {exc.message}")
            results[name] = False
        except anthropic.APIConnectionError as exc:
            print(f"  connection error: {exc}")
            results[name] = False
        print()
    print("RESULT:", "OK" if all(results.values()) else f"FAILED {results}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
