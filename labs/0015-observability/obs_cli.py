"""第 15 课 lab 的命令行入口。

用法：
    python3 obs_cli.py audit
    python3 obs_cli.py trace --id R04
    python3 obs_cli.py cost
    python3 obs_cli.py latency
    python3 obs_cli.py dashboard
    python3 obs_cli.py demo
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from dashboard import Dashboard, build_dashboard
from latency import (
    LatencySummary,
    TraceLatency,
    analyze_trace,
    check_budget,
    exclusive_ms,
    summarize,
    tail_amplification,
)
from ledger import ArmSummary, Ledger, TraceCost
from pricing import PriceBook
from trace_schema import Trace, load_traces

HERE = Path(__file__).resolve().parent
DEFAULT_TRACES = HERE / "traces.jsonl"
DEFAULT_PRICING = HERE / "pricing.json"
DEFAULT_BUDGETS = HERE / "budgets.json"


def _resolve(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if candidate.exists():
        return candidate
    return HERE / candidate


def _load(args: argparse.Namespace):
    traces = load_traces(_resolve(args.traces))
    pricebook = PriceBook.from_path(_resolve(args.pricing))
    with _resolve(args.budgets).open("r", encoding="utf-8") as handle:
        budgets = json.load(handle)
    return traces, pricebook, budgets


def _ms(value: float) -> str:
    if value >= 1000:
        return "%.1fs" % (value / 1000.0)
    return "%.0fms" % value


def _usd(value: float) -> str:
    return "$%.6f" % value


def _pct(value: float) -> str:
    return "%.1f%%" % (value * 100)


def _depth(trace: Trace) -> Dict[str, int]:
    children: Dict[Optional[str], List] = {}
    for span in trace.spans:
        children.setdefault(span.parent_id, []).append(span)
    depths: Dict[str, int] = {}

    def visit(span, depth: int) -> None:
        depths[span.span_id] = depth
        for child in sorted(children.get(span.span_id, []), key=lambda item: item.start_ms):
            visit(child, depth + 1)

    visit(trace.root, 0)
    return depths


def _span_usage_text(span) -> str:
    usage = span.usage
    if usage is None:
        return "usage=MISSING"
    parts = ["in=%d" % usage.input_tokens, "out=%d" % usage.output_tokens]
    if usage.cache_read_input_tokens:
        parts.append("cache_read=%d" % usage.cache_read_input_tokens)
    if usage.cache_write_input_tokens:
        parts.append("cache_write=%d" % usage.cache_write_input_tokens)
    if usage.reasoning_output_tokens:
        parts.append("reasoning=%d" % usage.reasoning_output_tokens)
    return " ".join(parts)


def cmd_audit(args: argparse.Namespace) -> int:
    traces, pricebook, _budgets = _load(args)
    ledger = Ledger(traces, pricebook)
    arms: Dict[str, int] = {}
    for trace in traces:
        arms[trace.arm] = arms.get(trace.arm, 0) + 1

    missing = []
    mismatches = []
    unknown = []
    for entry in ledger.entries.values():
        for span_id in entry.missing_usage:
            missing.append("%s:%s" % (entry.trace.trace_id, span_id))
        for name in entry.unknown_models:
            unknown.append("%s:%s" % (entry.trace.trace_id, name))
        rec = entry.trace.reconciliation()
        if rec["status"] == "mismatch":
            mismatches.append(
                "%s (input %+d, output %+d)"
                % (entry.trace.trace_id, rec["delta_input"], rec["delta_output"])
            )
        elif rec["status"] == "missing_aggregate":
            mismatches.append("%s (no aggregate usage on agent span)" % entry.trace.trace_id)

    print("traces: %d · spans: %d" % (len(traces), sum(len(trace.spans) for trace in traces)))
    print(
        "arms: %s"
        % " · ".join("%s %d" % (arm, count) for arm, count in sorted(arms.items()))
    )
    print(
        "success: %d/%d (%s)"
        % (ledger.successes, ledger.runs, _pct(ledger.successes / ledger.runs))
    )
    complete = sum(1 for entry in ledger.entries.values() if entry.complete)
    print(
        "usage: complete %d/%d · missing %d · aggregate mismatch %d"
        % (complete, len(ledger.entries), len(missing), len(mismatches))
    )
    for item in missing:
        print("  missing usage: %s" % item)
    for item in mismatches:
        print("  mismatch: %s" % item)
    for item in unknown:
        print("  unknown model: %s" % item)

    ok = not missing and not mismatches and not unknown
    print("")
    print("audit: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def _print_trace(trace: Trace, ledger: Ledger) -> None:
    entry = ledger.entry(trace.trace_id)
    latency = analyze_trace(trace)
    depths = _depth(trace)
    print(
        "trace %s · case %s · arm %s · %s"
        % (trace.trace_id, trace.case_id, trace.arm, "success" if trace.success else "FAILED")
    )
    print(
        "wall %s · blocked %s · active %s · overhead %s"
        % (
            _ms(latency.wall_ms),
            _ms(latency.blocked_ms),
            _ms(latency.active_ms),
            _ms(latency.overhead_ms),
        )
    )
    print(
        "critical path %s (active %s)"
        % (_ms(latency.critical_path_ms), _ms(latency.active_critical_path_ms))
    )
    print("")
    print("%-22s %-10s %-9s %-9s %-8s %s" % ("span", "kind", "start", "end", "dur", "excl"))
    for span in trace.walk():
        depth = depths.get(span.span_id, 0)
        name = "  " * depth + span.span_id
        print(
            "%-22s %-10s %-9s %-9s %-8s %s"
            % (
                name,
                span.kind,
                _ms(span.start_ms),
                _ms(span.end_ms),
                _ms(span.duration_ms),
                _ms(_exclusive(trace, span)),
            )
        )
    print("")
    usage, missing = trace.billed_usage()
    print(
        "billed usage: input %d (cache_read %d, %s) · output %d · reasoning %d"
        % (
            usage.input_tokens,
            usage.cache_read_input_tokens,
            _pct(usage.cache_hit_rate),
            usage.output_tokens,
            usage.reasoning_output_tokens,
        )
    )
    print(
        "cost: %s · cache savings %s · failed+retry %s"
        % (_usd(entry.total), _usd(entry.cost.cache_savings), _usd(entry.retry_cost))
    )
    if missing:
        print("WARNING: cost is a lower bound; missing usage on %s" % ", ".join(sorted(missing)))
    if entry.unknown_models:
        print("WARNING: unknown model price: %s" % ", ".join(sorted(entry.unknown_models)))
    rec = trace.reconciliation()
    if rec["status"] == "mismatch":
        print(
            "WARNING: agent aggregate mismatch: input %+d, output %+d"
            % (rec["delta_input"], rec["delta_output"])
        )
    print("")
    print(
        "stage union: llm %s · tool %s · approval %s · retrieval %s · guardrail %s"
        % (
            _ms(latency.stage_union_ms.get("llm", 0)),
            _ms(latency.stage_union_ms.get("tool", 0)),
            _ms(latency.stage_union_ms.get("approval", 0)),
            _ms(latency.stage_union_ms.get("retrieval", 0)),
            _ms(latency.stage_union_ms.get("guardrail", 0)),
        )
    )
    print("critical path: %s" % " → ".join(span.span_id for span in latency.path))


def _exclusive(trace: Trace, span) -> int:
    return exclusive_ms(span, trace.children_of(span.span_id))


def cmd_trace(args: argparse.Namespace) -> int:
    traces, pricebook, _budgets = _load(args)
    ledger = Ledger(traces, pricebook)
    trace = next((item for item in traces if item.trace_id == args.id), None)
    if trace is None:
        print("unknown trace id: %s (choose from %s)" % (args.id, ", ".join(t.trace_id for t in traces)))
        return 2
    _print_trace(trace, ledger)
    return 0


def _top_driver(entry: TraceCost) -> str:
    if not entry.model_costs:
        return "unknown" if entry.unknown_models else "-"
    model = max(entry.model_costs.items(), key=lambda item: item[1].total)
    return "%s %s" % (model[0], _usd(model[1].total))


def cmd_cost(args: argparse.Namespace) -> int:
    traces, pricebook, _budgets = _load(args)
    ledger = Ledger(traces, pricebook)
    print(
        "%-5s %-6s %-14s %-4s %-11s %-9s %-11s %s"
        % ("run", "case", "arm", "ok", "cost", "cache", "retry", "top driver")
    )
    for trace in sorted(traces, key=lambda item: ledger.entry(item.trace_id).total, reverse=True):
        entry = ledger.entry(trace.trace_id)
        print(
            "%-5s %-6s %-14s %-4s %-11s %-9s %-11s %s"
            % (
                trace.trace_id,
                trace.case_id,
                trace.arm,
                "yes" if trace.success else "NO",
                _usd(entry.total) + ("" if entry.complete else "*"),
                _pct(entry.trace.billed_usage()[0].cache_hit_rate),
                _usd(entry.retry_cost),
                _top_driver(entry),
            )
        )
    print("")
    print("arm summary")
    for arm in sorted(ledger.by_arm()):
        summary: ArmSummary = ledger.by_arm()[arm]
        cps = summary.cost_per_success
        print(
            "  %-14s runs %d · success %d · cost %s · $/success %s · cache %s · retry %s%s"
            % (
                arm,
                summary.runs,
                summary.successes,
                _usd(summary.cost),
                _usd(cps) if cps is not None else "n/a",
                _pct(summary.cache_hit_rate),
                _usd(summary.retry_cost),
                "" if summary.known_cost else " · INCOMPLETE",
            )
        )
    print("")
    print("by model")
    for model, cost in sorted(ledger.by_model().items()):
        print("  %-14s %s" % (model, _usd(cost.total)))
    print("")
    print(
        "total %s · successes %d · cost/success %s · cache hit %s · retry share %s"
        % (
            _usd(ledger.total),
            ledger.successes,
            _usd(ledger.cost_per_success) if ledger.cost_per_success is not None else "n/a",
            _pct(ledger.cache_hit_rate),
            _pct(ledger.retry_share),
        )
    )
    if not ledger.complete:
        print("* cost is a lower bound: some inference spans have no usage")
    print("")
    print("top cost runs")
    for entry in ledger.top_runs(3):
        share = entry.total / ledger.total if ledger.total else 0.0
        print(
            "  %s %s (%s of total) · %s"
            % (entry.trace.trace_id, _usd(entry.total), _pct(share), _top_driver(entry))
        )
    print("")
    print("context growth (input tokens re-billed across turns)")
    for trace in traces:
        growth = Ledger.context_growth(trace)
        if growth["turns"] >= 2 and growth["growth_ratio"] >= 1.5:
            print(
                "  %s %d turns · first %d → last %d (%s) · total input %d (%s last)"
                % (
                    trace.trace_id,
                    int(growth["turns"]),
                    int(growth["first_input"]),
                    int(growth["last_input"]),
                    "%.1f×" % growth["growth_ratio"],
                    int(growth["total_input"]),
                    "%.1f×" % growth["rebilled_ratio"],
                )
            )
    return 0


def _stage_top(latency: TraceLatency) -> str:
    stages = [
        (kind, value)
        for kind, value in latency.stage_union_ms.items()
        if value > 0
    ]
    if not stages:
        return "-"
    kind, value = max(stages, key=lambda item: item[1])
    return "%s %s" % (kind, _ms(value))


def cmd_latency(args: argparse.Namespace) -> int:
    traces, _pricebook, budgets = _load(args)
    print(
        "%-5s %-9s %-9s %-9s %-11s %s"
        % ("run", "wall", "blocked", "active", "critical", "top stage")
    )
    for trace in traces:
        latency = analyze_trace(trace)
        print(
            "%-5s %-9s %-9s %-9s %-11s %s"
            % (
                trace.trace_id,
                _ms(latency.wall_ms),
                _ms(latency.blocked_ms),
                _ms(latency.active_ms),
                _ms(latency.critical_path_ms),
                _stage_top(latency),
            )
        )
    summary: LatencySummary = summarize(traces)
    print("")
    print(
        "active p50/p95/p99: %s / %s / %s"
        % (_ms(summary.active_p50), _ms(summary.active_p95), _ms(summary.active_p99))
    )
    print(
        "wall p50/p95/p99:   %s / %s / %s"
        % (_ms(summary.wall_p50), _ms(summary.wall_p95), _ms(summary.wall_p99))
    )
    print(
        "tool p95 %s · approval p95 %s · ttft p95 %s"
        % (_ms(summary.tool_p95), _ms(summary.approval_p95), _ms(summary.ttft_p95))
    )
    print("")
    violations = check_budget(traces, budgets)
    if violations:
        print("budget violations")
        for violation in violations:
            print("  %s" % violation)
    else:
        print("budget: OK")
    print("")
    print("tail amplification (1 - (1 - p)^n):")
    for fanout in (5, 20, 100):
        print(
            "  p=1%% · fanout=%d → %s of requests hit at least one slow leaf"
            % (fanout, _pct(tail_amplification(0.01, fanout)))
        )
    return 0


def _print_dashboard(dash: Dashboard, ledger: Ledger) -> None:
    integrity_codes = ("missing_usage", "reconciliation", "unknown_model")
    cost_codes = ("cost_per_success", "cache_hit_rate", "retry_share")

    print("integrity")
    for alert in dash.alerts:
        if alert.code in integrity_codes:
            print("  [%s] %-18s %s" % (alert.level, alert.code, alert.message))
    print("latency")
    for alert in dash.alerts:
        if alert.code == "latency_budget":
            print("  [%s] %-18s %s" % (alert.level, alert.code, alert.message))
    print("cost")
    for alert in dash.alerts:
        if alert.code in cost_codes:
            print("  [%s] %-18s %s" % (alert.level, alert.code, alert.message))
    print("")
    print(
        "active p95 %s · cost/success %s · cache %s · retry share %s"
        % (
            _ms(dash.latency.active_p95),
            _usd(ledger.cost_per_success) if ledger.cost_per_success is not None else "n/a",
            _pct(ledger.cache_hit_rate),
            _pct(ledger.retry_share),
        )
    )
    print("verdict: %s" % dash.verdict)


def cmd_dashboard(args: argparse.Namespace) -> int:
    traces, pricebook, budgets = _load(args)
    ledger = Ledger(traces, pricebook)
    dash = build_dashboard(traces, ledger, budgets)
    _print_dashboard(dash, ledger)
    return 0 if dash.verdict != "BLOCK" else 1


def cmd_demo(args: argparse.Namespace) -> int:
    traces, pricebook, budgets = _load(args)
    ledger = Ledger(traces, pricebook)
    print("=" * 72)
    print("1. audit")
    cmd_audit(args)
    print("")
    print("=" * 72)
    print("2. trace R04 (human wait dominates wall time)")
    trace = next(item for item in traces if item.trace_id == "R04")
    _print_trace(trace, ledger)
    print("")
    print("=" * 72)
    print("3. cost")
    cmd_cost(args)
    print("")
    print("=" * 72)
    print("4. latency")
    cmd_latency(args)
    print("")
    print("=" * 72)
    print("5. dashboard")
    dash = build_dashboard(traces, ledger, budgets)
    _print_dashboard(dash, ledger)
    return 0 if dash.verdict != "BLOCK" else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="RepoMedic observability lab")
    parser.add_argument("--traces", default=str(DEFAULT_TRACES))
    parser.add_argument("--pricing", default=str(DEFAULT_PRICING))
    parser.add_argument("--budgets", default=str(DEFAULT_BUDGETS))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("audit", help="检查 trace schema、usage 完整性与汇总一致性")
    trace_parser = sub.add_parser("trace", help="看一条 trace 的 waterfall、账本与延迟")
    trace_parser.add_argument("--id", required=True, help="trace id，例如 R04")
    sub.add_parser("cost", help="按 run / arm / model 汇总成本")
    sub.add_parser("latency", help="看 active / critical path / 尾延迟")
    sub.add_parser("dashboard", help="合成面板；BLOCK 时 exit 1")
    sub.add_parser("demo", help="依次跑 audit、trace、cost、latency、dashboard")

    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            return cmd_audit(args)
        if args.command == "trace":
            return cmd_trace(args)
        if args.command == "cost":
            return cmd_cost(args)
        if args.command == "latency":
            return cmd_latency(args)
        if args.command == "dashboard":
            return cmd_dashboard(args)
        if args.command == "demo":
            return cmd_demo(args)
    except (ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
