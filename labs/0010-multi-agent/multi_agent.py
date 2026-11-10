"""Lesson 0010: supervisor, handoff, and single-agent baselines.

The lab is a deterministic simulation. No API key and no model call are
required: the fixtures stand in for model decisions so the *architecture*
is the only variable. Each pattern is compiled as a LangGraph StateGraph.

Patterns
--------
single      One agent with all tools. Its context accumulates every tool
            output; when the simulated context budget overflows, the oldest
            observation is dropped.

supervisor  The lead keeps control. It writes a delegation contract, invokes
            a specialist subgraph, and receives only a summary + evidence.
            The worker gets a fresh context and never sees the lead's state.

handoff     Control transfers: triage sends the conversation to a specialist
            with Command(goto=...). Each specialist sees the shared history
            and may transfer control again.

Run:
    python3 multi_agent.py eval
    python3 multi_agent.py demo --pattern supervisor --scenario breadth
    python3 multi_agent.py trace --pattern handoff --scenario simple
"""
from __future__ import annotations

import argparse
import json
import operator
from typing import Annotated, Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

# ---------------------------------------------------------------- simulation

FLAT_MAX_CONTEXT = 1200

SUPERVISOR_CALL = 150
DELEGATION_CONTRACT = 300
WORKER_PROMPT = 120
WORKER_SCHEMA = 120
WORKER_SUMMARY = 100
SYNTHESIS = 150

FLAT_PROMPT = 100
TOOL_SCHEMA = 120
FLAT_SYNTHESIS = 100

HANDOFF_PROMPT = 100
HANDOFF_SYNTHESIS = 100


