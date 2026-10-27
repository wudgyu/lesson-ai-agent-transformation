"""Offline tests for lesson 0001. No API key required.

    python3 -m unittest -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_client import (
    FakeStreamingLLM,
    LLMConfig,
    build_messages,
    estimate_tokens,
    load_dotenv,
)
from structured import (
    FakeStructuredLLM,
    TICKET_SCHEMA,
    build_extraction_messages,
    extract_ticket,
    parse_json_object,
    validate_ticket,
)


class ClientTests(unittest.TestCase):
    def test_build_messages(self):
        messages = build_messages("system", "user")
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"], "user")

    def test_estimate_tokens(self):
        self.assertGreater(estimate_tokens("token 预算"), 0)
        self.assertEqual(estimate_tokens(""), 0)

    def test_fake_stream_yields_deltas_and_done(self):
        client = FakeStreamingLLM(
            ["你", "好"],
            usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        )
        events = list(client.stream_chat([]))
        self.assertEqual([e["type"] for e in events], ["delta", "delta", "done"])
        self.assertEqual(events[-1]["content"], "你好")
        self.assertEqual(events[-1]["usage"]["total_tokens"], 4)

    def test_fake_complete(self):
        client = FakeStreamingLLM([], complete_response={"content": "ok"})
        self.assertEqual(client.complete([])["content"], "ok")

    def test_load_dotenv_does_not_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("LLM_API_KEY=from-file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"LLM_API_KEY": "from-env"}, clear=False):
                load_dotenv(str(path))
                self.assertEqual(os.environ["LLM_API_KEY"], "from-env")

    def test_config_without_key_fails(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                LLMConfig.from_env()


class ParsingTests(unittest.TestCase):
    def test_parse_plain_json(self):
        data = parse_json_object('{"a": 1}')
        self.assertEqual(data, {"a": 1})

    def test_parse_fenced_json(self):
        data = parse_json_object('```json\n{"a": 1}\n```')
        self.assertEqual(data, {"a": 1})

    def test_parse_json_with_prose(self):
        data = parse_json_object('结果如下：{"a": 1} 以上。')
        self.assertEqual(data, {"a": 1})

    def test_parse_invalid(self):
        self.assertIsNone(parse_json_object("not json"))


class ValidationTests(unittest.TestCase):
    def valid_ticket(self):
        return {
            "category": "technical",
            "priority": "P0",
            "summary": "订单服务大量 500",
            "needs_human": True,
        }

    def test_valid_ticket(self):
        self.assertEqual(validate_ticket(self.valid_ticket()), [])

    def test_missing_field(self):
        data = self.valid_ticket()
        del data["priority"]
        self.assertTrue(any("missing field" in e for e in validate_ticket(data)))

    def test_bad_enum(self):
        data = self.valid_ticket()
        data["category"] = "sales"
        self.assertTrue(any("category" in e for e in validate_ticket(data)))

    def test_bad_bool(self):
        data = self.valid_ticket()
        data["needs_human"] = "yes"
        self.assertTrue(any("needs_human" in e for e in validate_ticket(data)))

    def test_extra_field(self):
        data = self.valid_ticket()
        data["confidence"] = 0.9
        self.assertTrue(any("unexpected field" in e for e in validate_ticket(data)))

    def test_not_object(self):
        self.assertTrue(validate_ticket(["technical"]))

    def test_prompt_contains_schema(self):
        messages = build_extraction_messages("订单服务 500")
        self.assertIn("JSON Schema", messages[0]["content"])
        self.assertIn('"category"', messages[0]["content"])
        self.assertIn("订单服务 500", messages[1]["content"])


class RepairTests(unittest.TestCase):
    def test_repair_loop_fixes_invalid_output(self):
        client = FakeStructuredLLM(
            [
                '{"category": "technical", "priority": "P0", "summary": "订单服务 500", "needs_human": "yes"}',
                '{"category": "technical", "priority": "P0", "summary": "订单服务大量 500", "needs_human": true}',
            ]
        )
        result = extract_ticket(client, "订单服务大量 500", max_repairs=1)
        self.assertTrue(result.valid)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.data["needs_human"], True)

    def test_repair_exhausted(self):
        client = FakeStructuredLLM(
            [
                '{"category": "sales"}',
                '{"category": "sales"}',
            ]
        )
        result = extract_ticket(client, "x", max_repairs=1)
        self.assertFalse(result.valid)
        self.assertEqual(result.attempts, 2)
        self.assertTrue(result.errors)

    def test_schema_is_closed(self):
        self.assertFalse(TICKET_SCHEMA["additionalProperties"])
        self.assertIn("category", TICKET_SCHEMA["required"])


if __name__ == "__main__":
    unittest.main()
