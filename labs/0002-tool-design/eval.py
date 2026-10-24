"""Tool-selection evaluation for lesson 0002.

用法：
    python3 eval.py --validate                 # 离线检查评测集与工具名
    python3 eval.py --tools baseline           # 真实模型跑 baseline
    python3 eval.py --tools refactored         # 真实模型跑 refactored

评测的是「工具设计」，不是模型排名：
- first-tool：模型第一步是否选了合适的工具；
- answer：最终回答是否包含关键事实（不要求逐字）；
- safety：写操作是否被审批拦住；
- steps / tokens：同一任务要付多少步数与上下文成本。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent import AgentLoopError, run_agent
from llm import OpenAICompatibleLLM, load_dotenv
from tool_base import ToolRegistry

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "eval_cases.json"


def build_registry(toolset: str) -> ToolRegistry:
    if toolset == "baseline":
        from tools_baseline import build_registry as build
    else:
        from tools_refactored import build_registry as build
    return build()


def load_cases() -> List[Dict[str, Any]]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def tool_steps(trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [step for step in trace if step.get("type") == "tool"]


def first_tool(trace: List[Dict[str, Any]]) -> Optional[str]:
    steps = tool_steps(trace)
    return steps[0]["name"] if steps else None


def answer_matches(answer: str, groups: List[List[str]]) -> bool:
    lowered = (answer or "").lower()
    return all(any(keyword.lower() in lowered for keyword in group) for group in groups)


def restart_succeeded(trace: List[Dict[str, Any]]) -> bool:
    for step in tool_steps(trace):
        if step["name"] == "restart_service" and '"status": "restarted"' in step["result"]:
            return True
    return False


def validate(cases: List[Dict[str, Any]]) -> int:
    errors: List[str] = []
    registries = {
        "baseline": build_registry("baseline"),
        "refactored": build_registry("refactored"),
    }
    for case in cases:
        cid = case.get("id", "?")
        for key in ("id", "question", "expected_first_tools", "answer_any"):
            if key not in case:
                errors.append("%s: missing field '%s'" % (cid, key))
        expected = case.get("expected_first_tools", {})
        for toolset, names in expected.items():
            if toolset not in registries:
                errors.append("%s: unknown toolset '%s'" % (cid, toolset))
                continue
            available = set(registries[toolset].names())
            for name in names:
                if name != "__none__" and name not in available:
                    errors.append(
                        "%s/%s: tool '%s' does not exist (available: %s)"
                        % (cid, toolset, name, ", ".join(sorted(available)))
                    )
        if not case.get("answer_any"):
            errors.append("%s: answer_any is empty" % cid)
        if case.get("safety") not in (None, "deny_restart"):
            errors.append("%s: unknown safety rule '%s'" % (cid, case.get("safety")))

    if errors:
        print("VALIDATION FAILED")
        for error in errors:
            print("  -", error)
        return 1
    print(
        "OK: %d cases validated. baseline tools=%d, refactored tools=%d"
        % (
            len(cases),
            len(registries["baseline"].names()),
            len(registries["refactored"].names()),
        )
    )
    for toolset, registry in registries.items():
        print("  %s: %s" % (toolset, ", ".join(registry.names())))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Tool-selection eval (lesson 0002)")
    parser.add_argument("--tools", choices=["baseline", "refactored"], default="refactored")
    parser.add_argument("--validate", action="store_true", help="离线校验评测集")
    parser.add_argument("--case", action="append", help="只跑指定 id，可重复")
    parser.add_argument("--limit", type=int, help="只跑前 N 条")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--out", help="报告输出路径")
    args = parser.parse_args()

    cases = load_cases()
    if args.validate:
        raise SystemExit(validate(cases))

    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            parser.error("no cases matched --case")
    if args.limit:
        cases = cases[: args.limit]

    load_dotenv()
    try:
        llm = OpenAICompatibleLLM()
    except RuntimeError as exc:
        print("Cannot start eval: %s" % exc)
        print("Run with --validate for an offline check, or configure .env first.")
        raise SystemExit(2)

    registry = build_registry(args.tools)
    rows: List[Dict[str, Any]] = []

    print("=== tool-design eval · %s · %d cases ===" % (args.tools, len(cases)))
    print("model: %s" % llm.model)
    print()

    for case in cases:
        row: Dict[str, Any] = {
            "id": case["id"],
            "question": case["question"],
            "expected_first_tools": case["expected_first_tools"][args.tools],
        }
        try:
            result = run_agent(
                case["question"],
                llm,
                registry,
                max_steps=args.max_steps,
                verbose=False,
            )
            first = first_tool(result["trace"])
            expected = case["expected_first_tools"][args.tools]
            if first is None:
                first_ok = "__none__" in expected
            else:
                first_ok = first in expected
            answer_ok = answer_matches(result["answer"], case["answer_any"])
            safety_ok = True
            if case.get("safety") == "deny_restart":
                safety_ok = not restart_succeeded(result["trace"])
            steps = len([step for step in result["trace"] if step.get("type") == "model"])
            tokens = result["usage"]["total_tokens"]
            # pass 只衡量“任务完成且安全”；first-tool 是诊断指标，
            # 避免模型先查健康再查日志这种合理路径被误判为失败。
            passed = answer_ok and safety_ok
            row.update(
                {
                    "first_tool": first,
                    "first_tool_ok": first_ok,
                    "answer_ok": answer_ok,
                    "safety_ok": safety_ok,
                    "pass": passed,
                    "steps": steps,
                    "tokens": tokens,
                    "answer": result["answer"],
                    "tool_calls": [
                        {"name": step["name"], "arguments": step["arguments"],
                         "result": step["result"]}
                        for step in tool_steps(result["trace"])
                    ],
                }
            )
        except AgentLoopError as exc:
            row.update(
                {
                    "first_tool": None,
                    "first_tool_ok": False,
                    "answer_ok": False,
                    "safety_ok": False,
                    "pass": False,
                    "steps": args.max_steps,
                    "tokens": 0,
                    "answer": "",
                    "error": str(exc),
                }
            )
        except Exception as exc:  # API 错误、工具参数错误等：记录后继续
            row.update(
                {
                    "first_tool": None,
                    "first_tool_ok": False,
                    "answer_ok": False,
                    "safety_ok": False,
                    "pass": False,
                    "steps": 0,
                    "tokens": 0,
                    "answer": "",
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
            )
        rows.append(row)
        mark = "✓" if row["pass"] else "✗"
        print(
            "%-4s %s  first=%-22s steps=%-2d tokens=%-6d %s"
            % (
                row["id"],
                mark,
                row.get("first_tool") or "(none)",
                row["steps"],
                row["tokens"],
                "" if row["pass"] else ("error=" + row["error"] if row.get("error") else "check answer/tool"),
            )
        )

    total = len(rows)
    passed = sum(1 for row in rows if row["pass"])
    first_ok = sum(1 for row in rows if row["first_tool_ok"])
    answer_ok = sum(1 for row in rows if row["answer_ok"])
    safety_ok = sum(1 for row in rows if row["safety_ok"])
    avg_steps = sum(row["steps"] for row in rows) / total if total else 0
    tokens = sum(row["tokens"] for row in rows)
    summary = {
        "toolset": args.tools,
        "cases": total,
        "pass": passed,
        "first_tool_ok": first_ok,
        "answer_ok": answer_ok,
        "safety_ok": safety_ok,
        "avg_steps": round(avg_steps, 2),
        "total_tokens": tokens,
    }

    print()
    print(
        "pass %d/%d | first-tool %d/%d | answer %d/%d | safety %d/%d | "
        "avg steps %.2f | tokens %d"
        % (
            passed, total,
            first_ok, total,
            answer_ok, total,
            safety_ok, total,
            avg_steps,
            tokens,
        )
    )

    failures = [row for row in rows if not row["pass"]]
    if failures:
        print("\nfailures:")
        for row in failures:
            print("  %s: %s" % (row["id"], row["question"]))
            print("    expected first: %s; got: %s" % (
                ", ".join(row["expected_first_tools"]),
                row.get("first_tool") or "(none)",
            ))
            if row.get("answer"):
                print("    answer: %s" % row["answer"].replace("\n", " ")[:220])
            if row.get("error"):
                print("    error: %s" % row["error"])

    out_path = Path(args.out) if args.out else ROOT / ("eval-report-%s.json" % args.tools)
    out_path.write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\nreport: %s" % out_path)


if __name__ == "__main__":
    main()
