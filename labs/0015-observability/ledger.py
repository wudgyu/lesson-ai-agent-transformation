"""Token 账本：把 trace 变成可解释的账单。

三条规则：

1. 只从叶子 inference span 取 usage。agent span 的 usage 是汇总，
   对所有 span 求和会重复计费。
2. 失败调用也计费。失败 run 的成本必须进入分子；分母只放成功 run。
3. 缺失 usage 不估算。宁可在面板上显示 `unknown`，也不要猜一个
   看起来合理的数字——估算会掩盖 instrumentation 的 bug。

Python >= 3.9，仅标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pricing import Cost, PriceBook, UnknownModelError
from trace_schema import Span, Trace, Usage


@dataclass
class TraceCost:
    trace: Trace
    cost: Cost = field(default_factory=Cost)
    model_costs: Dict[str, Cost] = field(default_factory=dict)
    missing_usage: List[str] = field(default_factory=list)
    unknown_models: List[str] = field(default_factory=list)
    failed_cost: float = 0.0
    retry_extra_cost: float = 0.0
    failed_tokens: int = 0
    retry_extra_tokens: int = 0

    @property
    def complete(self) -> bool:
        return not self.missing_usage and not self.unknown_models

    @property
    def total(self) -> float:
        return self.cost.total

    @property
    def retry_cost(self) -> float:
        """失败调用 + 第 2 次及以后尝试的成本。

        失败的那次调用本来不需要发生；重试次数超过 1 的部分也是额外成本。
        这两个集合不重叠：失败 span 的 attempt 通常还是 1。
        """
        return self.failed_cost + self.retry_extra_cost

    @property
    def retry_tokens(self) -> int:
        return self.failed_tokens + self.retry_extra_tokens


def _trace_date(trace: Trace) -> Optional[str]:
    if trace.started_at:
        return trace.started_at[:10]
    return None


def compute_trace_cost(trace: Trace, pricebook: PriceBook) -> TraceCost:
    result = TraceCost(trace=trace)
    for span in trace.inference_spans:
        usage = span.usage
        if usage is None:
            result.missing_usage.append(span.span_id)
            continue
        model = span.model
        if not model:
            result.unknown_models.append(span.span_id)
            continue
        try:
            span_cost = pricebook.cost(usage, model, at=_trace_date(trace))
        except UnknownModelError:
            result.unknown_models.append(model)
            continue
        result.cost = result.cost + span_cost
        result.model_costs[model] = result.model_costs.get(model, Cost()) + span_cost

        if span.status == "error":
            result.failed_cost += span_cost.total
            result.failed_tokens += usage.total_tokens
        if span.is_retry:
            result.retry_extra_cost += span_cost.total
            result.retry_extra_tokens += usage.total_tokens
    return result


@dataclass
class ArmSummary:
    arm: str
    runs: int
    successes: int
    cost: float
    known_cost: bool
    cache_read_tokens: int
    input_tokens: int
    retry_cost: float

    @property
    def cost_per_success(self) -> Optional[float]:
        if self.successes == 0:
            return None
        return self.cost / self.successes

    @property
    def cache_hit_rate(self) -> float:
        if self.input_tokens == 0:
            return 0.0
        return self.cache_read_tokens / self.input_tokens


class Ledger:
    def __init__(self, traces: List[Trace], pricebook: PriceBook) -> None:
        self.traces = list(traces)
        self.pricebook = pricebook
        self.entries: Dict[str, TraceCost] = {
            trace.trace_id: compute_trace_cost(trace, pricebook)
            for trace in self.traces
        }

    def entry(self, trace_id: str) -> TraceCost:
        return self.entries[trace_id]

    @property
    def total(self) -> float:
        return sum(entry.cost.total for entry in self.entries.values())

    @property
    def complete(self) -> bool:
        return all(entry.complete for entry in self.entries.values())

    @property
    def successes(self) -> int:
        return sum(1 for trace in self.traces if trace.success)

    @property
    def runs(self) -> int:
        return len(self.traces)

    @property
    def cost_per_success(self) -> Optional[float]:
        if self.successes == 0:
            return None
        return self.total / self.successes

    @property
    def failed_run_cost(self) -> float:
        return sum(
            entry.cost.total
            for entry in self.entries.values()
            if not entry.trace.success
        )

    @property
    def retry_cost(self) -> float:
        return sum(entry.retry_cost for entry in self.entries.values())

    @property
    def retry_share(self) -> float:
        if self.total == 0:
            return 0.0
        return self.retry_cost / self.total

    def totals_usage(self) -> Usage:
        total = Usage()
        for trace in self.traces:
            usage, _missing = trace.billed_usage()
            total = total + usage
        return total

    @property
    def cache_hit_rate(self) -> float:
        return self.totals_usage().cache_hit_rate

    def by_model(self) -> Dict[str, Cost]:
        totals: Dict[str, Cost] = {}
        for entry in self.entries.values():
            for model, cost in entry.model_costs.items():
                totals[model] = totals.get(model, Cost()) + cost
        return totals

    def by_arm(self) -> Dict[str, ArmSummary]:
        grouped: Dict[str, List[TraceCost]] = {}
        for entry in self.entries.values():
            grouped.setdefault(entry.trace.arm, []).append(entry)
        summaries: Dict[str, ArmSummary] = {}
        for arm, entries in grouped.items():
            usage = Usage()
            for entry in entries:
                trace_usage, _missing = entry.trace.billed_usage()
                usage = usage + trace_usage
            summaries[arm] = ArmSummary(
                arm=arm,
                runs=len(entries),
                successes=sum(1 for entry in entries if entry.trace.success),
                cost=sum(entry.cost.total for entry in entries),
                known_cost=all(entry.complete for entry in entries),
                cache_read_tokens=usage.cache_read_input_tokens,
                input_tokens=usage.input_tokens,
                retry_cost=sum(entry.retry_cost for entry in entries),
            )
        return summaries

    def top_runs(self, limit: int = 5) -> List[TraceCost]:
        return sorted(
            self.entries.values(), key=lambda entry: entry.cost.total, reverse=True
        )[:limit]

    @staticmethod
    def context_growth(trace: Trace) -> Dict[str, float]:
        """看输入 token 是否随着轮次增长，以及历史被重复计费的比例。

        `rebilled_ratio = 所有轮次 input 之和 / 最后一轮 input`。
        如果每轮都重发完整历史，这个值会接近 T/2；它不是“unique tokens”，
        而是账单上真实发生的输入 token 总量。
        """
        llm_spans = sorted(trace.inference_spans, key=lambda span: span.start_ms)
        inputs = [span.usage.input_tokens for span in llm_spans if span.usage]
        if not inputs:
            return {
                "turns": 0,
                "first_input": 0,
                "last_input": 0,
                "total_input": 0,
                "growth_ratio": 0.0,
                "rebilled_ratio": 0.0,
            }
        first = inputs[0]
        last = inputs[-1]
        total = sum(inputs)
        return {
            "turns": float(len(inputs)),
            "first_input": float(first),
            "last_input": float(last),
            "total_input": float(total),
            "growth_ratio": (last / first) if first else 0.0,
            "rebilled_ratio": (total / last) if last else 0.0,
        }