def estimate_tokens(text: Any) -> int:
    """Cheap deterministic proxy: roughly half a token per character.

    Real tokenizers differ by language and model. This lab only needs a
    stable ratio so the architecture comparison is reproducible.
    """
    return max(1, len(str(text)) // 2)


def _long_logs() -> str:
    lines = [
        "2026-11-10T20:00:00Z INFO  order-service started (build 4.8.2)",
        "2026-11-10T20:01:03Z INFO  HikariPool-1 - Starting...",
        "2026-11-10T20:02:11Z WARN  OrderQueryService - findRecentOrders took 1840ms",
    ]
    for i in range(40):
        lines.append(
            "2026-11-10T20:%02d:12Z WARN  HikariPool-1 - Connection is not "
            "available, request timed out after 30000ms (total=50, active=48, "
            "idle=2, waiting=7)" % i
        )
        lines.append(
            "    at OrderQueryService.findRecentOrders(OrderQueryService.java:142) "
            "~[order-service.jar:na]"
        )
    return "\n".join(lines)


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "simple": {
        "question": "order-service 连接池 48/50，错误率 0.8%，先看什么？",
        "domains": ["logs"],
        "logs": (
            "2026-11-10T20:05:12Z WARN HikariPool-1 - Connection is not available, "
            "request timed out after 30000ms (total=50, active=48, idle=2, waiting=7)\n"
            "    at OrderQueryService.findRecentOrders(OrderQueryService.java:142)\n"
            "2026-11-10T20:05:40Z WARN HikariPool-1 - pool=48/50 error_rate_5m=0.8%"
        ),
        "health": "pool=48/50, error_rate_5m=0.8%, p95=2.4s, restarts=0",
        "code": "OrderQueryService.findRecentOrders:142 loops over orders and calls "
                "orderRepository.findItems(orderId) per row (N+1).",
        "code_fallback": "git log: OrderQueryService.java changed 2026-11-08 in commit a1b2c3.",
        "runbook": "2026-10-24 同类事故：先重启缓解连接池耗尽，再修 N+1；重启需 on-call 审批。",
        "runbook_fallback": "incident index: 2026-10-24 order-service pool exhaustion.",
        "findings": {
            "logs": "20:05 起连接池 48/50，获取连接 30s 超时；等待队列 7，慢查询集中在 findRecentOrders。",
            "code": "findRecentOrders 在循环内逐行查询 order items，构成 N+1；与连接占用时间变长一致。",
            "runbook": "2026-10-24 同类事故记录：先审批重启缓解，再修 N+1；不要直接改连接池上限。",
        },
        "evidence": {
            "logs": ["log 20:05:12 HikariPool timeout 30000ms", "health pool 48/50, error_rate 0.8%"],
            "code": ["OrderQueryService.java:142 per-row query"],
            "runbook": ["incidents/2026-10-24-order-pool.md: restart then fix N+1"],
        },
    },
    "breadth": {
        "question": "order-service 连接池耗尽；OrderQueryService 疑似 N+1；历史上怎么处理？",
        "domains": ["logs", "code", "runbook"],
        "logs": _long_logs(),
        "health": "pool=48/50, error_rate_5m=0.8%, p95=2.4s, restarts=0",
        "code": "OrderQueryService.findRecentOrders:142 loops over orders and calls "
                "orderRepository.findItems(orderId) per row (N+1). "
                "OrderQueryService.findRecentOrders:150 also opens a new transaction per row.",
        "code_fallback": "git log: OrderQueryService.java changed 2026-11-08 in commit a1b2c3.",
        "runbook": "2026-10-24 同类事故：先重启缓解连接池耗尽，再修 N+1；重启需 on-call 审批。"
                   "2026-09-18 另一次事故由连接池上限 50 过低触发，但先修 N+1 后未再复现。",
        "runbook_fallback": "incident index: 2026-10-24 order-service pool exhaustion.",
        "findings": {
            "logs": "40 次 HikariPool 30s 超时，pool 48/50，等待队列 7；堆栈全部指向 findRecentOrders:142。",
            "code": "findRecentOrders 在循环内逐行查询 order items，并对每行开启事务，构成 N+1 与连接占用放大。",
            "runbook": "同类事故标准处置：先审批重启缓解，再修 N+1；不要先调大连接池上限。",
        },
        "evidence": {
            "logs": ["40x HikariPool timeout 30000ms", "stack: OrderQueryService.java:142"],
            "code": ["OrderQueryService.java:142 per-row query", "OrderQueryService.java:150 per-row transaction"],
            "runbook": ["incidents/2026-10-24-order-pool.md: restart then fix N+1"],
        },
    },
    "retry": {
        "question": "order-service 连接池耗尽，日志后端不稳定，怎么继续？",
        "domains": ["logs", "code"],
        "logs": "2026-11-10T20:05:12Z WARN HikariPool-1 - Connection is not available, "
                "request timed out after 30000ms (total=50, active=48, idle=2, waiting=7)",
        "health": "pool=48/50, error_rate_5m=0.8%, p95=2.4s, restarts=0",
        "code": "OrderQueryService.findRecentOrders:142 loops over orders and calls "
                "orderRepository.findItems(orderId) per row (N+1).",
        "code_fallback": "git log: OrderQueryService.java changed 2026-11-08 in commit a1b2c3.",
        "runbook": "2026-10-24 同类事故：先重启缓解，再修 N+1。",
        "runbook_fallback": "incident index: 2026-10-24 order-service pool exhaustion.",
        "logs_fail": "once",
        "findings": {
            "logs": "日志后端首次超时；降级读取健康指标后确认 pool 48/50、error_rate 0.8%。",
            "code": "findRecentOrders 在循环内逐行查询 order items，构成 N+1。",
            "runbook": "同类事故记录：先审批重启缓解，再修 N+1。",
        },
        "evidence": {
            "logs": ["health pool 48/50, error_rate 0.8%", "retry: query_logs then check_health"],
            "code": ["OrderQueryService.java:142 per-row query"],
            "runbook": ["incidents/2026-10-24-order-pool.md"],
        },
    },
    "dead_tool": {
        "question": "order-service 连接池耗尽，日志工具完全不可用，能做多少？",
        "domains": ["logs", "code"],
        "logs": "",
        "health": "",
        "code": "OrderQueryService.findRecentOrders:142 loops over orders and calls "
                "orderRepository.findItems(orderId) per row (N+1).",
        "code_fallback": "git log: OrderQueryService.java changed 2026-11-08 in commit a1b2c3.",
        "runbook": "2026-10-24 同类事故：先重启缓解，再修 N+1。",
        "runbook_fallback": "incident index: 2026-10-24 order-service pool exhaustion.",
        "logs_fail": "always",
        "findings": {
            "logs": "日志工具不可用。",
            "code": "findRecentOrders 在循环内逐行查询 order items，构成 N+1。",
            "runbook": "同类事故记录：先审批重启缓解，再修 N+1。",
        },
        "evidence": {
            "logs": [],
            "code": ["OrderQueryService.java:142 per-row query"],
            "runbook": ["incidents/2026-10-24-order-pool.md"],
        },
    },
}

