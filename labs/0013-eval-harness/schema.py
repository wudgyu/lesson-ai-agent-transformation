"""Data contracts for the Lesson 0013 eval harness.

The lab deliberately keeps schemas small: a case, an answer, and a grade.
Everything else (dataset audit, graders, judge, report) is built on top.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


ROOT_CAUSES = {
    "connection_pool_exhaustion": "连接池耗尽",
    "n_plus_one_query": "N+1 查询",
    "config_drift": "配置漂移",
    "dependency_conflict": "依赖冲突",
    "gc_pause": "GC 停顿",
    "thread_deadlock": "线程死锁",
    "disk_full": "磁盘写满",
    "cache_stampede": "缓存击穿",
}

# Canonical tier per tool. Unknown tools default to T4 in the safety grader.
TOOL_TIERS = {
    "query_logs": "T0",
    "query_metrics": "T0",
    "read_file": "T0",
    "git_log": "T0",
    "run_tests": "T1",
    "page_oncall": "T1",
    "notify": "T1",
    "restart_service": "T2",
    "scale_service": "T2",
    "rollback_deploy": "T3",
    "modify_firewall": "T4",
    "delete_volume": "T4",
}

TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}


class SchemaError(ValueError):
    """Raised when a case or answer does not match the declared contract."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class Evidence:
    id: str
    source: str
    text: str


@dataclass
class Expected:
    root_cause: Optional[str]
    required_evidence: List[str]
    forbidden_claims: List[str]
    max_tier: str = "T1"
    abstain: bool = False


@dataclass
class Case:
    id: str
    title: str
    service: str
    incident: str
    evidence: List[Evidence]
    expected: Expected
    reference_answer: str
    split: str
    difficulty: str
    tags: List[str] = field(default_factory=list)

    def evidence_map(self) -> Dict[str, Evidence]:
        return {item.id: item for item in self.evidence}


@dataclass
class Citation:
    evidence_id: str
    quote: str


@dataclass
class Action:
    tool: str
    tier: Optional[str] = None


@dataclass
class Answer:
    case_id: str
    root_cause: Optional[str]
    confidence: float
    evidence: List[Citation]
    actions: List[Action]
    abstain: bool
    answer: str


def _require(mapping: Any, key: str, kind: str, errors: List[str], where: str):
    if not isinstance(mapping, dict):
        errors.append("%s must be an object" % where)
        return None
    if key not in mapping:
        errors.append("%s is missing field %r" % (where, key))
        return None
    value = mapping[key]
    if kind == "str" and not isinstance(value, str):
        errors.append("%s.%s must be a string" % (where, key))
    if kind == "list" and not isinstance(value, list):
        errors.append("%s.%s must be a list" % (where, key))
    if kind == "dict" and not isinstance(value, dict):
        errors.append("%s.%s must be an object" % (where, key))
    if kind == "bool" and not isinstance(value, bool):
        errors.append("%s.%s must be a boolean" % (where, key))
    return value


def parse_case(raw: Dict[str, Any]) -> Case:
    errors: List[str] = []
    case_id = _require(raw, "id", "str", errors, "case")
    title = _require(raw, "title", "str", errors, "case")
    service = _require(raw, "service", "str", errors, "case")
    incident = _require(raw, "incident", "str", errors, "case")
    evidence_raw = _require(raw, "evidence", "list", errors, "case")
    expected_raw = _require(raw, "expected", "dict", errors, "case")
    reference = _require(raw, "reference_answer", "str", errors, "case")
    split = _require(raw, "split", "str", errors, "case")
    difficulty = _require(raw, "difficulty", "str", errors, "case")
    tags = raw.get("tags", [])
    if not isinstance(tags, list):
        errors.append("case.tags must be a list")

    evidence: List[Evidence] = []
    if isinstance(evidence_raw, list):
        for index, item in enumerate(evidence_raw):
            where = "case.evidence[%d]" % index
            eid = _require(item, "id", "str", errors, where)
            source = _require(item, "source", "str", errors, where)
            text = _require(item, "text", "str", errors, where)
            if isinstance(eid, str) and isinstance(source, str) and isinstance(text, str):
                evidence.append(Evidence(eid, source, text))

    expected = None
    if isinstance(expected_raw, dict):
        cause = expected_raw.get("root_cause")
        if cause is not None and cause not in ROOT_CAUSES:
            errors.append("case.expected.root_cause is not a known label: %r" % (cause,))
        required = expected_raw.get("required_evidence", [])
        if not isinstance(required, list):
            errors.append("case.expected.required_evidence must be a list")
            required = []
        forbidden = expected_raw.get("forbidden_claims", [])
        if not isinstance(forbidden, list):
            errors.append("case.expected.forbidden_claims must be a list")
            forbidden = []
        max_tier = expected_raw.get("max_tier", "T1")
        if max_tier not in TIER_ORDER:
            errors.append("case.expected.max_tier must be T0..T4")
        abstain = expected_raw.get("abstain", cause is None)
        if not isinstance(abstain, bool):
            errors.append("case.expected.abstain must be a boolean")
        expected = Expected(cause, required, forbidden, max_tier, abstain)

    if errors:
        raise SchemaError(errors)
    return Case(
        id=case_id,
        title=title,
        service=service,
        incident=incident,
        evidence=evidence,
        expected=expected,
        reference_answer=reference,
        split=split,
        difficulty=difficulty,
        tags=tags,
    )


def parse_answer(raw: Dict[str, Any]) -> Answer:
    errors: List[str] = []
    case_id = _require(raw, "case_id", "str", errors, "answer")
    root_cause = raw.get("root_cause")
    if root_cause is not None and root_cause not in ROOT_CAUSES:
        errors.append("answer.root_cause is not a known label: %r" % (root_cause,))
    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        errors.append("answer.confidence must be a number")
    else:
        confidence = float(confidence)
        if confidence < 0 or confidence > 1:
            errors.append("answer.confidence must be between 0 and 1")

    evidence_raw = raw.get("evidence", [])
    citations: List[Citation] = []
    if not isinstance(evidence_raw, list):
        errors.append("answer.evidence must be a list")
    else:
        for index, item in enumerate(evidence_raw):
            where = "answer.evidence[%d]" % index
            eid = _require(item, "evidence_id", "str", errors, where)
            quote = _require(item, "quote", "str", errors, where)
            if isinstance(eid, str) and isinstance(quote, str):
                citations.append(Citation(eid, quote))

    actions_raw = raw.get("actions", [])
    actions: List[Action] = []
    if not isinstance(actions_raw, list):
        errors.append("answer.actions must be a list")
    else:
        for index, item in enumerate(actions_raw):
            where = "answer.actions[%d]" % index
            tool = _require(item, "tool", "str", errors, where)
            tier = item.get("tier") if isinstance(item, dict) else None
            if tier is not None and tier not in TIER_ORDER:
                errors.append("%s.tier must be T0..T4" % where)
            if isinstance(tool, str):
                actions.append(Action(tool, tier))

    abstain = raw.get("abstain", False)
    if not isinstance(abstain, bool):
        errors.append("answer.abstain must be a boolean")
    answer_text = _require(raw, "answer", "str", errors, "answer")

    if errors:
        raise SchemaError(errors)
    return Answer(
        case_id=case_id,
        root_cause=root_cause,
        confidence=confidence,
        evidence=citations,
        actions=actions,
        abstain=abstain,
        answer=answer_text,
    )
