"""Constants and environment reads. Nothing else in app/ reads os.environ."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))

# The only "today" the agent knows. After the newest fixture claim
# (2026-09-14) and before the earliest appeal deadline (2026-10-01).
DEMO_NOW = date(2026, 9, 18)

# The fictional insurer the agent works for. Nothing in the logic depends on
# it; it exists so the persona, the greeting and the UI all say the same name.
BRAND_NAME = os.getenv("BRAND_NAME", "Kestrel Mutual")

AGENT_NAME = os.getenv("AGENT_NAME", "Dana")

EXTRACTOR_MODEL = os.getenv("EXTRACTOR_MODEL", "claude-sonnet-5")
RESPONDER_MODEL = os.getenv("RESPONDER_MODEL", "claude-opus-5")

VERIFY_MIN_MATCHES = 3
VERIFY_OFFER_HUMAN_AT = 3
OOS_OFFER_HUMAN_AT = 2
REFUSAL_OFFER_HUMAN_AT = 2


def require_api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return key
