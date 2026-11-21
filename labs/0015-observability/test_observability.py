"""第 15 课 lab 的离线测试。

不需要 API key、网络或 sleep。测试分四组：

- schema：trace / span / usage 的契约
- pricing：价格版本、cache、未知模型
- ledger：重复计费、失败成本、cost per success
- latency / dashboard：critical path、percentile、门禁

运行：python3 -m unittest test_observability -v
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import obs_cli
from dashboard import BLOCK, PASS, WARN, build_dashboard
from latency import (
    analyze_trace,
    check_budget,
    critical_path,
    exclusive_ms,
    interval_union_ms,
    percentile,
    summarize,
    tail_amplification,
)
from ledger import Ledger, compute_trace_cost
from pricing import Cost, ModelPrice, PriceBook, UnknownModelError
from trace_schema import Span, Trace, Usage, load_traces

HERE = Path(__file__).resolve().parent
TRACES = load_traces(HERE / "traces.jsonl")
PRICEBOOK = PriceBook.from_path(HERE / "pricing.json")
LEDGER = Ledger(TRACES, PRICEBOOK)
BUDGETS = json.loads((HERE / "budgets.json").read_text(encoding="utf-8"))
TRACE_BY_ID = {trace.trace_id: trace for trace in TRACES}


def usage_attrs(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
):
    return {
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.cache_read.input_tokens": cache_read,
        "gen_ai.usage.cache_write.input_tokens": cache_write,
        "gen_ai.usage.reasoning.output_tokens": reasoning,
    }


def raw_span(
    span_id,
    parent_id,
    kind,
    start,
    end,
    status="ok",
    attrs=None,
    trace_id="T01",
):
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_id": parent_id,
        "name": "%s %s" % (kind, span_id),
        "kind": kind,
        "start_ms": start,
        "end_ms": end,
        "status": status,
        "attrs": attrs or {},
    }


def minimal_trace(
    trace_id="T01",
    success=True,
    root_usage=True,
    model="medic-small",
    llm_usage=True,
    include_tool=True,
):
    spans = [
        raw_span(
            "root",
            None,
            "agent",
            0,
            1000,
            attrs={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "RepoMedic",
                "gen_ai.request.model": model,
            },
            trace_id=trace_id,
        ),
        raw_span(
            "llm",
            "root",
            "llm",
            0,
            600,
            attrs={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": model,
                **(usage_attrs(1000, 100) if llm_usage else {}),
            },
            trace_id=trace_id,
        ),
    ]
    if include_tool:
        spans.append(
            raw_span(
                "tool",
                "root",
                "tool",
                600,
                1000,
                attrs={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "query_logs",
                },
                trace_id=trace_id,
            )
        )
    if root_usage:
        spans[0]["attrs"].update(usage_attrs(1000, 100))
    return Trace.from_dict(
        {
            "trace_id": trace_id,
            "run_id": "run-" + trace_id,
            "case_id": "C01",
            "arm": "v1",
            "success": success,
            "started_at": "2026-11-01T00:00:00Z",
            "spans": spans,
        }
    )


class UsageTests(unittest.TestCase):
    def test_missing_usage_is_none(self):
        self.assertIsNone(Usage.from_attrs({"gen_ai.request.model": "x"}))

    def test_zero_usage_is_not_missing(self):
        usage = Usage.from_attrs(usage_attrs(0, 0))
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 0)

    def test_negative_tokens_rejected(self):
        usage = Usage(input_tokens=-1)
        self.assertTrue(any("negative" in item for item in usage.validate("x")))

    def test_cache_subset_of_input(self):
        usage = Usage(input_tokens=100, cache_read_input_tokens=80, cache_write_input_tokens=30)
        self.assertTrue(any("exceeds input_tokens" in item for item in usage.validate("x")))

    def test_reasoning_subset_of_output(self):
        usage = Usage(output_tokens=10, reasoning_output_tokens=11)
        self.assertTrue(any("reasoning" in item for item in usage.validate("x")))

    def test_usage_addition(self):
        left = Usage(100, 10, cache_read_input_tokens=40)
        right = Usage(50, 5, cache_read_input_tokens=20)
        total = left + right
        self.assertEqual(total.input_tokens, 150)
        self.assertEqual(total.cache_read_input_tokens, 60)

    def test_cache_hit_rate(self):
        self.assertAlmostEqual(Usage(100, 0, cache_read_input_tokens=40).cache_hit_rate, 0.4)
        self.assertEqual(Usage(0, 0).cache_hit_rate, 0.0)


class SpanSchemaTests(unittest.TestCase):
    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            Span.from_dict(raw_span("s", None, "database", 0, 1))

    def test_end_before_start_rejected(self):
        with self.assertRaises(ValueError):
            Span.from_dict(raw_span("s", None, "tool", 10, 5))

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            Span.from_dict(raw_span("s", None, "tool", 0, 1, status="maybe"))

    def test_usage_is_validated_on_span(self):
        raw = raw_span(
            "s", None, "llm", 0, 1,
            attrs={"gen_ai.operation.name": "chat", "gen_ai.request.model": "m", **usage_attrs(10, 5, cache_read=11)},
        )
        with self.assertRaises(ValueError):
            Span.from_dict(raw)

    def test_properties_from_otel_attributes(self):
        raw = raw_span(
            "s", None, "llm", 0, 1,
            attrs={"gen_ai.operation.name": "chat", "gen_ai.request.model": "m", **usage_attrs(10, 5)},
        )
        span = Span.from_dict(raw)
        self.assertEqual(span.operation_name, "chat")
        self.assertEqual(span.model, "m")
        self.assertEqual(span.duration_ms, 1)
        self.assertTrue(span.is_inference)


class TraceSchemaTests(unittest.TestCase):
    def test_duplicate_span_id_rejected(self):
        spans = [
            raw_span("root", None, "agent", 0, 10),
            raw_span("root", "root", "tool", 0, 5),
        ]
        with self.assertRaises(ValueError):
            Trace.from_dict(
                {"trace_id": "T", "run_id": "r", "case_id": "c", "arm": "a", "success": True, "spans": spans}
            )

    def test_unknown_parent_rejected(self):
        spans = [raw_span("root", None, "agent", 0, 10), raw_span("x", "ghost", "tool", 0, 5)]
        with self.assertRaises(ValueError):
            Trace.from_dict(
                {"trace_id": "T", "run_id": "r", "case_id": "c", "arm": "a", "success": True, "spans": spans}
            )

    def test_multiple_roots_rejected(self):
        spans = [raw_span("a", None, "agent", 0, 10), raw_span("b", None, "tool", 0, 5)]
        with self.assertRaises(ValueError):
            Trace.from_dict(
                {"trace_id": "T", "run_id": "r", "case_id": "c", "arm": "a", "success": True, "spans": spans}
            )

    def test_root_must_be_agent(self):
        spans = [raw_span("root", None, "tool", 0, 10)]
        with self.assertRaises(ValueError):
            Trace.from_dict(
                {"trace_id": "T", "run_id": "r", "case_id": "c", "arm": "a", "success": True, "spans": spans}
            )

    def test_child_must_fit_parent(self):
        spans = [raw_span("root", None, "agent", 0, 10), raw_span("x", "root", "tool", 5, 11)]
        with self.assertRaises(ValueError):
            Trace.from_dict(
                {"trace_id": "T", "run_id": "r", "case_id": "c", "arm": "a", "success": True, "spans": spans}
            )

    def test_llm_needs_operation_name(self):
        spans = [
            raw_span("root", None, "agent", 0, 10),
            raw_span("x", "root", "llm", 0, 5, attrs={"gen_ai.request.model": "m"}),
        ]
        with self.assertRaises(ValueError):
            Trace.from_dict(
                {"trace_id": "T", "run_id": "r", "case_id": "c", "arm": "a", "success": True, "spans": spans}
            )

    def test_llm_needs_model(self):
        spans = [
            raw_span("root", None, "agent", 0, 10),
            raw_span("x", "root", "llm", 0, 5, attrs={"gen_ai.operation.name": "chat"}),
        ]
        with self.assertRaises(ValueError):
            Trace.from_dict(
                {"trace_id": "T", "run_id": "r", "case_id": "c", "arm": "a", "success": True, "spans": spans}
            )

    def test_walk_is_preorder(self):
        trace = minimal_trace()
        self.assertEqual([span.span_id for span in trace.walk()], ["root", "llm", "tool"])

    def test_children_sorted_by_start(self):
        trace = minimal_trace()
        self.assertEqual([span.span_id for span in trace.children_of("root")], ["llm", "tool"])

    def test_load_real_fixtures(self):
        self.assertEqual(len(TRACES), 10)
        self.assertEqual(sum(len(trace.spans) for trace in TRACES), 74)

    def test_duplicate_trace_id_rejected(self):
        spans = [
            raw_span(
                "root", None, "agent", 0, 1000,
                attrs={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "R"},
                trace_id="T",
            ),
            raw_span(
                "llm", "root", "llm", 0, 1000,
                attrs={"gen_ai.operation.name": "chat", "gen_ai.request.model": "m", **usage_attrs(10, 1)},
                trace_id="T",
            ),
        ]
        payload = {
            "trace_id": "T", "run_id": "r", "case_id": "c", "arm": "a",
            "success": True, "spans": spans,
        }
        with self.assertRaises(ValueError):
            import tempfile

            with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
                handle.write(json.dumps(payload) + "\n")
                handle.write(json.dumps(payload) + "\n")
                path = Path(handle.name)
            try:
                load_traces(path)
            finally:
                path.unlink()


class UsageAccountingTests(unittest.TestCase):
    def test_billed_usage_excludes_agent_aggregate(self):
        usage, missing = TRACE_BY_ID["R01"].billed_usage()
        self.assertEqual(usage.input_tokens, 9000)
        self.assertEqual(usage.output_tokens, 940)
        self.assertEqual(missing, set())

    def test_naive_sum_would_double_count(self):
        trace = TRACE_BY_ID["R01"]
        naive = Usage()
        for span in trace.spans:
            if span.usage:
                naive = naive + span.usage
        self.assertEqual(naive.input_tokens, 18000)

    def test_missing_usage_is_reported_not_estimated(self):
        usage, missing = TRACE_BY_ID["R08"].billed_usage()
        self.assertEqual(usage.input_tokens, 3900)
        self.assertEqual(missing, {"R08-l2"})

    def test_reconciliation_ok(self):
        self.assertEqual(TRACE_BY_ID["R01"].reconciliation()["status"], "ok")

    def test_reconciliation_mismatch(self):
        rec = TRACE_BY_ID["R09"].reconciliation()
        self.assertEqual(rec["status"], "mismatch")
        self.assertEqual(rec["delta_input"], 500)

    def test_reconciliation_incomplete(self):
        self.assertEqual(TRACE_BY_ID["R08"].reconciliation()["status"], "incomplete")


class PricingTests(unittest.TestCase):
    def test_price_effective_date(self):
        price = PRICEBOOK.price_for("medic-large", "2026-10-15")
        self.assertEqual(price.input_per_million, 2.50)
        with self.assertRaises(UnknownModelError):
            PRICEBOOK.price_for("medic-large", "2026-09-30")

    def test_unknown_model(self):
        with self.assertRaises(UnknownModelError):
            PRICEBOOK.price_for("ghost")

    def test_uncached_input_and_output_cost(self):
        cost = PRICEBOOK.cost(Usage(1_000_000, 1_000_000), "medic-large")
        self.assertAlmostEqual(cost.total, 12.50, places=6)

    def test_cache_read_savings(self):
        cost = PRICEBOOK.cost(
            Usage(1_000_000, 0, cache_read_input_tokens=1_000_000),
            "medic-large",
        )
        self.assertAlmostEqual(cost.total, 0.25, places=6)
        self.assertAlmostEqual(cost.cache_savings, 2.25, places=6)

    def test_cache_write_premium_reduces_savings(self):
        cost = PRICEBOOK.cost(
            Usage(1_000_000, 0, cache_write_input_tokens=1_000_000),
            "medic-large",
        )
        self.assertAlmostEqual(cost.total, 3.125, places=6)
        self.assertLess(cost.cache_savings, 0)

    def test_cache_tokens_cannot_exceed_input(self):
        with self.assertRaises(ValueError):
            PRICEBOOK.cost(Usage(10, 0, cache_read_input_tokens=11), "medic-large")

    def test_negative_price_rejected(self):
        with self.assertRaises(ValueError):
            ModelPrice.from_dict(
                {
                    "model": "m",
                    "input_per_million": -1,
                    "output_per_million": 1,
                    "cache_read_per_million": 1,
                    "cache_write_per_million": 1,
                }
            )

    def test_cost_addition(self):
        total = Cost(input_cost=1.0, output_cost=2.0) + Cost(input_cost=0.5, output_cost=0.25)
        self.assertAlmostEqual(total.total, 3.75)


class LedgerTests(unittest.TestCase):
    def test_total_cost(self):
        self.assertAlmostEqual(LEDGER.total, 0.167829, places=6)

    def test_cost_per_success(self):
        self.assertAlmostEqual(LEDGER.cost_per_success, 0.020978625, places=6)

    def test_success_count(self):
        self.assertEqual(LEDGER.successes, 8)
        self.assertEqual(LEDGER.runs, 10)

    def test_failed_run_cost_included(self):
        self.assertGreater(LEDGER.entry("R03").total, 0)
        self.assertAlmostEqual(LEDGER.failed_run_cost, 0.0175, places=6)

    def test_retry_cost_counts_failed_call_and_retry(self):
        self.assertAlmostEqual(LEDGER.entry("R03").retry_cost, 0.0091, places=6)

    def test_retry_share(self):
        self.assertAlmostEqual(LEDGER.retry_share, 0.054222, places=5)

    def test_cache_hit_rate(self):
        self.assertAlmostEqual(LEDGER.cache_hit_rate, 0.388704, places=5)

    def test_by_model(self):
        by_model = LEDGER.by_model()
        self.assertAlmostEqual(by_model["medic-large"].total, 0.160125, places=6)
        self.assertAlmostEqual(by_model["medic-small"].total, 0.007704, places=6)

    def test_by_arm(self):
        by_arm = LEDGER.by_arm()
        self.assertEqual(by_arm["v05-hotfix"].successes, 4)
        self.assertEqual(by_arm["v06-candidate"].successes, 4)
        self.assertFalse(by_arm["v06-candidate"].known_cost)

    def test_top_run_is_long_context(self):
        self.assertEqual(LEDGER.top_runs(1)[0].trace.trace_id, "R06")

    def test_context_growth_r06(self):
        growth = Ledger.context_growth(TRACE_BY_ID["R06"])
        self.assertEqual(growth["turns"], 8)
        self.assertEqual(growth["total_input"], 26400)
        self.assertAlmostEqual(growth["growth_ratio"], 4.5)
        self.assertAlmostEqual(growth["rebilled_ratio"], 4.888888, places=5)

    def test_missing_usage_marks_entry_incomplete(self):
        self.assertFalse(LEDGER.entry("R08").complete)
        self.assertIn("R08-l2", LEDGER.entry("R08").missing_usage)

    def test_agent_usage_not_billed(self):
        entry = compute_trace_cost(TRACE_BY_ID["R01"], PRICEBOOK)
        self.assertAlmostEqual(entry.total, 0.0193, places=6)

    def test_unknown_model_is_reported(self):
        trace = minimal_trace(model="ghost")
        entry = compute_trace_cost(trace, PRICEBOOK)
        self.assertIn("ghost", entry.unknown_models)
        self.assertFalse(entry.complete)


class LatencyTests(unittest.TestCase):
    def test_interval_union(self):
        self.assertEqual(interval_union_ms([(0, 10), (5, 15), (20, 25)]), 20)
        self.assertEqual(interval_union_ms([]), 0)

    def test_exclusive_sequential_parent(self):
        root = Span("t", "root", None, "r", "agent", 0, 100)
        child = Span("t", "child", "root", "c", "tool", 0, 100)
        self.assertEqual(exclusive_ms(root, [child]), 0)

    def test_exclusive_parallel_parent(self):
        root = Span("t", "root", None, "r", "agent", 0, 100)
        a = Span("t", "a", "root", "a", "tool", 20, 40)
        b = Span("t", "b", "root", "b", "tool", 20, 40)
        self.assertEqual(exclusive_ms(root, [a, b]), 80)

    def test_critical_path_sequential(self):
        cost, path = critical_path(TRACE_BY_ID["R01"])
        self.assertEqual(cost, 14000)
        self.assertEqual(path[0].span_id, "R01-root")

    def test_critical_path_parallel_tools(self):
        cost, path = critical_path(TRACE_BY_ID["R05"])
        self.assertEqual(cost, 12000)
        self.assertIn("R05-t1", [span.span_id for span in path])
        self.assertNotIn("R05-t2", [span.span_id for span in path])

    def test_critical_path_nested_agent(self):
        cost, _path = critical_path(TRACE_BY_ID["R09"])
        self.assertEqual(cost, 18000)

    def test_human_wait_is_blocked_not_active(self):
        latency = analyze_trace(TRACE_BY_ID["R04"])
        self.assertEqual(latency.wall_ms, 95000)
        self.assertEqual(latency.blocked_ms, 88000)
        self.assertEqual(latency.active_ms, 7000)
        self.assertEqual(latency.active_critical_path_ms, 7000)

    def test_stage_union_counts_parallel_tools_once(self):
        latency = analyze_trace(TRACE_BY_ID["R05"])
        self.assertEqual(latency.stage_union_ms["tool"], 7000)
        durations = sum(span.duration_ms for span in TRACE_BY_ID["R05"].spans if span.kind == "tool")
        self.assertEqual(durations, 14000)

    def test_percentile_nearest_rank(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(percentile(values, 50), 5)
        self.assertEqual(percentile(values, 95), 10)
        self.assertEqual(percentile(values, 0), 1)
        self.assertEqual(percentile(values, 100), 10)
        self.assertEqual(percentile([], 95), 0.0)

    def test_summary_p95(self):
        summary = summarize(TRACES)
        self.assertEqual(summary.active_p95, 22000)
        self.assertEqual(summary.tool_p95, 9000)
        self.assertEqual(summary.ttft_p95, 900)

    def test_budget_violations(self):
        violations = check_budget(TRACES, BUDGETS)
        self.assertEqual(len(violations), 4)
        self.assertTrue(any(item.metric == "active_p95_ms" for item in violations))
        self.assertTrue(any(item.scope == "R03" for item in violations))

    def test_tail_amplification(self):
        self.assertAlmostEqual(tail_amplification(0.01, 100), 0.633967659, places=6)
        self.assertEqual(tail_amplification(0.0, 100), 0.0)
        self.assertEqual(tail_amplification(1.0, 3), 1.0)

    def test_tail_amplification_validates(self):
        with self.assertRaises(ValueError):
            tail_amplification(-0.1, 5)
        with self.assertRaises(ValueError):
            tail_amplification(0.1, -1)


class DashboardTests(unittest.TestCase):
    def test_real_dashboard_is_blocked(self):
        dash = build_dashboard(TRACES, LEDGER, BUDGETS)
        self.assertEqual(dash.verdict, BLOCK)

    def test_real_dashboard_has_integrity_alerts(self):
        dash = build_dashboard(TRACES, LEDGER, BUDGETS)
        codes = {alert.code for alert in dash.alerts}
        self.assertIn("missing_usage", codes)
        self.assertIn("reconciliation", codes)
        self.assertIn("latency_budget", codes)
        self.assertIn("cost_per_success", codes)
        self.assertIn("retry_share", codes)

    def test_unknown_model_blocks(self):
        trace = minimal_trace(model="ghost")
        ledger = Ledger([trace], PRICEBOOK)
        dash = build_dashboard([trace], ledger, {})
        self.assertEqual(dash.verdict, BLOCK)
        self.assertTrue(any(alert.code == "unknown_model" for alert in dash.alerts))

    def test_no_success_blocks_cost(self):
        trace = minimal_trace(success=False)
        ledger = Ledger([trace], PRICEBOOK)
        dash = build_dashboard([trace], ledger, {})
        self.assertEqual(dash.verdict, BLOCK)
        self.assertTrue(any(alert.code == "cost_per_success" for alert in dash.alerts))

    def test_clean_trace_passes(self):
        trace = minimal_trace()
        ledger = Ledger([trace], PRICEBOOK)
        dash = build_dashboard([trace], ledger, {"active_p95_ms": 10_000})
        self.assertEqual(dash.verdict, PASS)

    def test_cache_floor_warns(self):
        trace = minimal_trace()
        ledger = Ledger([trace], PRICEBOOK)
        dash = build_dashboard([trace], ledger, {"cache_hit_rate_min": 0.5})
        self.assertEqual(dash.verdict, WARN)
        self.assertTrue(any(alert.code == "cache_hit_rate" for alert in dash.alerts))


class CliTests(unittest.TestCase):
    def test_audit_exits_nonzero(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(obs_cli.main(["audit"]), 1)

    def test_trace_exits_zero(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(obs_cli.main(["trace", "--id", "R04"]), 0)
        self.assertIn("blocked 88.0s", output.getvalue())

    def test_unknown_trace_exits_two(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(obs_cli.main(["trace", "--id", "NOPE"]), 2)

    def test_dashboard_exits_nonzero_on_block(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(obs_cli.main(["dashboard"]), 1)

    def test_cost_and_latency_exit_zero(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(obs_cli.main(["cost"]), 0)
            self.assertEqual(obs_cli.main(["latency"]), 0)


if __name__ == "__main__":
    unittest.main()
