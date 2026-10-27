"""Scenario evaluation for lesson 0005.

Runs deterministic, scripted sessions and compares memory strategies:

- none:        no compaction, no long-term memory
- compaction:  compact the state near the budget
- full:        compaction + long-term memory

Scenarios:
- long_incident: 18 turns; checks task success, prompt retention, token peak
- preference:    two sessions; checks cross-session memory recall
- stale:         old memory conflicts with a fresh tool result
- resume:        checkpoint after a crash; idempotent restart
"""
from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from memory import MemoryStore
from session import SessionAgent, SessionConfig, TurnResult
from state import AgentState

ROOT = Path(__file__).resolve().parent

LONG_TURNS = [
    "order-service 大量 5xx，先查日志，不要重启，最后给我证据。",
    "日志里有什么错误？",
    "根因是什么？",
    "最近一次部署是什么？",
    "连接池上限多少？",
    "重启能解决吗？",
    "还有什么没确认的？",
    "给我一个最终结论。",
    "再总结一次根因和证据。",
    "把日志里的关键行再复述一遍。",
    "这个结论的置信度如何？",
    "如果回滚，应该怎么验证？",
    "还有什么风险？",
    "下一步建议是什么？",
    "请给出一个简短的交接摘要。",
    "还有哪些证据支持这个结论？",
    "如果连接池扩容后仍然耗尽怎么办？",
    "最后用三句话总结。",
]

PREFERENCE_TURNS = [
    "order-service 大量 5xx，按我的偏好来。",
    "继续。",
    "继续。",
    "给我最终结论。",
]


def run_turns(agent: SessionAgent, turns: List[str]) -> Tuple[List[TurnResult], int]:
    results: List[TurnResult] = []
    peak = 0
    for turn in turns:
        result = agent.run_turn(turn)
        results.append(result)
        peak = max(peak, result.tokens)
        if result.action == "overflow":
            break
    return results, peak


def final_answer(agent: SessionAgent, results: List[TurnResult]) -> str:
    for result in reversed(results):
        if result.answer:
            return result.answer
    for message in reversed(agent.state.messages):
        if message.role == "assistant":
            return message.content
    return ""


def facts_ok(state: AgentState, required: Dict[str, str]) -> bool:
    return all(
        state.get_fact(key) is not None and state.get_fact(key).value == value
        for key, value in required.items()
    )


def prompt_retained(state: AgentState, prefix: str) -> bool:
    return any(
        message.role == "user" and message.content.startswith(prefix)
        for message in state.messages
    )


def memory_hit(results: List[TurnResult]) -> bool:
    return any(result.memory_hits > 0 for result in results)


