"""Offline tests for lesson 0002. No API key required."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from classifier import (
    BASELINE_SYSTEM,
    ENGINEERED_SYSTEM,
    FakeClassifierLLM,
    build_classifier_messages,
    classify,
    expected_match,
    schema_contract_summary,
)
from common import parse_json_object, validate_ticket
from eval import load_json, run_case, summarize

ROOT = Path(__file__).resolve().parent


class PromptTests(unittest.TestCase):
    def test_baseline_prompt(self):
        messages = build_classifier_messages("baseline", "发票抬头错了")
        self.assertEqual(messages[0]["content"], BASELINE_SYSTEM)
        self.assertIn("<ticket>", messages[1]["content"])

    def test_engineered_prompt_has_contract_and_examples(self):
        messages = build_classifier_messages("engineered", "订单服务 500")
        system = messages[0]["content"]
        self.assertIn("输出契约", system)
        self.assertIn("category 只能取", system)
        self.assertIn("示例 1", system)
        self.assertIn("数据边界", system)
        self.assertIn("<ticket>", messages[1]["content"])

    def test_engineered_prompt_is_longer(self):
        self.assertGreater(len(ENGINEERED_SYSTEM), len(BASELINE_SYSTEM))

    def test_unknown_version(self):
        with self.assertRaises(ValueError):
            build_classifier_messages("magic", "x")

    def test_schema_summary(self):
        summary = schema_contract_summary()
        self.assertIn('"category"', summary)
        self.assertIn('"required"', summary)


class ClassificationTests(unittest.TestCase):
    def test_classify_valid(self):
        raw = '{"category":"billing","priority":"P2","summary":"发票问题","needs_human":false}'
        client = FakeClassifierLLM(raw, raw)
        result = classify(client, "baseline", "发票抬头错了")
        self.assertTrue(result.schema_valid)
        self.assertEqual(result.data["category"], "billing")

    def test_classify_invalid(self):
        raw = '{"category":"sales","priority":"P2","summary":"发票问题","needs_human":false}'
        client = FakeClassifierLLM(raw, raw)
        result = classify(client, "baseline", "发票抬头错了")
        self.assertFalse(result.schema_valid)
        self.assertTrue(result.errors)

    def test_expected_match(self):
        case = {
            "expected_category": "technical",
            "expected_priority": "P0",
            "expected_needs_human": True,
        }
        data = {
            "category": "technical",
            "priority": "P0",
            "needs_human": True,
        }
        self.assertTrue(expected_match(data, case))
        data["priority"] = "P1"
        self.assertFalse(expected_match(data, case))

    def test_parse_invalid_json(self):
        self.assertIsNone(parse_json_object("not json"))

    def test_validate_ticket_reuses_lesson1(self):
        self.assertTrue(validate_ticket({"category": "sales"}))


class FixtureEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_json(ROOT / "cases.json")
        cls.fixtures = load_json(ROOT / "fixtures.json")

    def run_version(self, version):
        rows = [
            run_case(case, version, "fixtures", self.fixtures, None)
            for case in self.cases
        ]
        return rows, summarize(rows, version)

    def test_engineered_is_perfect_in_fixtures(self):
        _, summary = self.run_version("engineered")
        self.assertEqual(summary["exact_accuracy"], 1.0)
        self.assertEqual(summary["schema_validity"], 1.0)

    def test_baseline_has_schema_and_classification_errors(self):
        _, summary = self.run_version("baseline")
        self.assertLess(summary["exact_accuracy"], 0.8)
        self.assertLess(summary["schema_validity"], 1.0)
        self.assertGreater(summary["parse_failures"], 0)

    def test_fixtures_cover_every_case(self):
        for case in self.cases:
            self.assertIn(case["id"], self.fixtures)
            for version in ("baseline", "engineered"):
                self.assertIn(version, self.fixtures[case["id"]])

    def test_case_labels_are_valid(self):
        for case in self.cases:
            self.assertIn(case["expected_category"], ("billing", "technical", "account", "other"))
            self.assertIn(case["expected_priority"], ("P0", "P1", "P2"))
            self.assertIsInstance(case["expected_needs_human"], bool)

    def test_engineered_fixtures_parse(self):
        for case in self.cases:
            raw = self.fixtures[case["id"]]["engineered"]
            data = parse_json_object(raw)
            self.assertIsNotNone(data, case["id"])
            self.assertEqual(validate_ticket(data), [], case["id"])


if __name__ == "__main__":
    unittest.main()
