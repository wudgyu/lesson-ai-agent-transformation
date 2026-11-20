"""第 14 课 lab 的命令行入口。

用法：
    python3 ci_cli.py audit
    python3 ci_cli.py run --run runs/v04-candidate.jsonl
    python3 ci_cli.py passk --run runs/v04-candidate.jsonl --k 3
    python3 ci_cli.py failures --baseline runs/v03-baseline.jsonl --candidate runs/v04-candidate.jsonl
    python3 ci_cli.py gate --baseline runs/v03-baseline.jsonl --candidate runs/v04-candidate.jsonl
    python3 ci_cli.py demo
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from gate import evaluate_gate, format_gate
from graders import RunGrade, grade_runs
from metrics import summarize_failures
from passk import compute_passk
from trace_schema import Run, load_cases, load_runs

HERE = Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "cases.jsonl"
DEFAULT_BASELINE = HERE / "runs" / "v03-baseline.jsonl"
DEFAULT_CANDIDATE = HERE / "runs" / "v04-candidate.jsonl"
DEFAULT_HOTFIX = HERE / "runs" / "v05-hotfix.jsonl"


def _resolve(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if candidate.exists():
        return candidate
    return HERE / candidate


def _load(cases_path: Path, runs_path: Path):
    cases = load_cases(cases_path)
    runs = load_runs(runs_path, cases)
    return cases, runs


def _by_case(grades: List[RunGrade]) -> Dict[str, List[RunGrade]]:
    grouped: Dict[str, List[RunGrade]] = {}
    for grade in grades:
        grouped.setdefault(grade.case_id, []).append(grade)
    for case_id in grouped:
        grouped[case_id].sort(key=lambda g: g.trial)
    return grouped


def _pct(value: float) -> str:
    return "%.1f%%" % (value * 100)


def cmd_audit(args: argparse.Namespace) -> int:
    cases = load_cases(_resolve(args.cases))
    paths = [("baseline", _resolve(args.baseline)), ("candidate", _resolve(args.candidate)), ("hotfix", _resolve(args.hotfix))]

    split_counts = Counter(case.split for case in cases.values())
    abstain = sum(1 for case in cases.values() if case.expected.abstain)
    labels = Counter(case.expected.root_cause for case in cases.values() if case.expected.root_cause)
    print("cases: %d" % len(cases))
    print("split: %s" % " · ".join("%s %d" % (name, split_counts[name]) for name in sorted(split_counts)))
    print("answerable: %d · abstain: %d" % (len(cases) - abstain, abstain))
    print("labels: %s" % " · ".join("%s %d" % (label, labels[label]) for label in sorted(labels)))
    print("")

    ok = True
    for label, path in paths:
        runs = load_runs(path, cases)
        groups: Dict[str, List[Run]] = {}
        for run in runs:
            groups.setdefault(run.case_id, []).append(run)
        problems = []
        for case_id in sorted(cases):
            trials = sorted(run.trial for run in groups.get(case_id, []))
            if trials != [1, 2, 3]:
                problems.append("%s trials=%s" % (case_id, trials))
        if problems:
            ok = False
            print("%s: %d runs · ERROR %s" % (label, len(runs), ", ".join(problems[:4])))
        else:
            print("%s: %d runs · %d cases × 3 trials" % (label, len(runs), len(groups)))
    print("")
    print("audit: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    cases, runs = _load(_resolve(args.cases), _resolve(args.run))
    grades = grade_runs(cases, runs)
    grouped = _by_case(grades)
    passed = sum(1 for grade in grades if grade.passed)
    outcome = sum(1 for grade in grades if grade.outcome_passed)
    arm = grades[0].arm if grades else "unknown"
    print("arm: %s · runs: %d" % (arm, len(grades)))
    print(
        "contract pass: %d/%d (%s) · outcome-only: %d/%d (%s)"
        % (passed, len(grades), _pct(passed / len(grades)), outcome, len(grades), _pct(outcome / len(grades)))
    )
    print("")

    for case_id in sorted(grouped):
        case_grades = grouped[case_id]
        marks = "".join("✓" if grade.passed else "✗" for grade in case_grades)
        wins = sum(1 for grade in case_grades if grade.passed)
        failed = [grade for grade in case_grades if not grade.passed]
        detail = "" if not failed else "  %s: %s" % (failed[0].primary_failure, failed[0].primary_detail)
        print("%-4s %s  %d/%d%s" % (case_id, marks, wins, len(case_grades), detail))

    print("")
    print("slices")
    for split in ("capability", "regression"):
        subset = [grade for grade in grades if cases[grade.case_id].split == split]
        wins = sum(1 for grade in subset if grade.passed)
        print("  %-11s %d/%d (%s)" % (split, wins, len(subset), _pct(wins / len(subset) if subset else 0.0)))

    summary = summarize_failures(grades)
    print("")
    print("failure categories")
    if not summary.counts:
        print("  none")
    else:
        for category, count in sorted(summary.counts.items(), key=lambda item: (-item[1], item[0])):
            print("  %-22s %d" % (category, count))
            for example in summary.examples.get(category, [])[:2]:
                print("    · %s" % example)
    return 0


def cmd_passk(args: argparse.Namespace) -> int:
    cases, runs = _load(_resolve(args.cases), _resolve(args.run))
    grades = grade_runs(cases, runs)
    grouped = _by_case(grades)
    trials = min(len(items) for items in grouped.values())
    max_k = min(args.k, trials)
    arm = grades[0].arm if grades else "unknown"

    print("%s · %d cases × %d trials" % (arm, len(grouped), trials))
    print("")
    print("case matrix (✓ = contract pass)")
    matrix_report = compute_passk(grades, 1)
    for case_id in sorted(grouped):
        print("  %-4s %s" % (case_id, matrix_report.matrix[case_id]))
    print("")
    print("k   pass^k   pass@k   naive p^k   95% CI (bootstrap)")
    for k in range(1, max_k + 1):
        report = compute_passk(grades, k)
        print(
            "%-3d %-8s %-8s %-11s [%s, %s]"
            % (
                k,
                _pct(report.pass_hat_k),
                _pct(report.pass_at_k),
                _pct(report.naive_power),
                _pct(report.pass_hat_ci[0]),
                _pct(report.pass_hat_ci[1]),
            )
        )

    report = compute_passk(grades, max_k)
    print("")
    print(
        "all-%d pass: %d/%d (%s) · Wilson 95%% [%s, %s]"
        % (
            trials,
            round(report.all_trials_pass_fraction * report.cases),
            report.cases,
            _pct(report.all_trials_pass_fraction),
            _pct(report.all_trials_pass_ci[0]),
            _pct(report.all_trials_pass_ci[1]),
        )
    )
    print("flake rate: %d/%d (%s)" % (report.flake_cases, report.cases, _pct(report.flake_rate)))

    outcome = compute_passk(grades, max_k, predicate=lambda g: g.outcome_passed)
    print(
        "outcome-only pass^%d: %s (the final answers look better than the trajectories)"
        % (max_k, _pct(outcome.pass_hat_k))
    )
    return 0


def cmd_failures(args: argparse.Namespace) -> int:
    cases = load_cases(_resolve(args.cases))
    baseline_runs = load_runs(_resolve(args.baseline), cases)
    candidate_runs = load_runs(_resolve(args.candidate), cases)
    baseline = grade_runs(cases, baseline_runs)
    candidate = grade_runs(cases, candidate_runs)
    base_summary = summarize_failures(baseline)
    cand_summary = summarize_failures(candidate)

    categories = sorted(set(base_summary.counts) | set(cand_summary.counts))
    print("failure taxonomy (contract failures)")
    print("  %-24s %8s %10s" % ("category", "baseline", "candidate"))
    for category in categories:
        print(
            "  %-24s %8d %10d"
            % (category, base_summary.counts.get(category, 0), cand_summary.counts.get(category, 0))
        )
    print("")
    new_categories = sorted(set(cand_summary.counts) - set(base_summary.counts))
    if new_categories:
        print("new in candidate: %s" % ", ".join(new_categories))
        print("")
    print("candidate examples")
    for category in sorted(cand_summary.examples):
        for example in cand_summary.examples[category]:
            print("  [%s] %s" % (category, example))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    cases = load_cases(_resolve(args.cases))
    baseline = grade_runs(cases, load_runs(_resolve(args.baseline), cases))
    candidate = grade_runs(cases, load_runs(_resolve(args.candidate), cases))
    report = evaluate_gate(
        baseline,
        candidate,
        cases,
        candidate_arm=candidate[0].arm,
        baseline_arm=baseline[0].arm,
    )
    print(format_gate(report))
    return 1 if report.blocked else 0


def cmd_demo(args: argparse.Namespace) -> int:
    cases = load_cases(_resolve(args.cases))
    baseline = grade_runs(cases, load_runs(_resolve(args.baseline), cases))
    candidate = grade_runs(cases, load_runs(_resolve(args.candidate), cases))
    hotfix = grade_runs(cases, load_runs(_resolve(args.hotfix), cases))

    print("=" * 72)
    print("act 1 · candidate v0.4：总分更高，但发布更危险")
    print("=" * 72)
    report = evaluate_gate(baseline, candidate, cases, candidate_arm="v0.4-candidate", baseline_arm="v0.3-baseline")
    print(format_gate(report))
    k3 = compute_passk(candidate, 3)
    print("")
    print(
        "candidate pass^1 %s · pass^3 %s · flake %s"
        % (_pct(report.metrics["candidate_pass_rate"]), _pct(k3.pass_hat_k), _pct(k3.flake_rate))
    )
    print("capability +%.1f pp, regression %d→%d/12" % (
        report.metrics["capability_delta"] * 100,
        sum(1 for g in baseline if cases[g.case_id].split == "regression" and g.passed),
        sum(1 for g in candidate if cases[g.case_id].split == "regression" and g.passed),
    ))

    print("")
    print("=" * 72)
    print("act 2 · hotfix v0.5：先修可靠性、安全与回归，再谈发布")
    print("=" * 72)
    fixed = evaluate_gate(baseline, hotfix, cases, candidate_arm="v0.5-hotfix", baseline_arm="v0.3-baseline")
    print(format_gate(fixed))
    k3f = compute_passk(hotfix, 3)
    print("")
    print(
        "hotfix pass^1 %s · pass^3 %s · flake %s"
        % (_pct(fixed.metrics["candidate_pass_rate"]), _pct(k3f.pass_hat_k), _pct(k3f.flake_rate))
    )
    print("")
    print("lesson: 总分是仪表盘；门禁是发布契约。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="lesson 0014 trajectory / pass^k / release gate lab")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="cases.jsonl path")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="validate cases and runs")
    audit.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    audit.add_argument("--candidate", default=str(DEFAULT_CANDIDATE))
    audit.add_argument("--hotfix", default=str(DEFAULT_HOTFIX))
    audit.set_defaults(func=cmd_audit)

    run = sub.add_parser("run", help="grade one run file")
    run.add_argument("--run", default=str(DEFAULT_CANDIDATE))
    run.set_defaults(func=cmd_run)

    passk = sub.add_parser("passk", help="compute pass^k and flake rate")
    passk.add_argument("--run", default=str(DEFAULT_CANDIDATE))
    passk.add_argument("--k", type=int, default=3)
    passk.set_defaults(func=cmd_passk)

    failures = sub.add_parser("failures", help="compare failure taxonomy")
    failures.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    failures.add_argument("--candidate", default=str(DEFAULT_CANDIDATE))
    failures.set_defaults(func=cmd_failures)

    gate = sub.add_parser("gate", help="evaluate release gate")
    gate.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    gate.add_argument("--candidate", default=str(DEFAULT_CANDIDATE))
    gate.set_defaults(func=cmd_gate)

    demo = sub.add_parser("demo", help="run the full story")
    demo.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    demo.add_argument("--candidate", default=str(DEFAULT_CANDIDATE))
    demo.add_argument("--hotfix", default=str(DEFAULT_HOTFIX))
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