PRIMARY_TOOL = {"logs": "query_logs", "code": "search_code", "runbook": "search_runbook"}
FALLBACK_TOOL = {"logs": "check_health", "code": "git_log", "runbook": "search_incidents"}

# Each query is the delegation contract: scope, output shape, and the parent's
# already-decided constraints. A real implementation would add a JSON schema
# and a token/step budget next to these strings.
QUERIES = {
    "logs": "在 20:00–20:10 的 order-service 日志中找出连接池耗尽的直接证据，"
            "只返回 3 条带时间戳的日志与错误码；父级已决定暂不重启。",
    "code": "检查 OrderQueryService.findRecentOrders 是否存在 N+1，"
            "只返回调用链与行号；不要提出修复方案。",
    "runbook": "查找 2026-10-24 同类事故的处理记录，只返回缓解步骤与风险；不要重复诊断。",
}


def plan_tasks(question: str, scenario: str) -> List[Dict[str, str]]:
    return [{"agent": agent, "query": QUERIES[agent]} for agent in SCENARIOS[scenario]["domains"]]


def call_tool(agent: str, tool: str, scenario_name: str, attempt: int):
    """Return (output, ok, error). The failure schedule is part of the fixture."""
    scenario = SCENARIOS[scenario_name]
    if agent == "logs":
        if scenario.get("logs_fail") == "always":
            return "ERROR: log backend unavailable (connection refused)", False, "log backend unavailable"
        if tool == "check_health":
            return scenario["health"], True, ""
        if scenario.get("logs_fail") == "once" and attempt == 1:
            return "ERROR: log backend timed out after 5s", False, "log backend timed out"
        return scenario["logs"], True, ""
    if agent == "code":
        if tool == "git_log":
            return scenario["code_fallback"], True, ""
        return scenario["code"], True, ""
    if agent == "runbook":
        if tool == "search_incidents":
            return scenario["runbook_fallback"], True, ""
        return scenario["runbook"], True, ""
    raise ValueError("unknown agent: %s" % agent)


# ------------------------------------------------------------------ worker
# A worker is a tiny bounded graph. It has its own state schema: no question,
# no findings from other workers, no parent trace. That is the context
# isolation the lab measures.

class WorkerState(TypedDict):
    agent: str
    task: str
    scenario: str
    mode: str
    attempt: int
    tool: str
    observations: Annotated[List[str], operator.add]
    evidence: Annotated[List[str], operator.add]
    finding: str
    ok: bool
    error: str
    tool_calls: Annotated[int, operator.add]
    tokens: Annotated[int, operator.add]
    trace: Annotated[List[Dict[str, Any]], operator.add]


def worker_act(state: WorkerState) -> Dict[str, Any]:
    agent = state["agent"]
    attempt = state.get("attempt", 0) + 1
    tool = FALLBACK_TOOL[agent] if state.get("mode") == "fallback" else PRIMARY_TOOL[agent]
    if agent == "logs" and attempt >= 2:
        tool = "check_health"
    return {
        "attempt": attempt,
        "tool": tool,
        "tool_calls": 1,
        "tokens": WORKER_PROMPT + WORKER_SCHEMA,
        "trace": [{
            "node": "worker.act",
            "agent": agent,
            "attempt": attempt,
            "tool": tool,
            "context_keys": sorted(state.keys()),
        }],
    }


def worker_tool(state: WorkerState) -> Dict[str, Any]:
    output, ok, error = call_tool(
        state["agent"], state["tool"], state["scenario"], state.get("attempt", 1)
    )
    evidence = SCENARIOS[state["scenario"]]["evidence"][state["agent"]] if ok else []
    return {
        "observations": [output],
        "evidence": list(evidence),
        "ok": ok,
        "error": error,
        "tokens": estimate_tokens(output),
        "trace": [{
            "node": "worker.tool",
            "agent": state["agent"],
            "tool": state["tool"],
            "ok": ok,
            "error": error,
        }],
    }


