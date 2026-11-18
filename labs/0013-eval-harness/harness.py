"""Run a recorded agent run through deterministic graders and report slices."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common import load_jsonl, pct
from graders import CaseGrade, Grade, grade_case
from schema import Answer, Case, SchemaError, parse_answer
from stats import pass_hat_k, wilson_interval


@dataclass
class RunItem:
    case_id: str
    answer: Optional[Answer]
    error: Optional[str] = None


@dataclass
class RunFile:
    name: str
    path: Path
    items: Dict[str, RunItem] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


@dataclass
class SliceStats:
    name: str
    total: int
    passed: int

    @property
    def rate(self) -> float:
        return self.passed / float(self.total) if self.total else 0.0

    def format(self) -> str:
        return "%s: %d/%d (%s)" % (self.name, self.passed, self.total, pct(self.rate))


@dataclass
class RunReport:
    name: str
    results: List[CaseGrade]
    schema_errors: int
    soft_flags: int
    slices: Dict[str, SliceStats]
    labels: Dict[str, SliceStats]
    difficulties: Dict[str, SliceStats]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.overall)

    @property
    def pass_rate(self) -> float:
        return self.passed / float(self.total) if self.total else 0.0

    @property
    def ci(self):
        return wilson_interval(self.passed, self.total)

    def failures(self) -> List[Tuple[CaseGrade, List[Grade]]]:
        return [(result, result.hard_failed) for result in self.results if not result.overall]

    def format(self) -> str:
        low, high = self.ci
        lines = [
            "run: %s" % self.name,
            "cases: %d · schema errors: %d · soft flags: %d" % (
                self.total, self.schema_errors, self.soft_flags),
            "overall: %d/%d (%s) · 95%% CI [%s, %s] · pass^3 %s" % (
                self.passed, self.total, pct(self.pass_rate), pct(low), pct(high),
                pct(pass_hat_k(self.pass_rate, 3)),
            ),
            " · ".join(self.slices[key].format() for key in sorted(self.slices)),
        ]
        if self.labels:
            lines.append("by label: " + " · ".join(
                "%s %d/%d" % (key, value.passed, value.total) for key, value in sorted(self.labels.items())
            ))
        if self.difficulties:
            lines.append("by difficulty: " + " · ".join(
                "%s %d/%d" % (key, value.passed, value.total) for key, value in sorted(self.difficulties.items())
            ))
        failures = self.failures()
        if failures:
            lines.append("")
            lines.append("failures:")
            for result, grades in failures:
                details = "; ".join("%s: %s" % (grade.name, grade.detail) for grade in grades)
                lines.append("  %s  %s" % (result.case_id, details))
        return "\n".join(lines)


def load_run(path: Path, cases_by_id: Dict[str, Case], name: Optional[str] = None) -> RunFile:
    path = Path(path)
    run = RunFile(name or path.stem, path)
    for line_no, raw in load_jsonl(path):
        case_id = raw.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            run.errors.append("%s:%d: missing case_id" % (path, line_no))
            run.items["__line_%d__" % line_no] = RunItem("__line_%d__" % line_no, None, "missing case_id")
            continue
        if case_id in run.items:
            run.errors.append("%s:%d: duplicate answer for %s; keeping first" % (path, line_no, case_id))
            continue
        if case_id not in cases_by_id:
            run.errors.append("%s:%d: answer for unknown case %s" % (path, line_no, case_id))
            run.items[case_id] = RunItem(case_id, None, "unknown case_id %s" % case_id)
            continue
        try:
            answer = parse_answer(raw)
        except SchemaError as exc:
            run.errors.append("%s:%d: %s" % (path, line_no, exc))
            run.items[case_id] = RunItem(case_id, None, str(exc))
            continue
        run.items[case_id] = RunItem(case_id, answer)
    return run


def evaluate_run(cases: List[Case], run: RunFile) -> RunReport:
    results: List[CaseGrade] = []
    schema_errors = 0
    soft_flags = 0

    for case in cases:
        item = run.items.get(case.id)
        if item is None:
            schema_errors += 1
            results.append(CaseGrade(case.id, [Grade("schema", False, "missing answer for case")]))
            continue
        if item.error or item.answer is None:
            schema_errors += 1
            results.append(CaseGrade(case.id, [Grade("schema", False, item.error or "invalid answer")]))
            continue
        result = grade_case(case, item.answer)
        soft_flags += sum(1 for grade in result.grades if not grade.hard and not grade.passed)
        results.append(result)

    case_by_id = {case.id: case for case in cases}
    return RunReport(
        name=run.name,
        results=results,
        schema_errors=schema_errors,
        soft_flags=soft_flags,
        slices=_slice_by(results, case_by_id, lambda case: case.split),
        labels=_slice_by(results, case_by_id, lambda case: case.expected.root_cause or "abstain"),
        difficulties=_slice_by(results, case_by_id, lambda case: case.difficulty),
    )


def _slice_by(results: List[CaseGrade], cases_by_id: Dict[str, Case], key_fn) -> Dict[str, SliceStats]:
    buckets: Dict[str, List[bool]] = {}
    for result in results:
        case = cases_by_id[result.case_id]
        key = key_fn(case)
        buckets.setdefault(key, []).append(result.overall)
    return {
        key: SliceStats(key, len(values), sum(1 for value in values if value))
        for key, values in sorted(buckets.items())
    }


def compare_reports(baseline: RunReport, candidate: RunReport) -> str:
    lines = ["metric                          baseline    candidate   delta"]
    rows = [
        ("overall", baseline.pass_rate, candidate.pass_rate),
        ("capability", baseline.slices.get("capability", SliceStats("capability", 0, 0)).rate,
         candidate.slices.get("capability", SliceStats("capability", 0, 0)).rate),
        ("regression", baseline.slices.get("regression", SliceStats("regression", 0, 0)).rate,
         candidate.slices.get("regression", SliceStats("regression", 0, 0)).rate),
    ]
    for name, before, after in rows:
        lines.append("%-30s %9s %11s %+8.1f pp" % (
            name, pct(before), pct(after), (after - before) * 100.0))
    before_fail = {result.case_id for result in baseline.results if not result.overall}
    after_fail = {result.case_id for result in candidate.results if not result.overall}
    fixed = sorted(before_fail - after_fail)
    regressed = sorted(after_fail - before_fail)
    lines.append("")
    lines.append("fixed: %s" % (", ".join(fixed) if fixed else "none"))
    lines.append("regressed: %s" % (", ".join(regressed) if regressed else "none"))
    return "\n".join(lines)
