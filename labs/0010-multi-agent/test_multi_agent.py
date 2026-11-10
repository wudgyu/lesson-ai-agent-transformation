"""Offline tests for Lesson 0010. No API key required."""
from __future__ import annotations

import unittest

from multi_agent import (
    SCENARIOS,
    run_flat,
    run_handoff,
    run_supervisor,
    run_worker,
)


class WorkerIsolationTest(unittest.TestCase):
    def test_worker_context_has_no_parent_state(self):
        result = run_worker("logs", "查日志", "simple")
        act = next(item for item in result["trace"] if item["node"] == "worker.act")
        self.assertNotIn("findings", act["context_keys"])
        self.assertNotIn("question", act["context_keys"])
        self.assertNotIn("history", act["context_keys"])

    def test_worker_retries_with_fallback_tool(self):
        result = run_worker("logs", "查日志", "retry")
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool_calls"], 2)
        tools = [item["tool"] for item in result["trace"] if item["node"] == "worker.tool"]
        self.assertEqual(tools, ["query_logs", "check_health"])
        self.assertTrue(any("health" in item for item in result["evidence"]))

    def test_worker_stops_after_two_failed_attempts(self):
        result = run_worker("logs", "查日志", "dead_tool")
        self.assertFalse(result["ok"])
        self.assertEqual(result["tool_calls"], 2)
        self.assertIn("未收集到", result["finding"])


class SupervisorTest(unittest.TestCase):
    def test_simple_scenario_uses_one_worker(self):
        result = run_supervisor("simple")
        self.assertEqual([item["agent"] for item in result["findings"]], ["logs"])
        self.assertAlmostEqual(result["coverage"], 1.0)
        self.assertEqual(result["stop_reason"], "complete")
        self.assertIn("[logs]", result["answer"])

    def test_breadth_scenario_covers_three_domains(self):
        result = run_supervisor("breadth")
        agents = {item["agent"] for item in result["findings"]}
        self.assertEqual(agents, {"logs", "code", "runbook"})
        self.assertAlmostEqual(result["coverage"], 1.0)
        for agent in agents:
            self.assertIn("[%s]" % agent, result["answer"])

    def test_delegate_passes_no_parent_state_to_worker(self):
        result = run_supervisor("breadth")
        delegates = [item for item in result["trace"] if item["node"] == "delegate"]
        self.assertEqual(len(delegates), 3)
        for item in delegates:
            self.assertFalse(item["worker_saw_parent_findings"])
            self.assertFalse(item["worker_saw_question"])
        supervisor_calls = [item for item in result["trace"] if item["node"] == "supervisor"]
        self.assertTrue(any(item["context_keys"] for item in supervisor_calls))

    def test_budget_stops_supervisor_before_plan_finishes(self):
        result = run_supervisor("breadth", max_steps=2)
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertLess(result["coverage"], 1.0)
        self.assertIn("budget_exhausted", [item.get("event") for item in result["trace"]])

    def test_dead_tool_yields_partial_answer(self):
        result = run_supervisor("dead_tool")
        self.assertEqual(result["stop_reason"], "partial")
        self.assertAlmostEqual(result["coverage"], 0.5)
        self.assertIn("证据不足", result["answer"])
        self.assertIn("logs", result["answer"])


class FlatAgentTest(unittest.TestCase):
    def test_flat_agent_drops_oversized_observation(self):
        result = run_flat("breadth")
        self.assertIn("logs", result["dropped"])
        self.assertLess(result["coverage"], 1.0)
        self.assertIn("上下文已丢弃", result["answer"])

    def test_flat_agent_is_cheaper_on_a_simple_task(self):
        flat = run_flat("simple")
        supervisor = run_supervisor("simple")
        self.assertAlmostEqual(flat["coverage"], 1.0)
        self.assertAlmostEqual(supervisor["coverage"], 1.0)
        self.assertLess(flat["tokens"], supervisor["tokens"])

    def test_breadth_tradeoff_costs_more_but_covers_more(self):
        flat = run_flat("breadth")
        supervisor = run_supervisor("breadth")
        self.assertGreater(supervisor["coverage"], flat["coverage"])
        self.assertGreater(supervisor["tokens"], flat["tokens"])
        self.assertGreater(flat["tokens"], run_flat("simple")["tokens"])


class HandoffTest(unittest.TestCase):
    def test_handoff_transfers_control_and_shares_history(self):
        result = run_handoff("simple")
        self.assertEqual(result["triage_runs"], 1)
        self.assertEqual(result["handoffs"], 1)
        self.assertEqual(result["active_agent"], "done")
        self.assertGreaterEqual(len(result["history"]), 2)
        self.assertIn("[logs]", result["answer"])

    def test_handoff_chain_visits_all_domains(self):
        result = run_handoff("breadth")
        agents = [item["agent"] for item in result["findings"]]
        self.assertEqual(agents, ["logs", "code", "runbook"])
        self.assertEqual(result["handoffs"], 3)
        self.assertAlmostEqual(result["coverage"], 1.0)
        specialists = [item for item in result["trace"] if item["node"].endswith("_agent")]
        self.assertTrue(any(item["saw_prior_findings"] > 0 for item in specialists))

    def test_handoff_partial_when_a_specialist_fails(self):
        result = run_handoff("dead_tool")
        self.assertEqual(result["stop_reason"], "partial")
        self.assertAlmostEqual(result["coverage"], 0.5)
        self.assertIn("证据不足", result["answer"])


class FixtureTest(unittest.TestCase):
    def test_all_scenarios_have_a_question_and_plan(self):
        for name, scenario in SCENARIOS.items():
            self.assertTrue(scenario["question"], name)
            self.assertTrue(scenario["domains"], name)
            for agent in scenario["domains"]:
                self.assertIn(agent, scenario["findings"], name)
                self.assertIn(agent, scenario["evidence"], name)


if __name__ == "__main__":
    unittest.main()