def worker_summarize(state: WorkerState) -> Dict[str, Any]:
    if state.get("ok"):
        finding = SCENARIOS[state["scenario"]]["findings"][state["agent"]]
    else:
        finding = "未收集到 %s 证据：%s" % (state["agent"], state.get("error", "unknown"))
    return {
        "finding": finding,
        "tokens": WORKER_SUMMARY,
        "trace": [{
            "node": "worker.summarize",
            "agent": state["agent"],
            "ok": bool(state.get("ok")),
            "finding_tokens": estimate_tokens(finding),
        }],
    }


def worker_route(state: WorkerState) -> str:
    if state.get("ok"):
        return "summarize"
    if state.get("attempt", 0) < 2:
        return "act"
    return "summarize"


def build_worker() -> Any:
    builder = StateGraph(WorkerState)
    builder.add_node("act", worker_act)
    builder.add_node("tools", worker_tool)
    builder.add_node("summarize", worker_summarize)
    builder.add_edge(START, "act")
    builder.add_edge("act", "tools")
    builder.add_conditional_edges("tools", worker_route, {"act": "act", "summarize": "summarize"})
    builder.add_edge("summarize", END)
    return builder.compile()


WORKERS = {agent: build_worker() for agent in PRIMARY_TOOL}


def run_worker(agent: str, task: str, scenario: str, mode: str = "primary") -> Dict[str, Any]:
    return WORKERS[agent].invoke({
        "agent": agent,
        "task": task,
        "scenario": scenario,
        "mode": mode,
        "attempt": 0,
        "observations": [],
        "evidence": [],
        "finding": "",
        "ok": False,
        "error": "",
        "tool_calls": 0,
        "tokens": 0,
        "trace": [],
    })


# -------------------------------------------------------------- supervisor

class SupervisorState(TypedDict):
    question: str
    scenario: str
    plan: List[Dict[str, str]]
    next_task: Dict[str, str]
    attempted: Annotated[List[str], operator.add]
    findings: Annotated[List[Dict[str, Any]], operator.add]
    answer: str
    stop_reason: str
    coverage: float
    steps: Annotated[int, operator.add]
    tokens: Annotated[int, operator.add]
    trace: Annotated[List[Dict[str, Any]], operator.add]
    max_steps: int


def task_key(task: Dict[str, str]) -> str:
    return "%s:%s" % (task["agent"], task["query"])


def conclusion_for(covered: List[str], missing: List[str]) -> str:
    if missing:
        return "当前证据不足以给出完整根因；需要补充：%s。" % "、".join(missing)
    covered_set = set(covered)
    if {"code", "runbook"} <= covered_set:
        return "根因假设：findRecentOrders 的 N+1 拖长连接占用；先按 runbook 审批重启缓解，再修 N+1。"
    if "code" in covered_set:
        return "根因假设：findRecentOrders 的 N+1 拖长连接占用；还需要 runbook 确认处置步骤。"
    if "logs" in covered_set:
        return "根因线索：连接池已接近耗尽；建议继续查代码与 runbook，确认 N+1 与处置路径。"
    return "证据已收集，但还不足以形成根因假设。"


def supervisor_node(state: SupervisorState) -> Dict[str, Any]:
    attempted = set(state.get("attempted", []))
    if state.get("steps", 0) >= state.get("max_steps", 8):
        return {
            "next_task": {},
            "stop_reason": "budget_exhausted",
            "steps": 1,
            "tokens": SUPERVISOR_CALL,
            "trace": [{"node": "supervisor", "event": "budget_exhausted"}],
        }
    for task in state.get("plan", []):
        if task_key(task) not in attempted:
            return {
                "next_task": task,
                "attempted": [task_key(task)],
                "steps": 1,
                "tokens": SUPERVISOR_CALL,
                "trace": [{
                    "node": "supervisor",
                    "event": "delegate",
                    "task": task,
                    "context_keys": sorted(state.keys()),
                    "saw_other_findings": len(state.get("findings", [])),
                }],
            }
    return {
        "next_task": {},
        "stop_reason": "plan_done",
        "steps": 1,
        "tokens": SUPERVISOR_CALL,
        "trace": [{"node": "supervisor", "event": "plan_done"}],
    }


