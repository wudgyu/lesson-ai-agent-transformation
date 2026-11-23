#!/usr/bin/env python3
"""Command-line interface for the RepoMedic security lab.

Examples:
    python3 security_cli.py cases
    python3 security_cli.py run --mode baseline
    python3 security_cli.py run --mode guardrail
    python3 security_cli.py run --mode hardened
    python3 security_cli.py report
    python3 security_cli.py report --break taint
    python3 security_cli.py trace --id A07
    python3 security_cli.py demo
"""

import argparse
import sys

from cases import ATTACKS, BENIGN, CASES, case_by_id
from gateway import Gateway
from policy import safe_preview
from report import format_table, run_all, run_mode, verdict


def cmd_cases(args) -> int:
    print("%d attacks · %d benign\n" % (len(ATTACKS), len(BENIGN)))
    print("%-4s %-6s %-22s %-20s %s" % ("ID", "kind", "category", "expected layer", "title"))
    print("-" * 100)
    for case in CASES:
        print("%-4s %-6s %-22s %-20s %s" % (
            case.id, case.kind, case.category, case.expected_layer or case.expect_effect, case.title))
    return 0


def cmd_run(args) -> int:
    metrics, runs = run_mode(args.mode, args.break_layer)
    print("mode: %s%s" % (args.mode, (" · break=%s" % args.break_layer) if args.break_layer else ""))
    print("")
    for run in runs:
        if run.kind == "attack":
            status = "HARM" if run.harm else "BLOCK:%s" % run.blocked_by
        else:
            status = "OK" if run.utility else "LOST:%s" % run.blocked_by
        print("%-4s %-18s %-22s %s" % (run.case_id, status, run.category, run.blocked_by))
    print("")
    print("ASR %.1f%% (%d/%d) · utility %.1f%% (%d/%d) · FP %d · approvals %d · redactions %d"
          % (metrics.asr * 100, metrics.attacks_harm, metrics.attacks,
             metrics.utility * 100, metrics.benign_ok, metrics.benign,
             metrics.false_positives, metrics.approvals, metrics.redactions))
    return 0


def cmd_report(args) -> int:
    results = run_all(args.break_layer)
    print(format_table(results))
    print("")
    hardened = results[-1]
    print("hardened blocked_by:")
    for layer, count in sorted(hardened.layer_counts.items(), key=lambda item: (-item[1], item[0])):
        print("  %-22s %d" % (layer, count))
    print("")
    print("hardened by category:")
    for category, count in sorted(hardened.category_counts.items()):
        print("  %-22s %d" % (category, count))
    print("")
    print("verdict: %s" % verdict(hardened))
    return 0 if verdict(hardened) == "PASS" else 1


def cmd_trace(args) -> int:
    case = case_by_id(args.id)
    run = Gateway(mode="hardened").run_case(case)
    print("case %s · %s · %s" % (case.id, case.category, case.kind))
    print("title: %s" % case.title)
    print("user: %s" % safe_preview(case.user_request, 200))
    print("")
    print("origins:")
    for origin in case.origins:
        print("  %-20s %-9s %s" % (origin.id, origin.trust, safe_preview(origin.content, 120)))
    print("")
    print("proposed calls:")
    for call in case.calls:
        print("  %s %s" % (call.call_id, call.tool))
        for name, arg in sorted(call.args.items()):
            origins = ",".join(arg.origins) if arg.origins else "literal"
            print("    %-8s %-40s origins=%s" % (name, safe_preview(arg.value, 40), origins))
    print("")
    print("audit:")
    for event in run.audit:
        print("  #%d %-8s %-16s %-10s %-24s %s" % (
            event.seq, event.call_id, event.layer, event.outcome, event.reason,
            safe_preview(event.detail, 100)))
    print("")
    print("effects:")
    if not run.effects:
        print("  (none)")
    for effect in run.effects:
        print("  %-10s %-30s %s" % (effect.kind, safe_preview(effect.target, 28),
                                      safe_preview(effect.payload, 80)))
    print("")
    print("result: harm=%s · blocked_by=%s · utility=%s · redactions=%d"
          % (run.harm, run.blocked_by, run.utility, run.redactions))
    return 0


def cmd_demo(args) -> int:
    print("RepoMedic security demo")
    print("=" * 72)
    for case_id in ("A03", "A14", "A15", "A13", "B08", "B06"):
        case = case_by_id(case_id)
        print("")
        print("%s · %s" % (case.id, case.title))
        print("  user: %s" % case.user_request)
        for mode in ("baseline", "guardrail", "hardened"):
            run = Gateway(mode=mode).run_case(case)
            if case.kind == "attack":
                result = "HARM" if run.harm else "safe via %s" % run.blocked_by
            else:
                result = "utility preserved" if run.utility else "utility lost via %s" % run.blocked_by
            print("  %-10s %s" % (mode + ":", result))
    print("")
    print("The baseline executes the attack; the guardrail catches the obvious")
    print("phrasing but misses encoding; the hardened pipeline blocks the flow")
    print("at the tool boundary without blocking benign security documentation.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RepoMedic security lab")
    sub = parser.add_subparsers(dest="command")

    p_cases = sub.add_parser("cases", help="list attack and benign cases")
    p_cases.set_defaults(func=cmd_cases)

    p_run = sub.add_parser("run", help="run one pipeline")
    p_run.add_argument("--mode", choices=("baseline", "guardrail", "hardened"), required=True)
    p_run.add_argument("--break", dest="break_layer", default="", help="comma-separated layers to disable")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="compare pipelines and show the gate")
    p_report.add_argument("--break", dest="break_layer", default="", help="comma-separated layers to disable")
    p_report.set_defaults(func=cmd_report)

    p_trace = sub.add_parser("trace", help="show one case through the hardened pipeline")
    p_trace.add_argument("--id", required=True)
    p_trace.set_defaults(func=cmd_trace)

    p_demo = sub.add_parser("demo", help="run the narrative cases")
    p_demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
