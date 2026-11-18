"""Calibrate a judge against human labels.

Positive class = FAILURE. That is the class we care about catching.
  TPR = caught failures / all human failures
  TNR = passed good answers / all human passes
Raw agreement alone is misleading when failures are rare: an always-pass
judge gets 95% agreement on a 5%-failure set while catching nothing.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common import load_jsonl, pct
from schema import Answer, Case, SchemaError, parse_answer


BASE = Path(__file__).resolve().parent
DEFAULT_CALIBRATION = BASE / "data" / "judge_calibration.jsonl"


@dataclass
class CalibrationExample:
    id: str
    case_id: str
    answer: Answer
    human_pass: bool
    human_critique: str


@dataclass
class ConfusionMatrix:
    tp: int = 0  # human fail, judge fail (caught)
    tn: int = 0  # human pass, judge pass
    fp: int = 0  # human pass, judge fail (false alarm)
    fn: int = 0  # human fail, judge pass (missed)

    @property
    def total(self) -> int:
        return self.tp + self.tn + self.fp + self.fn

    @property
    def tpr(self) -> float:
        denom = self.tp + self.fn
        return self.tp / float(denom) if denom else 0.0

    @property
    def tnr(self) -> float:
        denom = self.tn + self.fp
        return self.tn / float(denom) if denom else 0.0

    @property
    def raw_agreement(self) -> float:
        return (self.tp + self.tn) / float(self.total) if self.total else 0.0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / float(denom) if denom else 0.0

    @property
    def kappa(self) -> float:
        if not self.total:
            return 0.0
        po = self.raw_agreement
        human_fail = self.tp + self.fn
        human_pass = self.tn + self.fp
        judge_fail = self.tp + self.fp
        judge_pass = self.tn + self.fn
        pe = (human_fail * judge_fail + human_pass * judge_pass) / float(self.total * self.total)
        return (po - pe) / (1.0 - pe) if pe != 1.0 else 0.0

    def format(self, title: str) -> str:
        return "\n".join([
            title,
            "  TP %d (fail caught) · TN %d (pass kept) · FP %d (false alarm) · FN %d (failure missed)" % (
                self.tp, self.tn, self.fp, self.fn),
            "  TPR (catch failures): %s" % pct(self.tpr),
            "  TNR (keep passes):    %s" % pct(self.tnr),
            "  raw agreement:        %s" % pct(self.raw_agreement),
            "  precision:            %s" % pct(self.precision),
            "  Cohen's kappa:        %.3f" % self.kappa,
        ])


@dataclass
class CalibrationReport:
    examples: int
    human_fail: int
    judge_unknown: int
    matrix: ConfusionMatrix
    always_pass: ConfusionMatrix
    disagreements: List[Tuple[CalibrationExample, str, str]] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            "calibration set: %d examples · human fail: %d · judge UNKNOWN: %d" % (
                self.examples, self.human_fail, self.judge_unknown),
            "",
            self.matrix.format("judge"),
            "",
            self.always_pass.format("always-pass baseline"),
        ]
        if self.disagreements:
            lines.append("")
            lines.append("disagreements (human, judge, critique):")
            for example, human, critique in self.disagreements:
                lines.append("  %s human=%s · %s" % (example.id, human, critique))
        return "\n".join(lines)


def load_calibration(path: Optional[Path] = None, cases_by_id: Optional[Dict[str, Case]] = None):
    path = Path(path) if path is not None else DEFAULT_CALIBRATION
    cases_by_id = cases_by_id or {}
    examples: List[CalibrationExample] = []
    for line_no, raw in load_jsonl(path):
        try:
            example_id = str(raw["id"])
            case_id = str(raw["case_id"])
            answer = parse_answer(raw["answer"])
            label = raw["human_label"]
            if label not in ("pass", "fail"):
                raise SchemaError(["human_label must be 'pass' or 'fail'"])
            critique = str(raw.get("human_critique", ""))
        except (KeyError, SchemaError) as exc:
            raise SchemaError(["%s:%d: %s" % (path, line_no, exc)])
        if cases_by_id and case_id not in cases_by_id:
            raise SchemaError(["%s:%d: unknown case_id %s" % (path, line_no, case_id)])
        examples.append(CalibrationExample(example_id, case_id, answer, label == "pass", critique))
    if not examples:
        raise SchemaError(["%s: no examples found" % path])
    return examples


def evaluate_judge(judge, examples: List[CalibrationExample], cases_by_id: Dict[str, Case]) -> CalibrationReport:
    matrix = ConfusionMatrix()
    always = ConfusionMatrix()
    disagreements = []
    unknown = 0
    human_fail = 0

    for example in examples:
        case = cases_by_id[example.case_id]
        verdict = judge.overall(case, example.answer)
        if verdict.verdict == "UNKNOWN":
            unknown += 1
        judge_pass = verdict.verdict == "PASS"
        if not example.human_pass:
            human_fail += 1
            if judge_pass:
                matrix.fn += 1
            else:
                matrix.tp += 1
            always.fn += 1  # an always-pass judge misses every failure
        else:
            if judge_pass:
                matrix.tn += 1
            else:
                matrix.fp += 1
            always.tn += 1  # and keeps every pass
        if judge_pass != example.human_pass:
            disagreements.append((example, "pass" if example.human_pass else "fail", verdict.critique))

    return CalibrationReport(len(examples), human_fail, unknown, matrix, always, disagreements)