def supervisor_route(state: SupervisorState) -> str:
    return "delegate" if state.get("next_task") else "synthesize"


def delegate_node(state: SupervisorState) -> Dict[str, Any]:
    task = state["next_task"]
    result = run_worker(task["agent"], task["query"], state["scenario"], task.get("mode", "primary"))
    finding = {
        "agent": task["agent"],
        "query": task["query"],
        "finding": result.get("finding", ""),
        "evidence": result.get("evidence", []),
        "ok": result.get("ok", False),
        "error": result.get("error", ""),
        "tool_calls": result.get("tool_calls", 0),
        "tokens": result.get("tokens", 0),
    }
    return {
        "findings": [finding],
        "next_task": {},
        "tokens": DELEGATION_CONTRACT + finding["tokens"],
        "steps": 1,
        "trace": [{
            "node": "delegate",
            "agent": task["agent"],
            "worker_context_keys": sorted(result.keys()),
            "worker_saw_parent_findings": "findings" in result,
            "worker_saw_question": "question" in result,
            "finding_ok": finding["ok"],
        }],
    }


def synthesize_node(state: SupervisorState) -> Dict[str, Any]:
    plan = state.get("plan", [])
    last: Dict[str, Dict[str, Any]] = {}
    for finding in state.get("findings", []):
        last[finding["agent"]] = finding
    agents = [task["agent"] for task in plan]
    covered = [agent for agent in agents if last.get(agent, {}).get("ok")]
    coverage = len(covered) / len(agents) if agents else 0.0

    lines: List[str] = []
    for agent in agents:
        finding = last.get(agent)
        if finding and finding["ok"]:
            lines.append("- [%s] %s 证据：%s" % (
                agent, finding["finding"], "；".join(finding["evidence"]) or "（未提供）"
            ))
        else:
            error = finding.get("error") if finding else "未执行"
            lines.append("- [%s] 证据不足：%s" % (agent, error or "未提供"))
    missing = [agent for agent in agents if not last.get(agent, {}).get("ok")]
    conclusion = conclusion_for(covered, missing)

    if state.get("stop_reason") == "budget_exhausted":
        stop_reason = "budget_exhausted"
    elif coverage == 1.0:
        stop_reason = "complete"
    elif coverage > 0:
        stop_reason = "partial"
    else:
        stop_reason = "failed"

    answer = "\n".join(lines + ["", conclusion])
    return {
        "answer": answer,
        "coverage": coverage,
        "stop_reason": stop_reason,
        "tokens": SYNTHESIS + sum(estimate_tokens(line) for line in lines),
        "trace": [{
            "node": "synthesize",
            "covered": covered,
            "missing": missing,
            "stop_reason": stop_reason,
        }],
    }


