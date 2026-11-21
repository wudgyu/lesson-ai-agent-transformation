"""延迟分析：wall、active、critical path。

三个时间不是一回事：

- wall：用户从发起请求到拿到答案的墙钟时间。
- active：wall 减去等人审批（approval）的时间。审批慢不是模型慢。
- critical path：决定端到端时间的那条依赖链。并行工具的总耗时相加会骗人；
  只有最长路径决定结束时间。

实现方式：把 span 树变成 DAG。每个 span 的权重是“独占时间”
（duration - 直接子 span 区间的并集）；父 → 子加边，同一父节点下
按时间先后给顺序执行的兄弟加边。最长路径就是 critical path。
并行兄弟之间没有先后边，所以它们不会相加。

Python >= 3.9，仅标准库。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from trace_schema import Span, Trace

NON_AGENT_KINDS = ("llm", "tool", "retrieval", "guardrail", "approval")
STAGE_KINDS = NON_AGENT_KINDS + ("agent",)


def interval_union_ms(intervals: Iterable[Tuple[int, int]]) -> int:
    """一组区间的并集长度；重叠只算一次。"""
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return 0
    total = 0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start > current_end:
            total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    total += current_end - current_start
    return total


def exclusive_ms(span: Span, children: Sequence[Span]) -> int:
    """span 自己占用的时间，不含直接子 span 覆盖的区间。"""
    clipped = [
        (max(child.start_ms, span.start_ms), min(child.end_ms, span.end_ms))
        for child in children
    ]
    busy = interval_union_ms(clipped)
    return max(0, span.duration_ms - busy)


def _children_map(trace: Trace) -> Dict[Optional[str], List[Span]]:
    children: Dict[Optional[str], List[Span]] = {}
    for span in trace.spans:
        children.setdefault(span.parent_id, []).append(span)
    for key in children:
        children[key].sort(key=lambda span: (span.start_ms, span.end_ms, span.span_id))
    return children


def _leaf_descendants(
    span: Span,
    children: Dict[Optional[str], List[Span]],
    memo: Dict[str, List[str]],
) -> List[str]:
    if span.span_id in memo:
        return memo[span.span_id]
    kids = children.get(span.span_id, [])
    if not kids:
        result = [span.span_id]
    else:
        result = []
        for child in kids:
            result.extend(_leaf_descendants(child, children, memo))
    memo[span.span_id] = result
    return result


def _build_dag(trace: Trace, children: Dict[Optional[str], List[Span]]):
    """parent → child；顺序执行的兄弟，从“前一个子树的所有叶子”连到后一个。

    只连 sibling → sibling 不够：如果前一个 sibling 自己是一棵子树，
    路径需要先走完它的叶子，才能接到下一个 sibling。否则嵌套 agent
    后面的步骤会被漏掉。
    """
    edges: Dict[str, List[str]] = {
        span.span_id: [] for span in trace.spans
    }
    weights: Dict[str, int] = {}
    for span in trace.spans:
        weights[span.span_id] = exclusive_ms(
            span, children.get(span.span_id, [])
        )
        for child in children.get(span.span_id, []):
            edges[span.span_id].append(child.span_id)

    leaf_memo: Dict[str, List[str]] = {}
    for parent_id, siblings in children.items():
        if parent_id is None:
            continue
        for i, earlier in enumerate(siblings):
            for later in siblings[i + 1 :]:
                if earlier.end_ms <= later.start_ms:
                    for leaf_id in _leaf_descendants(earlier, children, leaf_memo):
                        edges[leaf_id].append(later.span_id)
    return edges, weights


def critical_path(trace: Trace) -> Tuple[int, List[Span]]:
    """返回 (critical_path_ms, span 列表)，从 root 到叶子。"""
    children = _children_map(trace)
    edges, weights = _build_dag(trace, children)
    memo: Dict[str, Tuple[int, List[str]]] = {}
    visiting = set()

    def best(span_id: str) -> Tuple[int, List[str]]:
        if span_id in memo:
            return memo[span_id]
        if span_id in visiting:
            raise ValueError("cycle in span DAG at %s" % span_id)
        visiting.add(span_id)
        best_cost = 0
        best_tail: List[str] = []
        for next_id in edges[span_id]:
            next_cost, next_tail = best(next_id)
            if next_cost > best_cost:
                best_cost = next_cost
                best_tail = next_tail
        visiting.remove(span_id)
        result = (weights[span_id] + best_cost, [span_id] + best_tail)
        memo[span_id] = result
        return result

    cost, ids = best(trace.root.span_id)
    by_id = {span.span_id: span for span in trace.spans}
    return cost, [by_id[span_id] for span_id in ids]


@dataclass
class TraceLatency:
    trace_id: str
    wall_ms: int
    blocked_ms: int
    active_ms: int
    critical_path_ms: int
    active_critical_path_ms: int
    path: List[Span] = field(default_factory=list)
    stage_on_path_ms: Dict[str, int] = field(default_factory=dict)
    stage_union_ms: Dict[str, int] = field(default_factory=dict)
    overhead_ms: int = 0
    ttft_ms: Optional[int] = None

    @property
    def approval_on_path_ms(self) -> int:
        return self.stage_on_path_ms.get("approval", 0)


def analyze_trace(trace: Trace) -> TraceLatency:
    root = trace.root
    children = _children_map(trace)
    path_cost, path = critical_path(trace)

    approval_spans = [span for span in trace.spans if span.kind == "approval"]
    blocked = interval_union_ms(
        (span.start_ms, span.end_ms) for span in approval_spans
    )
    active = max(0, root.duration_ms - blocked)

    stage_on_path = {kind: 0 for kind in STAGE_KINDS}
    for span in path:
        stage_on_path[span.kind] += exclusive_ms(
            span, children.get(span.span_id, [])
        )

    stage_union = {}
    for kind in NON_AGENT_KINDS:
        stage_union[kind] = interval_union_ms(
            (span.start_ms, span.end_ms)
            for span in trace.spans
            if span.kind == kind
        )
    non_agent_union = interval_union_ms(
        (span.start_ms, span.end_ms)
        for span in trace.spans
        if span.kind != "agent"
    )
    overhead = max(0, root.duration_ms - non_agent_union)

    ttfts = [
        int(span.attrs["app.ttft_ms"])
        for span in trace.inference_spans
        if isinstance(span.attrs.get("app.ttft_ms"), int)
    ]

    # critical path 上 approval 的独占时间；active critical path 从路径里扣掉它。
    approval_on_path = sum(
        exclusive_ms(span, children.get(span.span_id, []))
        for span in path
        if span.kind == "approval"
    )
    return TraceLatency(
        trace_id=trace.trace_id,
        wall_ms=root.duration_ms,
        blocked_ms=blocked,
        active_ms=active,
        critical_path_ms=path_cost,
        active_critical_path_ms=max(0, path_cost - approval_on_path),
        path=path,
        stage_on_path_ms=stage_on_path,
        stage_union_ms=stage_union,
        overhead_ms=overhead,
        ttft_ms=max(ttfts) if ttfts else None,
    )


def percentile(values: Sequence[float], q: float) -> float:
    """nearest-rank percentile，q ∈ [0, 100]。

    样本少的时候不要用插值假装精确；nearest-rank 的语义是
    “至少有 q% 的样本小于等于该值”。
    """
    if not values:
        return 0.0
    if q <= 0:
        return float(min(values))
    if q >= 100:
        return float(max(values))
    ordered = sorted(values)
    rank = int(math.ceil(q / 100.0 * len(ordered)))
    index = min(len(ordered) - 1, max(0, rank - 1))
    return float(ordered[index])


@dataclass
class LatencySummary:
    runs: int
    wall_p50: float
    wall_p95: float
    wall_p99: float
    active_p50: float
    active_p95: float
    active_p99: float
    critical_p95: float
    tool_p95: float
    approval_p95: float
    ttft_p95: float


def summarize(traces: List[Trace]) -> LatencySummary:
    latencies = [analyze_trace(trace) for trace in traces]
    wall = [item.wall_ms for item in latencies]
    active = [item.active_ms for item in latencies]
    critical = [item.critical_path_ms for item in latencies]
    tool = [item.stage_union_ms.get("tool", 0) for item in latencies]
    approval = [item.blocked_ms for item in latencies]
    ttft = [item.ttft_ms for item in latencies if item.ttft_ms is not None]
    return LatencySummary(
        runs=len(latencies),
        wall_p50=percentile(wall, 50),
        wall_p95=percentile(wall, 95),
        wall_p99=percentile(wall, 99),
        active_p50=percentile(active, 50),
        active_p95=percentile(active, 95),
        active_p99=percentile(active, 99),
        critical_p95=percentile(critical, 95),
        tool_p95=percentile(tool, 95),
        approval_p95=percentile(approval, 95),
        ttft_p95=percentile(ttft, 95),
    )


@dataclass
class Violation:
    scope: str
    metric: str
    value: float
    limit: float

    def __str__(self) -> str:
        return "%s: %s %.1f > %.1f" % (self.scope, self.metric, self.value, self.limit)


def check_budget(traces: List[Trace], budget: Dict[str, float]) -> List[Violation]:
    """检查全局 SLO 与单 run 上限。

    budget 的 key 使用毫秒：`active_p95_ms`、`max_active_ms`、
    `tool_p95_ms`、`approval_p95_ms`、`ttft_p95_ms`。
    """
    latencies = [analyze_trace(trace) for trace in traces]
    violations: List[Violation] = []
    summary = summarize(traces)

    checks = (
        ("active_p95_ms", summary.active_p95, "global"),
        ("tool_p95_ms", summary.tool_p95, "global"),
        ("approval_p95_ms", summary.approval_p95, "global"),
        ("ttft_p95_ms", summary.ttft_p95, "global"),
    )
    for key, value, scope in checks:
        limit = budget.get(key)
        if limit is not None and value > limit:
            violations.append(Violation(scope, key, value, float(limit)))

    max_active = budget.get("max_active_ms")
    if max_active is not None:
        for item in latencies:
            if item.active_ms > max_active:
                violations.append(
                    Violation(item.trace_id, "active_ms", item.active_ms, float(max_active))
                )
    return violations


def tail_amplification(p_slow: float, fanout: int) -> float:
    """fan-out 后“至少一个叶子慢”的概率。

    p_slow 是单个叶子的慢请求概率，fanout 是并行叶子数。
    1 - (1 - p)^n。1% 的叶子慢，100 个叶子就有 63.4% 的请求慢。
    """
    if fanout < 0:
        raise ValueError("fanout must be >= 0")
    if not 0 <= p_slow <= 1:
        raise ValueError("p_slow must be in [0, 1]")
    return 1.0 - (1.0 - p_slow) ** fanout
