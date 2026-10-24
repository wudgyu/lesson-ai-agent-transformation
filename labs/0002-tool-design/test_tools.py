"""Contract tests for the refactored toolset.

这些测试不调用模型：工具首先是普通函数，应该像 API 一样被测试。
"""
import json
import unittest

import tools_refactored as refactored
from tools_baseline import build_registry as build_baseline
from tools_refactored import build_registry as build_refactored


class RefactoredToolsTest(unittest.TestCase):
    def setUp(self):
        refactored.reset_receipts()
        self.registry = build_refactored()

    def test_tool_count_and_names(self):
        self.assertEqual(
            self.registry.names(),
            [
                "get_service_health",
                "query_logs",
                "restart_service",
                "search_runbooks",
            ],
        )

    def test_health_returns_semantic_summary(self):
        payload = json.loads(
            self.registry.execute("get_service_health", {"service": "order-service"})
        )
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["db_pool"], {"used": 48, "max": 50})
        self.assertEqual(payload["recent_deploy"]["version"], "v2.14.1")
        self.assertIn("order-service", payload["summary"])
        self.assertIn("48/50", payload["summary"])

    def test_unknown_service_is_actionable(self):
        result = self.registry.execute("get_service_health", {"service": "nope"})
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("order-service", result)

    def test_query_logs_filters_and_limits(self):
        result = self.registry.execute(
            "query_logs",
            {
                "service": "order-service",
                "level": "ERROR",
                "since_minutes": 30,
                "limit": 5,
            },
        )
        self.assertIn("matched=", result)
        self.assertIn("shown=5", result)
        lines = [line for line in result.splitlines() if line.startswith("2026-")]
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(" ERROR " in line for line in lines))
        self.assertIn("HikariPool", result)

    def test_query_logs_respects_time_window(self):
        result = self.registry.execute(
            "query_logs",
            {
                "service": "order-service",
                "level": "ERROR",
                "since_minutes": 10,
                "limit": 50,
            },
        )
        # 窗口是 20:30–20:40，只有 20:34 的两条 ERROR。
        self.assertIn("matched=2", result)
        self.assertNotIn("20:12:03", result)

    def test_query_logs_invalid_level(self):
        result = self.registry.execute(
            "query_logs", {"service": "order-service", "level": "DEBUG"}
        )
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("ERROR, WARN, INFO, ALL", result)

    def test_query_logs_no_match_is_actionable(self):
        result = self.registry.execute(
            "query_logs",
            {
                "service": "order-service",
                "level": "ERROR",
                "since_minutes": 1,
            },
        )
        self.assertIn("no logs matched", result)
        self.assertIn("wider since_minutes", result)

    def test_search_runbooks_returns_source_and_line(self):
        result = self.registry.execute(
            "search_runbooks", {"query": "连接池", "limit": 3}
        )
        self.assertIn("runbook-db-pool.md", result)
        self.assertIn("max-active", result)
        self.assertIn(":", result)

    def test_search_runbooks_handles_long_chinese_query(self):
        result = self.registry.execute(
            "search_runbooks", {"query": "连接池耗尽", "limit": 3}
        )
        self.assertIn("max-active", result)

    def test_search_runbooks_no_match_is_actionable(self):
        result = self.registry.execute(
            "search_runbooks", {"query": "不存在的词", "limit": 3}
        )
        self.assertIn("no runbook matched", result)
        self.assertIn("shorter keyword", result)

    def test_restart_requires_approval(self):
        result = self.registry.execute(
            "restart_service",
            {"service": "order-service", "reason": "test", "approval_id": ""},
        )
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("approval_id", result)
        self.assertIn("do not guess", result)

    def test_restart_rejects_expired_approval(self):
        result = self.registry.execute(
            "restart_service",
            {
                "service": "payment-service",
                "reason": "test",
                "approval_id": "APR-4M8N-11BZ",
            },
        )
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("expired", result)

    def test_restart_is_idempotent(self):
        args = {
            "service": "order-service",
            "reason": "大促前恢复连接池",
            "approval_id": "APR-7F3K-92QX",
        }
        first = json.loads(self.registry.execute("restart_service", args))
        second = json.loads(self.registry.execute("restart_service", args))
        self.assertEqual(first["status"], "restarted")
        self.assertEqual(second["status"], "noop")
        self.assertEqual(first["receipt_id"], second["previous_receipt"]["receipt_id"])


class BaselineContrastTest(unittest.TestCase):
    def test_baseline_restarts_without_approval(self):
        registry = build_baseline()
        payload = json.loads(
            registry.execute("restart_service", {"svc": "order-service"})
        )
        self.assertEqual(payload["status"], "restarted")

    def test_baseline_has_more_tools(self):
        self.assertEqual(len(build_baseline().names()), 7)
        self.assertEqual(len(build_refactored().names()), 4)


if __name__ == "__main__":
    unittest.main()
