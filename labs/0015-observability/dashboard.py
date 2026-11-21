"""可观测性面板：把 trace、账本、延迟合成一张发布视图。

面板不是“把所有指标画出来”，而是回答四个问题：

1. 数据可信吗？（usage 缺失、汇总不一致、未知价格 → fail closed）
2. 钱花在哪？（cost per success、cache、retry）
3. 慢在哪？（active p95、critical path、approval wait）
4. 哪些是硬阻断，哪些只是观察项？

Python >= 3.9，仅标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from latency import LatencySummary, Violation, check_budget, summarize
from ledger import Ledger
from trace_schema import Trace

PASS = "PASS"
WARN = "WARN"
BLOCK = "BLOCK"
LEVEL_ORDER = {PASS: 0, WARN: 1, BLOCK: 2}


@dataclass
class Alert:
    level: str
    code: str
    message: str


@dataclass
class Dashboard:
    alerts: List[Alert] = field(default_factory=list)
    latency: LatencySummary = None  # type: ignore[assignment]
    violations: List[Violation] = field(default_factory=list)
    verdict: str = PASS

    def add(self, level: str, code: str, message: str) -> None:
        self.alerts.append(Alert(level, code, message))

    def finalize(self) -> "Dashboard":
        worst = PASS
        for alert in self.alerts:
            if LEVEL_ORDER[alert.level] > LEVEL_ORDER[worst]:
                worst = alert.level
        self.verdict = worst
        return self


def build_dashboard(
    traces: List[Trace], ledger: Ledger, budget: Dict[str, float]
) -> Dashboard:
    dash = Dashboard()
    dash.latency = summarize(traces)
    dash.violations = check_budget(traces, budget)

    # -- 1. integrity ----------------------------------------------------
    missing = [
        "%s:%s" % (entry.trace.trace_id, span_id)
        for entry in ledger.entries.values()
        for span_id in entry.missing_usage
    ]
    if missing:
        dash.add(
            BLOCK,
            "missing_usage",
            "%d inference span(s) without usage: %s"
            % (len(missing), ", ".join(sorted(missing)[:6])),
        )
    else:
        dash.add(PASS, "missing_usage", "all inference spans report usage")

    mismatches = []
    for trace in traces:
        rec = trace.reconciliation()
        if rec["status"] == "mismatch":
            mismatches.append(
                "%s (input %+d, output %+d)"
                % (trace.trace_id, rec["delta_input"], rec["delta_output"])
            )
        elif rec["status"] == "missing_aggregate":
            mismatches.append("%s (agent span has no aggregate usage)" % trace.trace_id)
    if mismatches:
        dash.add(
            BLOCK,
            "reconciliation",
            "agent aggregate != sum(inference spans): %s" % ", ".join(mismatches),
        )
    else:
        dash.add(PASS, "reconciliation", "agent totals reconcile with inference spans")

    unknown = [
        "%s:%s" % (entry.trace.trace_id, name)
        for entry in ledger.entries.values()
        for name in entry.unknown_models
    ]
    if unknown:
        dash.add(
            BLOCK,
            "unknown_model",
            "%d span(s) without a matching price: %s"
            % (len(unknown), ", ".join(sorted(unknown)[:6])),
        )
    else:
        dash.add(PASS, "unknown_model", "every billed model has a price")

    # -- 2. latency ------------------------------------------------------
    if dash.violations:
        for violation in dash.violations:
            dash.add(BLOCK, "latency_budget", str(violation))
    else:
        dash.add(PASS, "latency_budget", "active / tool / approval p95 within budget")

    # -- 3. cost ---------------------------------------------------------
    cost_limit = budget.get("cost_per_success_usd")
    cost_per_success = ledger.cost_per_success
    if cost_per_success is None:
        dash.add(BLOCK, "cost_per_success", "no successful run; cost per success is undefined")
    elif cost_limit is not None and cost_per_success > cost_limit:
        dash.add(
            WARN,
            "cost_per_success",
            "$%.4f per success > $%.4f budget" % (cost_per_success, cost_limit),
        )
    else:
        dash.add(
            PASS,
            "cost_per_success",
            "$%.4f per success" % cost_per_success,
        )

    cache_floor = budget.get("cache_hit_rate_min")
    if cache_floor is not None and ledger.cache_hit_rate < cache_floor:
        dash.add(
            WARN,
            "cache_hit_rate",
            "%.1f%% < %.1f%% floor" % (ledger.cache_hit_rate * 100, cache_floor * 100),
        )
    else:
        dash.add(
            PASS,
            "cache_hit_rate",
            "%.1f%% of input tokens served from cache" % (ledger.cache_hit_rate * 100),
        )

    retry_limit = budget.get("retry_share_max")
    if retry_limit is not None and ledger.retry_share > retry_limit:
        dash.add(
            WARN,
            "retry_share",
            "%.1f%% of cost is failure/retry > %.1f%%"
            % (ledger.retry_share * 100, retry_limit * 100),
        )
    else:
        dash.add(
            PASS,
            "retry_share",
            "%.1f%% of cost is failure/retry" % (ledger.retry_share * 100),
        )

    return dash.finalize()
