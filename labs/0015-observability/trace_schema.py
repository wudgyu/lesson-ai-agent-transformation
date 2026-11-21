"""Trace 数据契约：trace / span / usage。

这一课的核心主张：可观测性不是“多打日志”，而是把一次 run 拆成一棵
可归属的树——每个 span 有父子、起止、状态与属性，token 与耗时都能落到
具体的 operation 上。

Schema 尽量贴近 OpenTelemetry GenAI semantic conventions（2026-11 仍为
Development）：`invoke_agent`、`chat`、`execute_tool`、`gen_ai.usage.*`。
属性名保留英文原文，避免翻译造成歧义。

Python >= 3.9，仅标准库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SPAN_KINDS = {"agent", "llm", "tool", "approval", "retrieval", "guardrail"}
SPAN_STATUSES = {"ok", "error"}

# OTel GenAI usage 属性。cache_read / cache_write / reasoning 都是
# 总量（input_tokens / output_tokens）的子集，不是额外加项。
USAGE_KEYS = {
    "gen_ai.usage.input_tokens": "input_tokens",
    "gen_ai.usage.output_tokens": "output_tokens",
    "gen_ai.usage.cache_read.input_tokens": "cache_read_input_tokens",
    "gen_ai.usage.cache_write.input_tokens": "cache_write_input_tokens",
    "gen_ai.usage.reasoning.output_tokens": "reasoning_output_tokens",
}


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an int, got %r" % (field_name, value))
    return value


@dataclass(frozen=True)
class Usage:
    """一次 operation 的 token 用量。

    `input_tokens` 包含 cache_read 与 cache_write；`output_tokens` 包含
    reasoning。这个“包含关系”是账本不重复计费的前提。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    reasoning_output_tokens: int = 0

    @staticmethod
    def from_attrs(attrs: Dict[str, Any]) -> Optional["Usage"]:
        present = [key for key in USAGE_KEYS if key in attrs]
        if not present:
            return None
        values: Dict[str, int] = {}
        for key in USAGE_KEYS:
            values[USAGE_KEYS[key]] = 0
        for key in present:
            values[USAGE_KEYS[key]] = _as_int(attrs[key], key)
        return Usage(**values)

    def validate(self, where: str) -> List[str]:
        errors: List[str] = []
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cache_read_input_tokens", self.cache_read_input_tokens),
            ("cache_write_input_tokens", self.cache_write_input_tokens),
            ("reasoning_output_tokens", self.reasoning_output_tokens),
        ):
            if value < 0:
                errors.append("%s: %s is negative" % (where, name))
        if self.cache_read_input_tokens + self.cache_write_input_tokens > self.input_tokens:
            errors.append(
                "%s: cache_read + cache_write exceeds input_tokens "
                "(they are subsets of input, not extra tokens)" % where
            )
        if self.reasoning_output_tokens > self.output_tokens:
            errors.append("%s: reasoning_output_tokens exceeds output_tokens" % where)
        return errors

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens
            + other.cache_read_input_tokens,
            cache_write_input_tokens=self.cache_write_input_tokens
            + other.cache_write_input_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens
            + other.reasoning_output_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        if self.input_tokens == 0:
            return 0.0
        return self.cache_read_input_tokens / self.input_tokens


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    name: str
    kind: str
    start_ms: int
    end_ms: int
    status: str = "ok"
    attrs: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Span":
        required = ("trace_id", "span_id", "name", "kind", "start_ms", "end_ms")
        for key in required:
            if key not in raw:
                raise ValueError("span missing %s: %r" % (key, raw))
        kind = str(raw["kind"])
        if kind not in SPAN_KINDS:
            raise ValueError("span %s: unknown kind %r" % (raw["span_id"], kind))
        status = str(raw.get("status", "ok"))
        if status not in SPAN_STATUSES:
            raise ValueError("span %s: unknown status %r" % (raw["span_id"], status))
        start_ms = _as_int(raw["start_ms"], "start_ms")
        end_ms = _as_int(raw["end_ms"], "end_ms")
        if end_ms < start_ms:
            raise ValueError("span %s: end_ms < start_ms" % raw["span_id"])
        attrs = raw.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise ValueError("span %s: attrs must be an object" % raw["span_id"])
        span = Span(
            trace_id=str(raw["trace_id"]),
            span_id=str(raw["span_id"]),
            parent_id=None if raw.get("parent_id") is None else str(raw["parent_id"]),
            name=str(raw["name"]),
            kind=kind,
            start_ms=start_ms,
            end_ms=end_ms,
            status=status,
            attrs=dict(attrs),
        )
        usage = span.usage
        if usage is not None:
            errors = usage.validate("span %s" % span.span_id)
            if errors:
                raise ValueError("; ".join(errors))
        return span

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def operation_name(self) -> str:
        return str(self.attrs.get("gen_ai.operation.name", ""))

    @property
    def model(self) -> Optional[str]:
        value = self.attrs.get("gen_ai.request.model") or self.attrs.get(
            "gen_ai.response.model"
        )
        return None if value is None else str(value)

    @property
    def tool_name(self) -> Optional[str]:
        value = self.attrs.get("gen_ai.tool.name")
        return None if value is None else str(value)

    @property
    def agent_name(self) -> Optional[str]:
        value = self.attrs.get("gen_ai.agent.name")
        return None if value is None else str(value)

    @property
    def usage(self) -> Optional[Usage]:
        return Usage.from_attrs(self.attrs)

    @property
    def is_retry(self) -> bool:
        """应用自定义属性：这是第几次尝试。

        标准属性不表达“重试”，所以这里用 `app.attempt`。第 1 次尝试
        失败后仍可能计费，不能只统计成功调用。
        """
        attempt = self.attrs.get("app.attempt", 1)
        return isinstance(attempt, int) and attempt > 1

    @property
    def is_inference(self) -> bool:
        return self.kind == "llm"

    def uses_usage(self) -> bool:
        return self.usage is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "status": self.status,
            "attrs": self.attrs,
        }


