"""Lesson 0009: a durable, interruptible RepoMedic approval flow.

The graph is deliberately small:

    START -> diagnose -> approval -(approved)-> execute -> report -> END
                              \\-(rejected/invalid)-> report -> END

`approval` calls `interrupt()` and therefore needs a checkpointer and a
thread_id. `execute` performs a side effect in an external ledger, keyed by
`approval_id`, so a crash after the write does not repeat it on resume.

Run:
    python3 graph_agent.py mermaid
    python3 graph_agent.py start  --thread incident-42
    python3 graph_agent.py resume --thread incident-42 --decision approve \
        --approver alice --approval-id APR-7
    python3 graph_agent.py continue --thread incident-42
"""
from __future__ import annotations

import argparse
import json
import operator
import sqlite3
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Tuple, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from effects import EffectStore


class SimulatedCrash(RuntimeError):
    """Raised after the side effect to simulate a process dying mid-node."""


class IncidentState(TypedDict):
    """State that survives the process because it is checkpointed.

    `evidence`, `effects`, `audit`, `diagnose_runs` and `execute_runs` use
    `operator.add`. `proposal`, `approval`, `answer` and `stop_reason` use the
    default LastValue channel.
    """

    incident_id: str
    question: str
    evidence: Annotated[List[str], operator.add]
    proposal: Dict[str, Any]
    approval: Dict[str, Any]
    effects: Annotated[List[Dict[str, Any]], operator.add]
    audit: Annotated[List[Dict[str, Any]], operator.add]
    diagnose_runs: Annotated[int, operator.add]
    execute_runs: Annotated[int, operator.add]
    answer: str
    stop_reason: str


def initial_state(
    incident_id: str = "INC-42",
    question: str = "order-service 连接池耗尽，是否重启？",
) -> IncidentState:
    return {
        "incident_id": incident_id,
        "question": question,
        "evidence": [],
        "proposal": {},
        "approval": {},
        "effects": [],
        "audit": [],
        "diagnose_runs": 0,
        "execute_runs": 0,
        "answer": "",
        "stop_reason": "",
    }