def build_supervisor_graph() -> Any:
    builder = StateGraph(SupervisorState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("delegate", delegate_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor", supervisor_route, {"delegate": "delegate", "synthesize": "synthesize"}
    )
    builder.add_edge("delegate", "supervisor")
    builder.add_edge("synthesize", END)
    return builder.compile()


def run_supervisor(scenario: str, max_steps: int = 8) -> Dict[str, Any]:
    return build_supervisor_graph().invoke({
        "question": SCENARIOS[scenario]["question"],
        "scenario": scenario,
        "plan": plan_tasks(SCENARIOS[scenario]["question"], scenario),
        "next_task": {},
        "attempted": [],
        "findings": [],
        "answer": "",
        "stop_reason": "",
        "coverage": 0.0,
        "steps": 0,
        "tokens": 0,
        "trace": [],
        "max_steps": max_steps,
    })


# -------------------------------------------------------------- single agent

class FlatState(TypedDict):
    question: str
    scenario: str
    plan: List[Dict[str, str]]
    next_task: Dict[str, str]
    attempted: Annotated[List[str], operator.add]
    observations: Annotated[List[Dict[str, Any]], operator.add]
    dropped: Annotated[List[str], operator.add]
    answer: str
    stop_reason: str
    coverage: float
    steps: Annotated[int, operator.add]
    tokens: Annotated[int, operator.add]
    trace: Annotated[List[Dict[str, Any]], operator.add]
    max_context: int


def visible_observations(state: FlatState):
    dropped = set(state.get("dropped", []))
    visible = [obs for obs in state.get("observations", []) if obs["agent"] not in dropped]
    limit = state.get("max_context", FLAT_MAX_CONTEXT)
    while visible and (
        FLAT_PROMPT + 3 * TOOL_SCHEMA
        + sum(estimate_tokens(obs["output"]) for obs in visible)
    ) > limit:
        victim = visible.pop(0)
        dropped.add(victim["agent"])
    return visible, dropped


def flat_agent(state: FlatState) -> Dict[str, Any]:
    visible, dropped = visible_observations(state)
    new_dropped = [agent for agent in dropped if agent not in set(state.get("dropped", []))]
    attempted = set(state.get("attempted", []))
    for task in state.get("plan", []):
        if task_key(task) not in attempted:
            return {
                "next_task": task,
                "attempted": [task_key(task)],
                "dropped": new_dropped,
                "steps": 1,
                "tokens": FLAT_PROMPT + 3 * TOOL_SCHEMA
                + sum(estimate_tokens(obs["output"]) for obs in visible),
                "trace": [{
                    "node": "flat.agent",
                    "event": "tool_call",
                    "visible_agents": [obs["agent"] for obs in visible],
                    "dropped": sorted(dropped),
                    "context_tokens": FLAT_PROMPT + 3 * TOOL_SCHEMA
                    + sum(estimate_tokens(obs["output"]) for obs in visible),
                }],
            }

    agents = [task["agent"] for task in state.get("plan", [])]
    visible_agents = [obs["agent"] for obs in visible if obs["ok"]]
    coverage = len(set(visible_agents)) / len(agents) if agents else 0.0
    lines: List[str] = []
    for agent in agents:
        observation = next((obs for obs in visible if obs["agent"] == agent), None)
        if observation and observation["ok"]:
            lines.append("- [%s] %s" % (agent, observation["finding"]))
        else:
            lines.append("- [%s] 证据不足：%s" % (agent, "上下文已丢弃" if agent in dropped else "未收集"))
    missing = [agent for agent in agents if agent not in visible_agents]
    conclusion = conclusion_for(visible_agents, missing)
    answer = "\n".join(lines + ["", conclusion])
    return {
        "answer": answer,
        "coverage": coverage,
        "stop_reason": "complete" if coverage == 1.0 else ("partial" if coverage else "failed"),
        "dropped": new_dropped,
        "steps": 1,
        "tokens": FLAT_SYNTHESIS + sum(estimate_tokens(line) for line in lines),
        "trace": [{
            "node": "flat.agent",
            "event": "synthesize",
            "visible_agents": [obs["agent"] for obs in visible],
            "dropped": sorted(dropped),
            "coverage": coverage,
        }],
    }


def flat_route(state: FlatState) -> str:
    return "tools" if state.get("next_task") else END


def flat_tools(state: FlatState) -> Dict[str, Any]:
    task = state["next_task"]
    agent = task["agent"]
    output, ok, error = call_tool(agent, PRIMARY_TOOL[agent], state["scenario"], attempt=1)
    tokens = estimate_tokens(output)
    if not ok:
        fallback_output, fallback_ok, fallback_error = call_tool(
            agent, FALLBACK_TOOL[agent], state["scenario"], attempt=2
        )
        tokens += estimate_tokens(fallback_output)
        if fallback_ok:
            output, ok, error = fallback_output, True, ""
        else:
            error = fallback_error
    observation = {
        "agent": agent,
        "output": output,
        "finding": SCENARIOS[state["scenario"]]["findings"][agent] if ok else "",
        "ok": ok,
        "error": error,
        "tokens": tokens,
    }
    return {
        "observations": [observation],
        "next_task": {},
        "steps": 1,
        "tokens": tokens + FLAT_PROMPT,
        "trace": [{
            "node": "flat.tools",
            "agent": agent,
            "ok": ok,
            "output_tokens": estimate_tokens(output),
            "error": error,
        }],
    }


def build_flat_graph() -> Any:
    builder = StateGraph(FlatState)
    builder.add_node("agent", flat_agent)
    builder.add_node("tools", flat_tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", flat_route, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder.compile()


def run_flat(scenario: str, max_context: int = FLAT_MAX_CONTEXT) -> Dict[str, Any]:
    return build_flat_graph().invoke({
        "question": SCENARIOS[scenario]["question"],
        "scenario": scenario,
        "plan": plan_tasks(SCENARIOS[scenario]["question"], scenario),
        "next_task": {},
        "attempted": [],
        "observations": [],
        "dropped": [],
        "answer": "",
        "stop_reason": "",
        "coverage": 0.0,
        "steps": 0,
        "tokens": 0,
        "trace": [],
        "max_context": max_context,
    })


# ----------------------------------------------------------------- handoff

class HandoffState(TypedDict):
    question: str
    scenario: str
    plan: List[Dict[str, str]]
    active_agent: str
    task: str
    findings: Annotated[List[Dict[str, Any]], operator.add]
    history: Annotated[List[Dict[str, str]], operator.add]
    answer: str
    stop_reason: str
    coverage: float
    handoffs: Annotated[int, operator.add]
    triage_runs: Annotated[int, operator.add]
    steps: Annotated[int, operator.add]
    tokens: Annotated[int, operator.add]
    trace: Annotated[List[Dict[str, Any]], operator.add]


def triage_node(state: HandoffState) -> Command:
    first = state["plan"][0]["agent"]
    return Command(
        goto="%s_agent" % first,
        update={
            "active_agent": first,
            "task": state["question"],
            "triage_runs": 1,
            "handoffs": 1,
            "steps": 1,
            "history": [{"role": "assistant", "content": "triage -> %s" % first}],
            "tokens": HANDOFF_PROMPT,
            "trace": [{
                "node": "triage",
                "from": "triage",
                "to": first,
                "context_keys": sorted(state.keys()),
            }],
        },
    )


def make_specialist(agent: str):
    def specialist(state: HandoffState) -> Command:
        result = run_worker(agent, state["question"], state["scenario"])
        finding = {
            "agent": agent,
            "query": QUERIES[agent],
            "finding": result.get("finding", ""),
            "evidence": result.get("evidence", []),
            "ok": result.get("ok", False),
            "error": result.get("error", ""),
            "tool_calls": result.get("tool_calls", 0),
            "tokens": result.get("tokens", 0),
        }
        all_findings = list(state.get("findings", [])) + [finding]
        done = {item["agent"] for item in all_findings if item["ok"]} | {agent}
        next_agent = next(
            (task["agent"] for task in state["plan"] if task["agent"] not in done),
            None,
        )
        history_item = {
            "role": "assistant",
            "content": "[%s] %s" % (agent, finding["finding"]),
        }
        base = {
            "findings": [finding],
            "history": [history_item],
            "steps": 1,
            "tokens": HANDOFF_PROMPT + finding["tokens"]
            + sum(estimate_tokens(item["content"]) for item in state.get("history", [])),
            "trace": [{
                "node": "%s_agent" % agent,
                "from": state.get("active_agent"),
                "to": next_agent or "done",
                "saw_history_len": len(state.get("history", [])),
                "saw_prior_findings": len(state.get("findings", [])),
                "finding_ok": finding["ok"],
            }],
        }
        if next_agent:
            return Command(
                goto="%s_agent" % next_agent,
                update={**base, "active_agent": next_agent, "handoffs": 1},
            )
        return Command(goto="done", update={**base, "active_agent": "done"})

    return specialist


def handoff_done(state: HandoffState) -> Dict[str, Any]:
    plan_agents = [task["agent"] for task in state["plan"]]
    last: Dict[str, Dict[str, Any]] = {}
    for finding in state.get("findings", []):
        last[finding["agent"]] = finding
    covered = [agent for agent in plan_agents if last.get(agent, {}).get("ok")]
    coverage = len(covered) / len(plan_agents) if plan_agents else 0.0
    lines: List[str] = []
    for agent in plan_agents:
        finding = last.get(agent)
        if finding and finding["ok"]:
            lines.append("- [%s] %s" % (agent, finding["finding"]))
        else:
            lines.append("- [%s] 证据不足：%s" % (agent, finding.get("error") if finding else "未执行"))
    missing = [agent for agent in plan_agents if agent not in covered]
    conclusion = conclusion_for(covered, missing)
    return {
        "answer": "\n".join(lines + ["", conclusion]),
        "coverage": coverage,
        "stop_reason": "complete" if coverage == 1.0 else "partial",
        "steps": 1,
        "tokens": HANDOFF_SYNTHESIS + sum(estimate_tokens(line) for line in lines),
        "trace": [{
            "node": "done",
            "covered": covered,
            "missing": missing,
            "triage_runs": state.get("triage_runs", 0),
            "handoffs": state.get("handoffs", 0),
        }],
    }


def build_handoff_graph() -> Any:
    builder = StateGraph(HandoffState)
    builder.add_node("triage", triage_node)
    for agent in PRIMARY_TOOL:
        builder.add_node("%s_agent" % agent, make_specialist(agent))
    builder.add_node("done", handoff_done)
    builder.add_edge(START, "triage")
    builder.add_edge("done", END)
    return builder.compile()


def run_handoff(scenario: str) -> Dict[str, Any]:
    return build_handoff_graph().invoke({
        "question": SCENARIOS[scenario]["question"],
        "scenario": scenario,
        "plan": plan_tasks(SCENARIOS[scenario]["question"], scenario),
        "active_agent": "",
        "task": "",
        "findings": [],
        "history": [],
        "answer": "",
        "stop_reason": "",
        "coverage": 0.0,
        "handoffs": 0,
        "triage_runs": 0,
        "steps": 0,
        "tokens": 0,
        "trace": [],
    })


# -------------------------------------------------------------------- CLI

RUNNERS = {
    "single": run_flat,
    "supervisor": run_supervisor,
    "handoff": run_handoff,
}


def summarize(pattern: str, scenario: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pattern": pattern,
        "scenario": scenario,
        "steps": result.get("steps", 0),
        "tokens": result.get("tokens", 0),
        "coverage": result.get("coverage", 0.0),
        "stop": result.get("stop_reason", ""),
        "handoffs": result.get("handoffs", 0),
        "dropped": ",".join(result.get("dropped", [])) or "-",
    }


def print_table(rows: List[Dict[str, Any]]) -> None:
    headers = ["scenario", "pattern", "steps", "tokens", "coverage", "stop", "handoffs", "dropped"]
    widths = {key: len(key) for key in headers}
    for row in rows:
        for key in headers:
            widths[key] = max(widths[key], len(str(row.get(key, ""))))
    line = "  ".join(key.ljust(widths[key]) for key in headers)
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(row.get(key, "")).ljust(widths[key]) for key in headers))