@dataclass
class Trace:
    trace_id: str
    run_id: str
    case_id: str
    arm: str
    success: bool
    spans: List[Span]
    started_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Trace":
        required = ("trace_id", "run_id", "case_id", "arm", "success", "spans")
        for key in required:
            if key not in raw:
                raise ValueError("trace missing %s: %r" % (key, raw))
        spans = [Span.from_dict(item) for item in raw["spans"]]
        trace = Trace(
            trace_id=str(raw["trace_id"]),
            run_id=str(raw["run_id"]),
            case_id=str(raw["case_id"]),
            arm=str(raw["arm"]),
            success=bool(raw["success"]),
            spans=spans,
            started_at=str(raw.get("started_at", "")),
            metadata=dict(raw.get("metadata") or {}),
        )
        errors = trace.validate()
        if errors:
            raise ValueError("trace %s: %s" % (trace.trace_id, "; ".join(errors)))
        return trace

    # -- tree -------------------------------------------------------------

    @property
    def root(self) -> Span:
        roots = [span for span in self.spans if span.parent_id is None]
        if len(roots) != 1:
            raise ValueError("trace %s: expected exactly one root" % self.trace_id)
        return roots[0]

    def span_by_id(self, span_id: str) -> Span:
        for span in self.spans:
            if span.span_id == span_id:
                return span
        raise KeyError(span_id)

    def children_of(self, span_id: Optional[str]) -> List[Span]:
        children = [span for span in self.spans if span.parent_id == span_id]
        children.sort(key=lambda span: (span.start_ms, span.span_id))
        return children

    def walk(self) -> List[Span]:
        order: List[Span] = []

        def visit(span: Span) -> None:
            order.append(span)
            for child in self.children_of(span.span_id):
                visit(child)

        visit(self.root)
        return order

    def leaves(self) -> List[Span]:
        ids_with_children = {span.parent_id for span in self.spans if span.parent_id}
        return [span for span in self.spans if span.span_id not in ids_with_children]

    # -- usage ------------------------------------------------------------

    @property
    def inference_spans(self) -> List[Span]:
        """真正应该计费的 inference span。

        agent span 上的 usage 是子调用的汇总；如果对所有带 usage 的 span
        求和，就会把同一批 token 算两遍。账本只从叶子 inference span 取数。
        """
        return [span for span in self.spans if span.is_inference]

    def billed_usage(self) -> Tuple[Usage, Set[str]]:
        total = Usage()
        missing: Set[str] = set()
        for span in self.inference_spans:
            usage = span.usage
            if usage is None:
                missing.add(span.span_id)
                continue
            total = total + usage
        return total, missing

    def agent_usage(self) -> Optional[Usage]:
        return self.root.usage

    def reconciliation(self) -> Dict[str, Any]:
        """根 agent 的汇总 usage 与子 inference 之和是否一致。

        不一致通常意味着 instrumentation 在某处重复上报、漏报，或者
        汇总口径不同。账本必须先解释差异，不能静默取一个数。
        """
        leaf_usage, missing = self.billed_usage()
        root_usage = self.agent_usage()
        if root_usage is None:
            return {
                "status": "missing_aggregate",
                "delta_input": None,
                "delta_output": None,
                "missing_spans": sorted(missing),
            }
        if missing:
            return {
                "status": "incomplete",
                "delta_input": None,
                "delta_output": None,
                "missing_spans": sorted(missing),
            }
        delta_input = root_usage.input_tokens - leaf_usage.input_tokens
        delta_output = root_usage.output_tokens - leaf_usage.output_tokens
        status = "ok" if delta_input == 0 and delta_output == 0 else "mismatch"
        return {
            "status": status,
            "delta_input": delta_input,
            "delta_output": delta_output,
            "missing_spans": [],
        }

    # -- validation -------------------------------------------------------

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.trace_id:
            errors.append("trace_id is empty")
        if not self.run_id:
            errors.append("%s: run_id is empty" % self.trace_id)
        if not self.case_id:
            errors.append("%s: case_id is empty" % self.trace_id)
        if not self.spans:
            errors.append("%s: no spans" % self.trace_id)
            return errors

        seen: Dict[str, Span] = {}
        for span in self.spans:
            if span.trace_id != self.trace_id:
                errors.append(
                    "%s: span %s belongs to trace %s"
                    % (self.trace_id, span.span_id, span.trace_id)
                )
            if span.span_id in seen:
                errors.append("%s: duplicate span_id %s" % (self.trace_id, span.span_id))
            seen[span.span_id] = span

        roots = [span for span in self.spans if span.parent_id is None]
        if len(roots) != 1:
            errors.append(
                "%s: expected exactly one root, got %d" % (self.trace_id, len(roots))
            )
            return errors
        if roots[0].kind != "agent":
            errors.append("%s: root must be an agent span" % self.trace_id)

        for span in self.spans:
            if span.parent_id is not None:
                parent = seen.get(span.parent_id)
                if parent is None:
                    errors.append(
                        "%s: span %s has unknown parent %s"
                        % (self.trace_id, span.span_id, span.parent_id)
                    )
                    continue
                if span.start_ms < parent.start_ms or span.end_ms > parent.end_ms:
                    errors.append(
                        "%s: span %s is not contained in parent %s"
                        % (self.trace_id, span.span_id, parent.span_id)
                    )
            if span.kind == "llm":
                if not span.operation_name:
                    errors.append(
                        "%s: llm span %s lacks gen_ai.operation.name"
                        % (self.trace_id, span.span_id)
                    )
                if not span.model:
                    errors.append(
                        "%s: llm span %s lacks gen_ai.request.model"
                        % (self.trace_id, span.span_id)
                    )

        # cycle detection: every span must reach the root in <= n steps.
        n = len(self.spans)
        for span in self.spans:
            cursor = span
            steps = 0
            while cursor.parent_id is not None:
                if cursor.parent_id not in seen:
                    break
                cursor = seen[cursor.parent_id]
                steps += 1
                if steps > n:
                    errors.append("%s: cycle at span %s" % (self.trace_id, span.span_id))
                    break

        return errors


def load_traces(path: Path) -> List[Trace]:
    traces: List[Trace] = []
    seen: Set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("%s:%d invalid JSON: %s" % (path, line_no, exc))
            trace = Trace.from_dict(raw)
            if trace.trace_id in seen:
                raise ValueError("%s:%d duplicate trace_id %s" % (path, line_no, trace.trace_id))
            seen.add(trace.trace_id)
            traces.append(trace)
    return traces


def group_by_arm(traces: List[Trace]) -> Dict[str, List[Trace]]:
    grouped: Dict[str, List[Trace]] = {}
    for trace in traces:
        grouped.setdefault(trace.arm, []).append(trace)
    for arm in grouped:
        grouped[arm].sort(key=lambda trace: trace.trace_id)
    return grouped