def config_for(thread_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def normalize_decision(value: Any) -> Dict[str, Any]:
    """Validate the human decision before it can unlock a risky action.

    An approval without an approver and an approval_id is not an approval:
    it cannot be audited or used as an idempotency key. Reject it explicitly
    instead of silently executing.
    """
    if not isinstance(value, dict):
        return {
            "approved": False,
            "approver": "",
            "approval_id": "",
            "note": "resume value must be a dict",
            "valid": False,
        }

    approved = bool(value.get("approved"))
    approver = str(value.get("approver") or "").strip()
    approval_id = str(value.get("approval_id") or "").strip()
    note = str(value.get("note") or "").strip()
    valid = True

    if approved and (not approver or not approval_id):
        valid = False
        approved = False
        note = "approval requires both approver and approval_id"

    return {
        "approved": approved,
        "approver": approver,
        "approval_id": approval_id,
        "note": note,
        "valid": valid,
    }


def diagnose_node(state: IncidentState) -> Dict[str, Any]:
    """Collect evidence and propose one high-risk action. No side effects."""
    writer = get_stream_writer()
    writer({"node": "diagnose", "progress": 25, "message": "collecting evidence"})

    return {
        "evidence": [
            "db_pool=48/50 (source: health.json)",
            "error_rate_5m=0.8% (source: health.json)",
            "runbook=N+1 in OrderQueryService.findRecentOrders (source: incidents/2026-10-24-order-pool.md)",
        ],
        "proposal": {
            "action": "restart_service",
            "service": "order-service",
            "reason": "连接池接近耗尽；先重启缓解，根因修复另开任务",
            "risk": "high",
        },
        "diagnose_runs": 1,
        "audit": [
            {
                "node": "diagnose",
                "event": "proposal_ready",
                "action": "restart_service",
            }
        ],
    }


def approval_node(state: IncidentState) -> Dict[str, Any]:
    """Pause the graph. The node restarts from line one when resumed."""
    writer = get_stream_writer()
    writer({"node": "approval", "progress": 50, "message": "waiting for human"})

    decision = interrupt(
        {
            "type": "approval_required",
            "incident_id": state["incident_id"],
            "proposal": state["proposal"],
            "question": "是否批准执行 restart_service？",
        }
    )
    approval = normalize_decision(decision)

    return {
        "approval": approval,
        "audit": [
            {
                "node": "approval",
                "event": "approved" if approval["approved"] else "rejected",
                "approver": approval["approver"],
                "approval_id": approval["approval_id"],
                "valid": approval["valid"],
            }
        ],
    }


def route_after_approval(state: IncidentState) -> str:
    approval = state.get("approval") or {}
    if approval.get("approved") and approval.get("valid", True):
        return "execute"
    return "report"


def make_execute_node(effects: EffectStore, crash_after_effect: bool = False):
    def execute_node(state: IncidentState) -> Dict[str, Any]:
        approval = state["approval"]
        service = state["proposal"]["service"]
        writer = get_stream_writer()
        writer(
            {
                "node": "execute",
                "progress": 75,
                "message": "executing restart_service",
            }
        )

        # The external write happens before the node returns. A crash here
        # leaves the checkpoint at the previous super-step, so this node will
        # run again on resume. The idempotency key is what makes that safe.
        result = effects.restart(service=service, approval_id=approval["approval_id"])

        if crash_after_effect:
            raise SimulatedCrash(
                "simulated process crash after restart_service was recorded"
            )

        return {
            "effects": [result],
            "execute_runs": 1,
            "audit": [
                {
                    "node": "execute",
                    "event": result["status"],
                    "idempotency_key": result["idempotency_key"],
                }
            ],
            "stop_reason": "executed" if result["status"] == "restarted" else "already_executed",
        }

    return execute_node


def report_node(state: IncidentState) -> Dict[str, Any]:
    approval = state.get("approval") or {}
    effects = state.get("effects") or []

    if effects:
        last = effects[-1]
        if last["status"] == "restarted":
            answer = (
                "已按审批 %s 执行 restart_service(%s)。副作用已写入外部台账。"
                % (last["approval_id"], last["service"])
            )
            stop_reason = "executed"
        else:
            answer = (
                "审批 %s 对应的 restart_service 已执行过；本次 resume 命中幂等键 %s，"
                "没有重复重启。"
                % (last["approval_id"], last["idempotency_key"])
            )
            stop_reason = "already_executed"
    elif approval.get("approved"):
        answer = "审批已通过，但没有找到副作用记录；请检查 execute 节点。"
        stop_reason = "error"
    elif approval and not approval.get("valid", True):
        answer = "审批输入无效：%s。未执行任何写操作。" % approval.get("note", "")
        stop_reason = "invalid_approval"
    elif approval:
        who = approval.get("approver") or "未知审批人"
        note = approval.get("note") or "无备注"
        answer = "审批被 %s 拒绝（%s）。未执行 restart_service。" % (who, note)
        stop_reason = "rejected"
    else:
        answer = "图到达 report 时没有审批决定；这是一个状态错误。"
        stop_reason = "error"

    return {
        "answer": answer,
        "stop_reason": stop_reason,
        "audit": [{"node": "report", "event": stop_reason}],
    }


def build_graph(
    effects: EffectStore,
    checkpointer: Optional[Any] = None,
    crash_after_effect: bool = False,
):
    """Compile the incident graph. A checkpointer is required for interrupt()."""
    builder = StateGraph(IncidentState)
    builder.add_node("diagnose", diagnose_node)
    builder.add_node("approval", approval_node)
    builder.add_node(
        "execute",
        make_execute_node(effects, crash_after_effect=crash_after_effect),
    )
    builder.add_node("report", report_node)

    builder.add_edge(START, "diagnose")
    builder.add_edge("diagnose", "approval")
    builder.add_conditional_edges(
        "approval",
        route_after_approval,
        {"execute": "execute", "report": "report"},
    )
    builder.add_edge("execute", "report")
    builder.add_edge("report", END)

    return builder.compile(checkpointer=checkpointer)


def open_graph(
    db_path: Path,
    effects_path: Path,
    crash_after_effect: bool = False,
) -> Tuple[Any, sqlite3.Connection]:
    """Open a SQLite-backed graph. The connection must stay open while used."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    graph = build_graph(
        EffectStore(effects_path),
        SqliteSaver(conn),
        crash_after_effect=crash_after_effect,
    )
    return graph, conn


def print_interrupt(result: Dict[str, Any]) -> None:
    for item in result.get("__interrupt__") or ():
        print("interrupt id:", item.id)
        print(json.dumps(item.value, ensure_ascii=False, indent=2))


def print_result(graph: Any, config: Dict[str, Any], result: Dict[str, Any]) -> None:
    snapshot = graph.get_state(config)
    print("next:", snapshot.next)
    if result.get("__interrupt__"):
        print("status: interrupted")
        print_interrupt(result)
        print("thread is parked; use resume/continue with the same thread_id")
        return

    print("status: finished")
    print("stop_reason:", result.get("stop_reason"))
    print("answer:", result.get("answer"))
    print("approval:", json.dumps(result.get("approval"), ensure_ascii=False))
    print("effects:", json.dumps(result.get("effects"), ensure_ascii=False))
    print(
        "diagnose_runs=%d execute_runs=%d"
        % (result.get("diagnose_runs", 0), result.get("execute_runs", 0))
    )


def print_stream_part(part: Dict[str, Any]) -> None:
    kind = part.get("type")
    data = part.get("data")
    if kind == "custom":
        print("  custom:", json.dumps(data, ensure_ascii=False))
    elif kind == "updates":
        for node, update in (data or {}).items():
            if node == "__interrupt__":
                print("  updates: __interrupt__")
            else:
                print("  updates: %s -> %s" % (node, json.dumps(update, ensure_ascii=False)))
    else:
        print("  %s: %s" % (kind, json.dumps(data, ensure_ascii=False)))


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=".lab/checkpoints.db", help="SQLite checkpoint file")
    parser.add_argument("--effects", default=".lab/effects.json", help="external effect ledger")
    parser.add_argument("--thread", default="incident-42", help="thread_id used to resume")


def cmd_mermaid(args: argparse.Namespace) -> None:
    graph = build_graph(EffectStore(Path(args.effects)), InMemorySaver())
    print(graph.get_graph().draw_mermaid())


def cmd_start(args: argparse.Namespace) -> None:
    graph, conn = open_graph(Path(args.db), Path(args.effects))
    try:
        config = config_for(args.thread)
        if graph.get_state(config).next:
            print("thread already has pending work; use resume/continue", file=sys.stderr)
            raise SystemExit(2)
        result = graph.invoke(
            initial_state(args.incident, args.question),
            config,
            durability="sync",
        )
        print_result(graph, config, result)
    finally:
        conn.close()


def cmd_resume(args: argparse.Namespace) -> None:
    graph, conn = open_graph(
        Path(args.db),
        Path(args.effects),
        crash_after_effect=args.crash_after_effect,
    )
    try:
        config = config_for(args.thread)
        approval_id = args.approval_id
        if args.decision == "approve" and not approval_id:
            approval_id = "APR-%d" % int(time.time())
            print("no --approval-id supplied; generated", approval_id)

        decision = {
            "approved": args.decision == "approve",
            "approver": args.approver,
            "approval_id": approval_id,
            "note": args.note,
        }
        try:
            result = graph.invoke(Command(resume=decision), config, durability="sync")
        except SimulatedCrash as exc:
            print("process crashed:", exc)
            snapshot = graph.get_state(config)
            print("checkpoint still points at:", snapshot.next)
            print("external effect ledger:", json.dumps(
                EffectStore(Path(args.effects)).items(), ensure_ascii=False
            ))
            raise SystemExit(3)
        print_result(graph, config, result)
    finally:
        conn.close()


def cmd_continue(args: argparse.Namespace) -> None:
    graph, conn = open_graph(Path(args.db), Path(args.effects))
    try:
        config = config_for(args.thread)
        result = graph.invoke(None, config, durability="sync")
        print_result(graph, config, result)
    finally:
        conn.close()


def cmd_stream_demo(args: argparse.Namespace) -> None:
    graph, conn = open_graph(Path(args.db), Path(args.effects))
    try:
        thread = args.thread
        if thread == "incident-42":
            thread = "stream-demo-%d" % int(time.time())
        config = config_for(thread)
        print("thread:", thread)
        print("--- start (until interrupt) ---")
        for part in graph.stream(
            initial_state(args.incident, args.question),
            config,
            stream_mode=["updates", "custom"],
            version="v2",
            durability="sync",
        ):
            print_stream_part(part)
        print("--- resume (approve) ---")
        decision = {
            "approved": True,
            "approver": args.approver or "alice",
            "approval_id": args.approval_id or "APR-STREAM-1",
            "note": "stream demo",
        }
        for part in graph.stream(
            Command(resume=decision),
            config,
            stream_mode=["updates", "custom"],
            version="v2",
            durability="sync",
        ):
            print_stream_part(part)
        print_result(graph, config, graph.get_state(config).values)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lesson 0009 durable agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("mermaid", help="print the graph structure")
    p.add_argument("--effects", default=".lab/effects.json")
    p.set_defaults(func=cmd_mermaid)

    p = sub.add_parser("start", help="start a new incident thread and pause for approval")
    add_common_args(p)
    p.add_argument("--incident", default="INC-42")
    p.add_argument("--question", default="order-service 连接池耗尽，是否重启？")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("resume", help="resume a parked thread with an approval decision")
    add_common_args(p)
    p.add_argument("--decision", choices=["approve", "reject"], required=True)
    p.add_argument("--approver", default="")
    p.add_argument("--approval-id", default="")
    p.add_argument("--note", default="")
    p.add_argument(
        "--crash-after-effect",
        action="store_true",
        help="simulate a process crash after the external restart is recorded",
    )
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("continue", help="resume after a crash with invoke(None, config)")
    add_common_args(p)
    p.set_defaults(func=cmd_continue)

    p = sub.add_parser("stream-demo", help="show stream_mode updates + custom events")
    add_common_args(p)
    p.add_argument("--incident", default="INC-STREAM")
    p.add_argument("--question", default="order-service 连接池耗尽，是否重启？")
    p.add_argument("--approver", default="alice")
    p.add_argument("--approval-id", default="APR-STREAM-1")
    p.set_defaults(func=cmd_stream_demo)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