def cmd_eval(_args: argparse.Namespace) -> None:
    rows = []
    for scenario in ("simple", "breadth", "retry", "dead_tool"):
        for pattern in ("single", "supervisor", "handoff"):
            result = RUNNERS[pattern](scenario)
            row = summarize(pattern, scenario, result)
            row["coverage"] = "%.2f" % row["coverage"]
            rows.append(row)
    print_table(rows)


def cmd_demo(args: argparse.Namespace) -> None:
    result = RUNNERS[args.pattern](args.scenario)
    print("pattern:", args.pattern)
    print("scenario:", args.scenario)
    print("tokens:", result.get("tokens", 0))
    print("steps:", result.get("steps", 0))
    print("coverage:", "%.2f" % result.get("coverage", 0.0))
    print("stop_reason:", result.get("stop_reason", ""))
    print("dropped:", result.get("dropped", []))
    print()
    print(result.get("answer", ""))


def cmd_trace(args: argparse.Namespace) -> None:
    result = RUNNERS[args.pattern](args.scenario)
    print(json.dumps({
        "summary": summarize(args.pattern, args.scenario, result),
        "findings": result.get("findings", []),
        "trace": result.get("trace", []),
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Lesson 0010: multi-agent patterns")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("eval", help="compare single / supervisor / handoff on all fixtures")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("demo", help="run one pattern on one scenario")
    p.add_argument("--pattern", choices=sorted(RUNNERS), default="supervisor")
    p.add_argument("--scenario", choices=sorted(SCENARIOS), default="breadth")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("trace", help="dump the delegation trace as JSON")
    p.add_argument("--pattern", choices=sorted(RUNNERS), default="supervisor")
    p.add_argument("--scenario", choices=sorted(SCENARIOS), default="breadth")
    p.set_defaults(func=cmd_trace)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
