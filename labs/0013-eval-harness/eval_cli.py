#!/usr/bin/env python3
"""RepoMedic eval harness CLI.

Commands:
  audit                 check the 40-case dataset for structural problems
  run --run NAME        grade a recorded run (data/runs/NAME.jsonl)
  compare               baseline vs candidate, overall and by slice
  calibrate             judge vs human labels, TPR/TNR not just agreement
  bias                  pairwise position/verbosity bias simulation
  demo                  all of the above, in a fixed order
"""
import argparse
import sys
from pathlib import Path

from bias import evaluate_pairs, load_pairs
from calibration import evaluate_judge, load_calibration
from dataset import audit_cases, load_cases
from harness import compare_reports, evaluate_run, load_run
from judge import LLMJudge, RubricJudge
from common import pct


BASE = Path(__file__).resolve().parent
DEFAULT_CASES = BASE / "data" / "cases.jsonl"
DEFAULT_RUNS = BASE / "data" / "runs"


def resolve_run(value: str) -> Path:
    path = Path(value)
    if path.suffix == ".jsonl" or "/" in value:
        return path
    return DEFAULT_RUNS / (value + ".jsonl")


def judge_from_name(name: str):
    if name == "rubric":
        return RubricJudge()
    if name == "llm":
        try:
            return LLMJudge()
        except ValueError as exc:
            raise SystemExit("LLM judge unavailable: %s" % exc)
    raise SystemExit("unknown judge %r (use rubric or llm)" % name)


def cmd_audit(args) -> int:
    cases = load_cases(Path(args.cases))
    audit = audit_cases(cases)
    print(audit.format())
    return 0 if audit.passed else 1


def cmd_run(args) -> int:
    cases = load_cases(Path(args.cases))
    cases_by_id = {case.id: case for case in cases}
    run = load_run(resolve_run(args.run), cases_by_id)
    if run.errors:
        print("load warnings:")
        for error in run.errors:
            print("  - " + error)
        print("")
    report = evaluate_run(cases, run)
    print(report.format())
    if args.fail_under is not None and report.pass_rate < args.fail_under:
        print("")
        print("FAIL: pass rate %s is below --fail-under %s" % (
            pct(report.pass_rate), pct(args.fail_under)))
        return 1
    return 0


def cmd_compare(args) -> int:
    cases = load_cases(Path(args.cases))
    cases_by_id = {case.id: case for case in cases}
    baseline = evaluate_run(cases, load_run(resolve_run(args.baseline), cases_by_id))
    candidate = evaluate_run(cases, load_run(resolve_run(args.candidate), cases_by_id))
    print(baseline.format())
    print("")
    print(candidate.format())
    print("")
    print(compare_reports(baseline, candidate))
    return 0


def cmd_calibrate(args) -> int:
    cases = load_cases(Path(args.cases))
    cases_by_id = {case.id: case for case in cases}
    examples = load_calibration(Path(args.calibration), cases_by_id)
    judge = judge_from_name(args.judge)
    report = evaluate_judge(judge, examples, cases_by_id)
    print("judge: %s" % judge.name)
    print(report.format())
    return 0


def cmd_bias(args) -> int:
    cases = load_cases(Path(args.cases))
    cases_by_id = {case.id: case for case in cases}
    pairs = load_pairs(Path(args.pairs), cases_by_id)
    print(evaluate_pairs(pairs, cases_by_id).format())
    return 0


def cmd_demo(args) -> int:
    print("=" * 68)
    print("1. dataset audit")
    print("=" * 68)
    code = cmd_audit(args)
    if code != 0:
        return code

    print("")
    print("=" * 68)
    print("2. baseline vs candidate")
    print("=" * 68)
    cmd_compare(args)

    print("")
    print("=" * 68)
    print("3. judge calibration")
    print("=" * 68)
    cmd_calibrate(args)

    print("")
    print("=" * 68)
    print("4. pairwise bias simulation")
    print("=" * 68)
    cmd_bias(args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RepoMedic eval harness")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="cases.jsonl path")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="audit the dataset")
    audit.set_defaults(func=cmd_audit)

    run = sub.add_parser("run", help="grade one recorded run")
    run.add_argument("--run", default="candidate", help="run name or JSONL path")
    run.add_argument("--fail-under", type=float, default=None, help="CI gate, e.g. 0.80")
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="compare two recorded runs")
    compare.add_argument("--baseline", default="baseline")
    compare.add_argument("--candidate", default="candidate")
    compare.set_defaults(func=cmd_compare)

    calibrate = sub.add_parser("calibrate", help="calibrate a judge against human labels")
    calibrate.add_argument("--judge", default="rubric", choices=("rubric", "llm"))
    calibrate.add_argument("--calibration", default=str(BASE / "data" / "judge_calibration.jsonl"))
    calibrate.set_defaults(func=cmd_calibrate)

    bias = sub.add_parser("bias", help="pairwise position/verbosity bias simulation")
    bias.add_argument("--pairs", default=str(BASE / "data" / "pairwise.jsonl"))
    bias.set_defaults(func=cmd_bias)

    demo = sub.add_parser("demo", help="run the whole lesson demo")
    demo.add_argument("--baseline", default="baseline")
    demo.add_argument("--candidate", default="candidate")
    demo.add_argument("--judge", default="rubric", choices=("rubric", "llm"))
    demo.add_argument("--calibration", default=str(BASE / "data" / "judge_calibration.jsonl"))
    demo.add_argument("--pairs", default=str(BASE / "data" / "pairwise.jsonl"))
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
