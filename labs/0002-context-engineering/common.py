"""Reuse Lesson 0001's client, JSON parser and ticket validator."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

LESSON1 = Path(__file__).resolve().parent.parent / "0001-llm-client"
if str(LESSON1) not in sys.path:
    sys.path.insert(0, str(LESSON1))

from llm_client import (  # noqa: E402
    LLMConfig,
    OpenAICompatibleClient,
    build_messages,
    estimate_tokens,
)
from structured import (  # noqa: E402
    CATEGORIES,
    PRIORITIES,
    TICKET_SCHEMA,
    parse_json_object,
    validate_ticket,
)

__all__ = [
    "LLMConfig",
    "OpenAICompatibleClient",
    "build_messages",
    "estimate_tokens",
    "CATEGORIES",
    "PRIORITIES",
    "TICKET_SCHEMA",
    "parse_json_object",
    "validate_ticket",
]
