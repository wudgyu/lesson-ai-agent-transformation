"""Load and audit the 40-case RepoMedic eval set."""
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from common import load_jsonl, normalize_text
from schema import (
    ROOT_CAUSES,
    TIER_ORDER,
    Case,
    SchemaError,
    parse_case,
)


BASE = Path(__file__).resolve().parent
DEFAULT_CASES = BASE / "data" / "cases.jsonl"


@dataclass
class DatasetAudit:
    total: int = 0
    answerable: int = 0
    abstain: int = 0
    splits: Dict[str, int] = field(default_factory=dict)
    difficulties: Dict[str, int] = field(default_factory=dict)
    labels: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors

    def format(self) -> str:
        lines = []
        lines.append("cases: %d" % self.total)
        lines.append("answerable: %d · abstain: %d" % (self.answerable, self.abstain))
        lines.append("split: " + " · ".join("%s %d" % (k, v) for k, v in sorted(self.splits.items())))
        lines.append("difficulty: " + " · ".join("%s %d" % (k, v) for k, v in sorted(self.difficulties.items())))
        if self.labels:
            lines.append("labels: " + " · ".join("%s %d" % (k, v) for k, v in sorted(self.labels.items())))
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            for warning in self.warnings:
                lines.append("  - " + warning)
        if self.errors:
            lines.append("")
            lines.append("errors:")
            for error in self.errors:
                lines.append("  - " + error)
        else:
            lines.append("")
            lines.append("dataset audit: OK")
        return "\n".join(lines)


def load_cases(path: Optional[Path] = None) -> List[Case]:
    path = Path(path) if path is not None else DEFAULT_CASES
    cases: List[Case] = []
    seen_ids = set()
    for line_no, raw in load_jsonl(path):
        try:
            case = parse_case(raw)
        except SchemaError as exc:
            raise SchemaError(["%s:%d: %s" % (path, line_no, error) for error in exc.errors])
        if case.id in seen_ids:
            raise SchemaError(["%s:%d: duplicate case id %s" % (path, line_no, case.id)])
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise SchemaError(["%s: no cases found" % path])
    return cases


def audit_cases(cases: List[Case]) -> DatasetAudit:
    audit = DatasetAudit(total=len(cases))
    label_counts = Counter()
    incident_counts = Counter()
    title_counts = Counter()

    for case in cases:
        where = "case %s" % case.id
        incident_counts[normalize_text(case.incident)] += 1
        title_counts[normalize_text(case.title)] += 1

        if case.split not in ("capability", "regression"):
            audit.errors.append("%s: split must be capability or regression" % where)
        if case.difficulty not in ("easy", "medium", "hard"):
            audit.errors.append("%s: difficulty must be easy, medium, or hard" % where)

        evidence_ids = [item.id for item in case.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            audit.errors.append("%s: duplicate evidence id" % where)
        if len(case.evidence) < 2:
            audit.warnings.append("%s: fewer than 2 evidence items" % where)

        missing_required = [eid for eid in case.expected.required_evidence if eid not in evidence_ids]
        if missing_required:
            audit.errors.append("%s: required evidence does not exist: %s" % (where, ", ".join(missing_required)))

        if case.expected.abstain:
            audit.abstain += 1
            if case.expected.root_cause is not None:
                audit.errors.append("%s: abstain case must have root_cause = null" % where)
            if case.expected.required_evidence:
                audit.errors.append("%s: abstain case must not require evidence" % where)
        else:
            audit.answerable += 1
            if case.expected.root_cause not in ROOT_CAUSES:
                audit.errors.append("%s: answerable case needs a known root cause" % where)
            else:
                label_counts[case.expected.root_cause] += 1
            if len(case.expected.required_evidence) < 2:
                audit.warnings.append("%s: answerable case should require at least 2 evidence items" % where)

        if case.expected.max_tier not in TIER_ORDER:
            audit.errors.append("%s: invalid max_tier %s" % (where, case.expected.max_tier))
        if not case.reference_answer.strip():
            audit.errors.append("%s: missing reference_answer" % where)
        elif len(case.reference_answer) < 20:
            audit.warnings.append("%s: reference_answer is very short" % where)

        # The incident is what the agent sees. The title is metadata.
        lowered = case.incident.lower()
        for phrase in ("根因是", "根因：", "root cause"):
            if phrase in lowered:
                audit.errors.append("%s: incident leaks the answer with %r" % (where, phrase))
        label_zh = ROOT_CAUSES.get(case.expected.root_cause or "")
        if label_zh and label_zh in case.incident:
            audit.errors.append("%s: incident contains the root-cause label %r" % (where, label_zh))

        for claim in case.expected.forbidden_claims:
            if claim and claim in case.reference_answer:
                audit.warnings.append("%s: reference_answer contains forbidden claim %r" % (where, claim))

        audit.splits[case.split] = audit.splits.get(case.split, 0) + 1
        audit.difficulties[case.difficulty] = audit.difficulties.get(case.difficulty, 0) + 1

    audit.labels = dict(label_counts)

    for incident, count in incident_counts.items():
        if count > 1:
            audit.errors.append("duplicate incident text (%d cases): %s" % (count, incident[:60]))
    for title, count in title_counts.items():
        if count > 1:
            audit.errors.append("duplicate title (%d cases): %s" % (count, title[:60]))

    if audit.answerable:
        counts = list(label_counts.values())
        if counts and max(counts) - min(counts) > 1:
            audit.warnings.append(
                "label imbalance: min %d, max %d per root cause" % (min(counts), max(counts))
            )

    if audit.total and audit.abstain / audit.total < 0.15:
        audit.warnings.append("fewer than 15%% abstain cases; add 'should not answer' examples")
    if audit.total and audit.splits.get("regression", 0) / audit.total < 0.2:
        audit.warnings.append("fewer than 20%% regression cases; capability-only suites drift")

    return audit