def long_incident(
    mode: str,
    threshold: float = 0.70,
    keep_recent: int = 4,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        memory = MemoryStore(Path(tmp) / "memory.json")
        config = SessionConfig(
            max_context_tokens=800,
            compact_threshold=threshold,
            keep_recent=keep_recent,
            enable_compaction=(mode in ("compaction", "full")),
            enable_longterm=(mode == "full"),
            checkpoint_path=Path(tmp) / "checkpoint.json",
        )
        agent = SessionAgent.new("long", memory, config)
        results, peak = run_turns(agent, LONG_TURNS)
        answer = final_answer(agent, results)
        required = {
            "root_cause": "N+1 in OrderQueryService.findRecentOrders",
            "db_pool_max": "50",
            "deploy_version": "v2.14.1",
        }
        facts_retained = facts_ok(agent.state, required)
        return {
            "scenario": "long_incident",
            "mode": mode,
            "completed": results[-1].action != "overflow",
            "task_success": (
                "N+1" in answer and "48/50" in answer and "v2.14.1" in answer
            ),
            "facts_retained": facts_retained,
            "prompt_retained": prompt_retained(
                agent.state, "order-service 大量 5xx"
            ),
            "peak_tokens": peak,
            "final_tokens": agent.state.estimate_tokens(),
            "compactions": agent.state.compaction_count,
            "dropped_messages": sum(r.dropped_messages for r in results),
            "notes_has_facts": (
                "db_pool_max" in agent.state.notes
                and "root_cause" in agent.state.notes
            ),
            "answer": answer,
        }


def preference(longterm: bool) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        memory = MemoryStore(Path(tmp) / "memory.json")
        first = SessionAgent.new(
            "pref-1",
            memory,
            SessionConfig(max_context_tokens=5000, enable_compaction=False),
        )
        first.run_turn("记住：以后排查先看 runbook，再看日志。")

        second = SessionAgent.new(
            "pref-2",
            memory,
            SessionConfig(
                max_context_tokens=5000,
                enable_compaction=False,
                enable_longterm=longterm,
            ),
        )
        results, peak = run_turns(second, PREFERENCE_TURNS)
        answer = final_answer(second, results)
        return {
            "scenario": "preference",
            "mode": "longterm" if longterm else "no_longterm",
            "completed": results[-1].action != "overflow",
            "task_success": "N+1" in answer,
            "memory_hit": memory_hit(results),
            "first_tool": results[0].tool if results else None,
            "peak_tokens": peak,
            "answer": answer,
        }


def stale_memory(longterm: bool) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        memory = MemoryStore(Path(tmp) / "memory.json")
        memory.remember(
            "semantic",
            "order-service 的连接池上限是 db_pool_max=40",
            "old incident",
            tags=["order-service", "db_pool_max"],
            confidence=0.7,
        )
        agent = SessionAgent.new(
            "stale",
            memory,
            SessionConfig(
                max_context_tokens=5000,
                enable_compaction=False,
                enable_longterm=longterm,
            ),
        )
        results, _ = run_turns(
            agent,
            [
                "order-service 5xx，帮我查一下。",
                "日志里有什么错误？",
                "根因是什么？",
                "给我最终结论。",
            ],
        )
        answer = final_answer(agent, results)
        active = agent.state.get_fact("db_pool_max")
        superseded = any(
            fact.value == "40" and fact.status == "superseded"
            for fact in agent.state.fact_history
        )
        return {
            "scenario": "stale_memory",
            "mode": "longterm" if longterm else "no_longterm",
            "completed": results[-1].action != "overflow",
            "task_success": "48/50" in answer,
            "active_max": active.value if active else None,
            "superseded_old": superseded,
            "memory_hit": memory_hit(results),
            "answer": answer,
        }


def resume() -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        memory = MemoryStore(Path(tmp) / "memory.json")
        checkpoint = Path(tmp) / "checkpoint.json"
        config = SessionConfig(
            max_context_tokens=5000,
            enable_compaction=False,
            checkpoint_path=checkpoint,
        )
        first = SessionAgent.new("resume", memory, config)
        first.run_turn("order-service 5xx，帮我查一下。")
        first.run_turn("日志里有什么错误？")

        # Simulated crash: a new process resumes from the checkpoint.
        resumed = SessionAgent.resume(checkpoint, memory, config)
        before = {
            "error_code": resumed.state.get_fact("error_code").value
            if resumed.state.get_fact("error_code")
            else None
        }
        results, _ = run_turns(
            resumed,
            [
                "根因是什么？",
                "帮我重启 order-service",
                "帮我重启 order-service",
                "给我最终结论。",
            ],
        )
        restart_requests = sum(
            1
            for item in resumed.state.tool_results
            if item.get("tool") == "restart_service"
        )
        executed_restarts = sum(
            1
            for item in resumed.state.tool_results
            if item.get("tool") == "restart_service"
            and "status=restarted" in item.get("summary", "")
        )
        duplicate_side_effects = max(0, executed_restarts - 1)
        answer = final_answer(resumed, results)
        return {
            "scenario": "resume",
            "mode": "checkpoint",
            "completed": results[-1].action != "overflow",
            "task_success": "N+1" in answer,
            "resume_success": before.get("error_code") == "HikariPool timeout",
            "side_effect_count": len(resumed.state.side_effects),
            "restart_requests": restart_requests,
            "duplicate_side_effects": duplicate_side_effects,
            "answer": answer,
        }


def short(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def print_table(rows: List[Dict[str, Any]]) -> None:
    columns = [
        ("scenario", 16),
        ("mode", 13),
        ("completed", 9),
        ("task", 6),
        ("prompt", 7),
        ("peak", 6),
        ("comp", 5),
        ("drop", 5),
        ("mem", 5),
        ("stale", 6),
        ("resume", 7),
        ("dup", 4),
    ]
    header = " ".join(name.ljust(width) for name, width in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        values = {
            "scenario": row.get("scenario"),
            "mode": row.get("mode"),
            "completed": row.get("completed"),
            "task": row.get("task_success"),
            "prompt": row.get("prompt_retained"),
            "peak": row.get("peak_tokens"),
            "comp": row.get("compactions"),
            "drop": row.get("dropped_messages"),
            "mem": row.get("memory_hit"),
            "stale": row.get("superseded_old"),
            "resume": row.get("resume_success"),
            "dup": row.get("duplicate_side_effects"),
        }
        print(
            " ".join(
                short(values[name]).ljust(width) for name, width in columns
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="State/memory eval (lesson 0005)")
    parser.add_argument(
        "--scenario",
        choices=["all", "long", "preference", "stale", "resume"],
        default="all",
    )
    parser.add_argument("--threshold", type=float, default=0.70)
    parser.add_argument("--keep-recent", type=int, default=4)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    rows: List[Dict[str, Any]] = []
    if args.scenario in ("all", "long"):
        rows.extend(
            [
                long_incident("none", args.threshold, args.keep_recent),
                long_incident("compaction", args.threshold, args.keep_recent),
                long_incident("full", args.threshold, args.keep_recent),
            ]
        )
    if args.scenario in ("all", "preference"):
        rows.extend([preference(False), preference(True)])
    if args.scenario in ("all", "stale"):
        rows.extend([stale_memory(False), stale_memory(True)])
    if args.scenario in ("all", "resume"):
        rows.append(resume())

    print("=== lesson 0005 eval ===")
    print(
        "long_incident: 18 turns · budget 800 tokens · threshold=%.2f · keep_recent=%d"
        % (args.threshold, args.keep_recent)
    )
    print()
    print_table(rows)

    if args.scenario in ("all", "long"):
        print()
        print("long_incident detail:")
        for row in rows[:3]:
            print(
                "  %-12s completed=%s task=%s prompt_retained=%s peak=%s final=%s compactions=%s dropped=%s notes_has_facts=%s"
                % (
                    row["mode"],
                    short(row["completed"]),
                    short(row["task_success"]),
                    short(row["prompt_retained"]),
                    row["peak_tokens"],
                    row["final_tokens"],
                    row["compactions"],
                    row["dropped_messages"],
                    short(row["notes_has_facts"]),
                )
            )

    if args.scenario in ("all", "preference"):
        print()
        print("preference detail:")
        for row in rows:
            if row.get("scenario") == "preference":
                print(
                    "  %-12s memory_hit=%s first_tool=%-18s task=%s"
                    % (
                        row["mode"],
                        short(row["memory_hit"]),
                        row.get("first_tool") or "-",
                        short(row["task_success"]),
                    )
                )

    out_path = Path(args.out) if args.out else ROOT / "reports" / "memory-eval.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print()
    print("report: %s" % out_path)


if __name__ == "__main__":
    main()
