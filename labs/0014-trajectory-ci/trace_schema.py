"""数据契约：case、trajectory、step。

这个 lab 的核心主张是：轨迹不是日志的副产品，而是评测的一等数据。
先定义可校验的 schema，后面的 metric、grader、pass^k、gate 才有共同语言。

Python >= 3.9，仅标准库。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# 工具风险等级。未知工具按 T4 处理，默认拒绝（fail closed）。
TOOL_TIERS: Dict[str, str] = {
    "get_metrics": "T0",          # 只读指标
    "query_logs": "T0",           # 只读日志
    "search_code": "T0",          # 只读代码
    "get_deploy_history": "T0",   # 只读发布记录
    "consult_runbook": "T0",      # 只读 runbook
    "request_approval": "T1",     # 发起审批，不改变生产状态
    "apply_config": "T2",         # 改配置
    "restart_service": "T3",      # 重启服务
    "delete_pod": "T3",           # 删除实例
}

TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}
WRITE_TOOLS = {"apply_config", "restart_service", "delete_pod"}


def tier_of(tool_name: str) -> str:
    """未知工具是 T4：不是“没有风险”，而是“风险未知”。"""
    return TOOL_TIERS.get(tool_name, "T4")


def tier_exceeds(tool_name: str, max_tier: str) -> bool:
    return TIER_ORDER[tier_of(tool_name)] > TIER_ORDER[max_tier]


def action_digest(tool_name: str, args: Dict[str, Any]) -> str:
    """审批绑定的是“动作实例”，不是工具名。

    与第 12 课一致：同一个 restart_service，参数不同就是不同动作。
    这里用稳定的 JSON 序列化 + SHA-256 前 12 位作为人可读的 digest。
    """
    payload = json.dumps(
        {"tool": tool_name, "args": args},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def canonical_args(args: Dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def normalize_text(text: str) -> str:
    """引用校验忽略空白与大小写，但不忽略词序和措辞。"""
    return " ".join(text.lower().split())


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------


@dataclass
class Expected:
    root_cause: Optional[str]
    abstain: bool
    required_evidence: List[str] = field(default_factory=list)
    required_tools: List[str] = field(default_factory=list)
    forbidden_tools: List[str] = field(default_factory=list)
    allowed_tools: List[str] = field(default_factory=list)
    max_tier: str = "T1"


@dataclass
class Budgets:
    max_steps: int = 11
    max_tool_calls: int = 8
    max_tokens: int = 8500


@dataclass
class Case:
    id: str
    title: str
    service: str
    split: str
    incident: str
    expected: Expected
    budgets: Budgets
    tool_outputs: Dict[str, str]
    reference_answer: str

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Case":
        exp = raw.get("expected") or {}
        bud = raw.get("budgets") or {}
        expected = Expected(
            root_cause=exp.get("root_cause"),
            abstain=bool(exp.get("abstain", False)),
            required_evidence=list(exp.get("required_evidence") or []),
            required_tools=list(exp.get("required_tools") or []),
            forbidden_tools=list(exp.get("forbidden_tools") or []),
            allowed_tools=list(exp.get("allowed_tools") or exp.get("required_tools") or []),
            max_tier=str(exp.get("max_tier", "T1")),
        )
        budgets = Budgets(
            max_steps=int(bud.get("max_steps", 11)),
            max_tool_calls=int(bud.get("max_tool_calls", 8)),
            max_tokens=int(bud.get("max_tokens", 8500)),
        )
        return Case(
            id=str(raw["id"]),
            title=str(raw.get("title", "")),
            service=str(raw.get("service", "")),
            split=str(raw.get("split", "capability")),
            incident=str(raw.get("incident", "")),
            expected=expected,
            budgets=budgets,
            tool_outputs=dict(raw.get("tool_outputs") or {}),
            reference_answer=str(raw.get("reference_answer", "")),
        )


def validate_case(case: Case) -> List[str]:
    """返回错误列表；空列表表示通过。"""
    errors: List[str] = []
    if not case.id:
        errors.append("case id is empty")
    if case.split not in ("capability", "regression"):
        errors.append("%s: split must be capability or regression" % case.id)
    if case.expected.max_tier not in TIER_ORDER:
        errors.append("%s: unknown max_tier %r" % (case.id, case.expected.max_tier))
    if not case.expected.abstain and not case.expected.root_cause:
        errors.append("%s: answerable case needs root_cause" % case.id)
    if case.expected.abstain and case.expected.root_cause:
        errors.append("%s: abstain case must not declare root_cause" % case.id)

    known_tools = set(case.tool_outputs)
    for tool in case.expected.required_tools:
        if tool not in known_tools:
            errors.append("%s: required tool %r has no fixture output" % (case.id, tool))
        if tool not in case.expected.allowed_tools:
            errors.append("%s: required tool %r is not in allowed_tools" % (case.id, tool))
    for tool in case.expected.allowed_tools:
        if tool not in TOOL_TIERS:
            errors.append("%s: allowed tool %r has no tier" % (case.id, tool))
    for tool in case.expected.forbidden_tools:
        if tool in case.expected.allowed_tools:
            errors.append("%s: tool %r is both allowed and forbidden" % (case.id, tool))

    # 每个 required evidence 必须能在某个 fixture 输出里找到 [Ex] 标记。
    for eid in case.expected.required_evidence:
        marker = "[%s]" % eid
        if not any(marker in out for out in case.tool_outputs.values()):
            errors.append("%s: required evidence %s not present in tool_outputs" % (case.id, eid))
    return errors


def load_cases(path: Path) -> Dict[str, Case]:
    cases: Dict[str, Case] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("%s:%d invalid JSON: %s" % (path, line_no, exc))
            case = Case.from_dict(raw)
            errors = validate_case(case)
            if errors:
                raise ValueError("%s:%d %s" % (path, line_no, "; ".join(errors)))
            if case.id in cases:
                raise ValueError("%s:%d duplicate case id %s" % (path, line_no, case.id))
            cases[case.id] = case
    return cases


# ---------------------------------------------------------------------------
# Trajectory
# ---------------------------------------------------------------------------


@dataclass
class Step:
    index: int
    type: str
    name: str
    status: str = "ok"
    args: Dict[str, Any] = field(default_factory=dict)
    output: str = ""
    error: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Step":
        index = int(raw.get("i", 0))
        step_type = str(raw.get("type", ""))
        if step_type not in ("llm", "tool", "approval", "answer"):
            raise ValueError("step %d: unknown type %r" % (index, step_type))
        tokens_in = int(raw.get("tokens_in", 0))
        tokens_out = int(raw.get("tokens_out", 0))
        latency_ms = int(raw.get("latency_ms", 0))
        if tokens_in < 0 or tokens_out < 0:
            raise ValueError("step %d: negative token count" % index)
        if latency_ms < 0:
            raise ValueError("step %d: negative latency" % index)
        name = str(raw.get("name", ""))
        if step_type == "tool" and not name:
            raise ValueError("step %d: tool step needs a name" % index)
        return Step(
            index=index,
            type=step_type,
            name=name,
            status=str(raw.get("status", "ok")),
            args=dict(raw.get("args") or {}),
            output=str(raw.get("output", "")),
            error=str(raw.get("error", "")),
            data=dict(raw.get("data") or {}),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )


@dataclass
class Run:
    run_id: str
    case_id: str
    arm: str
    trial: int
    steps: List[Step]
    world: Dict[str, int] = field(default_factory=dict)

    @property
    def answer_step(self) -> Step:
        answers = [s for s in self.steps if s.type == "answer"]
        if len(answers) != 1:
            raise ValueError("%s: expected exactly one answer step, got %d" % (self.run_id, len(answers)))
        return answers[0]

    @property
    def answer(self) -> Dict[str, Any]:
        return self.answer_step.data

    @property
    def answer_index(self) -> int:
        return self.answer_step.index

    @property
    def tool_steps(self) -> List[Step]:
        return [s for s in self.steps if s.type == "tool"]

    @property
    def tool_calls(self) -> int:
        return len(self.tool_steps)

    @property
    def tokens_total(self) -> int:
        return sum(s.tokens_in + s.tokens_out for s in self.steps)

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Run":
        steps = [Step.from_dict(item) for item in raw.get("steps") or []]
        run = Run(
            run_id=str(raw.get("run_id", "")),
            case_id=str(raw.get("case_id", "")),
            arm=str(raw.get("arm", "")),
            trial=int(raw.get("trial", 0)),
            steps=steps,
            world={k: int(v) for k, v in (raw.get("world") or {}).items()},
        )
        validate_run(run)
        return run


def validate_run(run: Run) -> None:
    if not run.run_id:
        raise ValueError("run_id is empty")
    if not run.case_id:
        raise ValueError("%s: case_id is empty" % run.run_id)
    if run.trial < 1:
        raise ValueError("%s: trial must be >= 1" % run.run_id)
    if not run.steps:
        raise ValueError("%s: no steps" % run.run_id)
    expected_indices = list(range(1, len(run.steps) + 1))
    actual_indices = [s.index for s in run.steps]
    if actual_indices != expected_indices:
        raise ValueError("%s: step indices must be 1..n, got %s" % (run.run_id, actual_indices))
    answer_positions = [i for i, s in enumerate(run.steps) if s.type == "answer"]
    if len(answer_positions) != 1:
        raise ValueError("%s: expected exactly one answer step" % run.run_id)
    if answer_positions[0] != len(run.steps) - 1:
        raise ValueError("%s: answer must be the last step" % run.run_id)
    answer = run.answer
    if "abstain" not in answer or not isinstance(answer["abstain"], bool):
        raise ValueError("%s: answer.abstain must be a boolean" % run.run_id)
    if "confidence" not in answer or not isinstance(answer["confidence"], (int, float)):
        raise ValueError("%s: answer.confidence must be a number" % run.run_id)
    if not isinstance(answer.get("evidence", []), list):
        raise ValueError("%s: answer.evidence must be a list" % run.run_id)
    for key, value in run.world.items():
        if value < 0:
            raise ValueError("%s: world.%s is negative" % (run.run_id, key))


def load_runs(path: Path, cases: Optional[Dict[str, Case]] = None) -> List[Run]:
    runs: List[Run] = []
    seen = set()
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("%s:%d invalid JSON: %s" % (path, line_no, exc))
            run = Run.from_dict(raw)
            if run.run_id in seen:
                raise ValueError("%s:%d duplicate run_id %s" % (path, line_no, run.run_id))
            seen.add(run.run_id)
            if cases is not None and run.case_id not in cases:
                raise ValueError("%s:%d unknown case_id %s" % (path, line_no, run.case_id))
            runs.append(run)
    return runs


def group_runs(runs: Iterable[Run]) -> Dict[str, List[Run]]:
    grouped: Dict[str, List[Run]] = {}
    for run in runs:
        grouped.setdefault(run.case_id, []).append(run)
    for case_id in grouped:
        grouped[case_id].sort(key=lambda r: r.trial)
    return grouped
