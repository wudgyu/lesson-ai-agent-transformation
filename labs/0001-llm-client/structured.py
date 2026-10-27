"""Structured output + validation + repair loop for lesson 0001.

The model is asked for JSON that matches a schema. The schema is a contract,
not a suggestion: we parse, validate, and repair instead of trusting prose.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from llm_client import LLMConfig, OpenAICompatibleClient, build_messages

TICKET_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["billing", "technical", "account", "other"],
        },
        "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
        "summary": {"type": "string"},
        "needs_human": {"type": "boolean"},
    },
    "required": ["category", "priority", "summary", "needs_human"],
    "additionalProperties": False,
}

REQUIRED_FIELDS = ("category", "priority", "summary", "needs_human")
CATEGORIES = ("billing", "technical", "account", "other")
PRIORITIES = ("P0", "P1", "P2")


@dataclass
class ExtractionResult:
    data: Optional[Dict[str, Any]]
    raw: str
    attempts: int
    errors: List[str]

    @property
    def valid(self) -> bool:
        return self.data is not None and not self.errors


def build_extraction_messages(ticket_text: str) -> List[Dict[str, str]]:
    system = (
        "你是工单分类器。只输出 JSON，不要输出 Markdown 或解释。\n"
        "必须严格匹配下面的 JSON Schema：\n"
        + json.dumps(TICKET_SCHEMA, ensure_ascii=False, indent=2)
        + "\n规则：category 只能取枚举值；priority 只能是 P0/P1/P2；"
        "summary 用一句中文概括；拿不准时 needs_human=true。"
    )
    return build_messages(system, ticket_text)


def parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            data = json.loads(fenced.group(1).strip())
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start : index + 1])
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def validate_ticket(data: Any) -> List[str]:
    if not isinstance(data, dict):
        return ["output is not a JSON object"]
    errors: List[str] = []
    for field in REQUIRED_FIELDS:
        if field not in data:
            errors.append("missing field: %s" % field)
    for field in data:
        if field not in REQUIRED_FIELDS:
            errors.append("unexpected field: %s" % field)
    category = data.get("category")
    if category is not None and category not in CATEGORIES:
        errors.append("category must be one of %s" % ", ".join(CATEGORIES))
    priority = data.get("priority")
    if priority is not None and priority not in PRIORITIES:
        errors.append("priority must be one of %s" % ", ".join(PRIORITIES))
    summary = data.get("summary")
    if summary is not None and (not isinstance(summary, str) or not summary.strip()):
        errors.append("summary must be a non-empty string")
    needs_human = data.get("needs_human")
    if needs_human is not None and not isinstance(needs_human, bool):
        errors.append("needs_human must be a boolean")
    return errors


def extract_ticket(
    client,
    ticket_text: str,
    max_repairs: int = 1,
) -> ExtractionResult:
    messages = build_extraction_messages(ticket_text)
    last_raw = ""
    last_errors: List[str] = []
    for attempt in range(1, max_repairs + 2):
        try:
            response = client.complete(
                messages, response_format={"type": "json_object"}
            )
        except TypeError:
            response = client.complete(messages)

        raw = response["content"] if isinstance(response, dict) else str(response)
        last_raw = raw
        data = parse_json_object(raw)
        errors = validate_ticket(data)
        if not errors:
            return ExtractionResult(data=data, raw=raw, attempts=attempt, errors=[])
        last_errors = errors
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": "输出不合法：%s。只返回修正后的 JSON。"
                % "; ".join(errors),
            }
        )
    return ExtractionResult(
        data=None, raw=last_raw, attempts=max_repairs + 1, errors=last_errors
    )


class FakeStructuredLLM:
    def __init__(self, responses: Sequence[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, **kwargs) -> Dict[str, Any]:
        self.calls += 1
        if not self.responses:
            return {"content": "{}", "tool_calls": [], "finish_reason": "stop", "usage": {}}
        return {
            "content": self.responses.pop(0),
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": {},
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Structured output demo (lesson 0001)")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--ticket",
        default="生产环境订单服务大量 500，所有用户都无法下单，麻烦尽快处理。",
    )
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    if args.live:
        config = LLMConfig.from_env()
        if args.model:
            config.model = args.model
        client = OpenAICompatibleClient(config)
    else:
        client = FakeStructuredLLM(
            [
                '{"category": "technical", "priority": "P0", "summary": "订单服务 500", "needs_human": "yes"}',
                '{"category": "technical", "priority": "P0", "summary": "订单服务大量 500，用户无法下单", "needs_human": true}',
            ]
        )
        print("[demo] first response is invalid; repair loop should fix it")

    result = extract_ticket(client, args.ticket, max_repairs=1)
    print("valid:", result.valid, "attempts:", result.attempts)
    print("errors:", result.errors)
    print("data:", json.dumps(result.data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
