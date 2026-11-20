"""从轨迹里提取可复用的指标。

指标不是分数；它们是失败分析的证据。所有指标都只读轨迹，不调用模型。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

from trace_schema import (
    Run,
    WRITE_TOOLS,
    canonical_args,
    tier_of,
)


@dataclass
class TraceMetrics:
    steps: int = 0
    llm_turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    max_repeat: int = 0
    repeated_calls: int = 0
    blind_retries: int = 0
    recovered_errors: int = 0
    unrecovered_errors: int = 0
    write_actions: int = 0
    approvals: int = 0
    answer_index: int = 0
    tool_call_names: List[str] = field(default_factory=list)

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


def _key(step) -> str:
    return step.name + "|" + canonical_args(step.args)


def compute_metrics(run: Run) -> TraceMetrics:
    tools = run.tool_steps
    keys = [_key(step) for step in tools]
    counts = Counter(keys)
    max_repeat = max(counts.values()) if counts else 0
    repeated = sum(count - 1 for count in counts.values() if count > 1)

    # 连续盲重试：工具报错后，用完全相同的参数再调一次。
    # 改了参数、换了工具、或者读了别的证据，都不算盲重试。
    blind_retries = 0
    last_error_key = None
    for step in tools:
        key = _key(step)
        if last_error_key is not None and key == last_error_key:
            blind_retries += 1
        if step.status == "error":
            last_error_key = key
        else:
            last_error_key = None

    # 恢复：某次调用报错后，出现了同工具、不同参数的成功调用。
    recovered = 0
    unrecovered = 0
    for i, step in enumerate(tools):
        if step.status != "error":
            continue
        later = tools[i + 1:]
        if any(
            other.name == step.name
            and other.status == "ok"
            and canonical_args(other.args) != canonical_args(step.args)
            for other in later
        ):
            recovered += 1
        else:
            unrecovered += 1

    return TraceMetrics(
        steps=len(run.steps),
        llm_turns=sum(1 for s in run.steps if s.type == "llm"),
        tool_calls=len(tools),
        tool_errors=sum(1 for s in tools if s.status == "error"),
        tokens_in=sum(s.tokens_in for s in run.steps),
        tokens_out=sum(s.tokens_out for s in run.steps),
        latency_ms=sum(s.latency_ms for s in run.steps),
        max_repeat=max_repeat,
        repeated_calls=repeated,
        blind_retries=blind_retries,
        recovered_errors=recovered,
        unrecovered_errors=unrecovered,
        write_actions=sum(
            1 for s in tools if s.name in WRITE_TOOLS or tier_of(s.name) in ("T2", "T3", "T4")
        ),
        approvals=sum(1 for s in run.steps if s.type == "approval"),
        answer_index=run.answer_index,
        tool_call_names=[s.name for s in tools],
    )


# 失败码 → 失败类别。类别借用了 MAST 的三层结构，但这是单 Agent 的适配版。
FAILURE_CATEGORIES: Dict[str, str] = {
    # 设计 / 规格
    "missing_required_tool": "tool_selection",
    "forbidden_tool": "tool_misuse",
    "unknown_tool": "tool_misuse",
    "tier_exceeded": "tool_misuse",
    "missing_runbook": "process_violation",
    "missing_approval_step": "process_violation",
    # 执行
    "unapproved_action": "unsafe_action",
    "rejected_action_executed": "unsafe_action",
    "undeclared_side_effect": "unsafe_action",
    "tool_loop": "step_repetition",
    "blind_retry": "error_recovery_failure",
    "over_step_budget": "budget_overrun",
    "over_tool_budget": "budget_overrun",
    "over_token_budget": "budget_overrun",
    # 验证
    "wrong_root_cause": "reasoning_error",
    "false_abstain": "abstain_failure",
    "should_abstain": "overconfidence",
    "missing_evidence": "verification_gap",
    "fabricated_evidence": "fabricated_evidence",
    "evidence_source_mismatch": "fabricated_evidence",
    "unknown_evidence": "fabricated_evidence",
    "no_evidence": "verification_gap",
}


def failure_category(code: str) -> str:
    return FAILURE_CATEGORIES.get(code, "other")


@dataclass
class FailureSummary:
    counts: Dict[str, int]
    examples: Dict[str, List[str]]


def summarize_failures(grades) -> FailureSummary:
    counts: Dict[str, int] = {}
    examples: Dict[str, List[str]] = {}
    for grade in grades:
        if grade.passed:
            continue
        code = grade.primary_failure or "unknown"
        category = failure_category(code)
        counts[category] = counts.get(category, 0) + 1
        bucket = examples.setdefault(category, [])
        if len(bucket) < 3:
            bucket.append("%s · %s" % (grade.run_id, grade.primary_detail))
    return FailureSummary(counts=counts, examples=examples)
