"""Lesson 0008: the lesson-0003 ReAct loop, rebuilt as a LangGraph StateGraph.

Run:
    python3 graph_agent.py --demo
    python3 graph_agent.py --mermaid
    python3 graph_agent.py --max-rounds 1

The point is not to write fewer lines. The point is to make state channels,
reducers, routing, and stop conditions explicit objects that can be tested,
visualized, and (next lesson) checkpointed.
"""
from __future__ import annotations

import argparse
import json
import operator
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from llm import DEMO_TURNS, ScriptedLLM
from tools import ToolRegistry, build_registry

SYSTEM_PROMPT = """你是一个谨慎的 RepoMedic 排障助手。你可以调用工具获取事实。

规则：
1. 需要外部信息时先调用工具，不要猜。
2. 拿到观察结果后再决定下一步；每次只调用必要的工具。
3. 工具返回 ERROR 时，解释错误并换一种方法，不要假装成功。
4. 最终回答用中文，简洁，并说明依据了哪些工具结果。
"""


class AgentState(TypedDict):
    """The graph's shared state.

    Each annotated field is a channel. The annotation decides how a node's
    partial update is merged into the current value:

    - ``messages`` uses ``add_messages``: append, update by id, dedupe.
    - ``rounds`` uses ``operator.add``: every model call adds 1.
    - ``tool_events`` uses ``operator.add``: every tools call appends events.

    Unannotated fields (``answer``, ``stop_reason``, ``max_rounds``) use the
    default LastValue channel: the newest update replaces the old value.
    """

    messages: Annotated[List[AnyMessage], add_messages]
    rounds: Annotated[int, operator.add]
    tool_events: Annotated[List[Dict[str, Any]], operator.add]
    max_rounds: int
    question: str
    answer: str
    stop_reason: str


def initial_state(question: str, max_rounds: int = 6) -> AgentState:
    return {
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=question),
        ],
        "rounds": 0,
        "tool_events": [],
        "max_rounds": max_rounds,
        "question": question,
        "answer": "",
        "stop_reason": "",
    }


def make_model_node(llm: Any, registry: ToolRegistry):
    """A node is a function: state -> partial update."""

    def model_node(state: AgentState) -> Dict[str, Any]:
        response = llm.invoke(state["messages"], registry.schemas())
        # Return only the delta. add_messages appends it; rounds adds 1.
        return {"messages": [response], "rounds": 1}

    return model_node


def make_tools_node(registry: ToolRegistry):
    def tools_node(state: AgentState) -> Dict[str, Any]:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []
        messages: List[ToolMessage] = []
        events: List[Dict[str, Any]] = []

        for call in calls:
            result = registry.execute(call["name"], call["args"])
            # Every tool_call must get a matching ToolMessage, otherwise the
            # next model call sees an invalid message protocol.
            messages.append(
                ToolMessage(
                    content=result,
                    tool_call_id=call["id"],
                    name=call["name"],
                )
            )
            events.append(
                {
                    "round": state["rounds"],
                    "tool": call["name"],
                    "args": dict(call["args"]),
                    "result": result,
                }
            )

        return {"messages": messages, "tool_events": events}

    return tools_node


def finalize_node(state: AgentState) -> Dict[str, Any]:
    last = state["messages"][-1]
    content = last.content if isinstance(last.content, str) else str(last.content)
    return {"answer": content, "stop_reason": "done"}


def budget_exceeded_node(state: AgentState) -> Dict[str, Any]:
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []

    # Close the message protocol for tool calls we deliberately did not run.
    skipped = [
        ToolMessage(
            content="SKIPPED: max_rounds reached before this tool could run",
            tool_call_id=call["id"],
            name=call["name"],
        )
        for call in calls
    ]
    text = (
        "达到最大轮数 %d，Agent 已停止。最后一步的 %d 个工具调用没有执行；"
        "这是应用层的预算保护，不是模型给出的结论。"
        % (state["max_rounds"], len(calls))
    )
    return {
        "messages": skipped + [AIMessage(content=text)],
        "answer": text,
        "stop_reason": "max_rounds",
    }


def route_after_model(state: AgentState) -> str:
    """Conditional edge: decide the next node from the current state."""
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []

    if not calls:
        return "final"
    if state["rounds"] >= state["max_rounds"]:
        return "budget"
    return "tools"


def build_graph(llm: Any, registry: ToolRegistry):
    builder = StateGraph(AgentState)
    builder.add_node("model", make_model_node(llm, registry))
    builder.add_node("tools", make_tools_node(registry))
    builder.add_node("finalize", finalize_node)
    builder.add_node("budget_exceeded", budget_exceeded_node)

    builder.add_edge(START, "model")
    builder.add_conditional_edges(
        "model",
        route_after_model,
        {
            "tools": "tools",
            "final": "finalize",
            "budget": "budget_exceeded",
        },
    )
    builder.add_edge("tools", "model")
    builder.add_edge("finalize", END)
    builder.add_edge("budget_exceeded", END)
    return builder.compile()


def run_agent(
    question: str,
    llm: Any = None,
    registry: Optional[ToolRegistry] = None,
    max_rounds: int = 6,
) -> AgentState:
    root = Path(__file__).resolve().parent
    if registry is None:
        registry = build_registry(root / "sandbox")
    if llm is None:
        llm = ScriptedLLM(DEMO_TURNS)
    graph = build_graph(llm, registry)

    # The app-level budget should be the normal stop condition. Leave the
    # framework recursion limit comfortably above it, so a GraphRecursionError
    # means "your router is broken", not "your task was long".
    config = {"recursion_limit": max(50, max_rounds * 2 + 10)}
    return graph.invoke(initial_state(question, max_rounds), config=config)


def print_trace(messages: List[AnyMessage]) -> None:
    for message in messages:
        if isinstance(message, (SystemMessage, HumanMessage)):
            continue
        if isinstance(message, AIMessage):
            if message.tool_calls:
                if message.content:
                    print("[model] %s" % message.content)
                for call in message.tool_calls:
                    print(
                        "        -> %s(%s)"
                        % (
                            call["name"],
                            json.dumps(call["args"], ensure_ascii=False),
                        )
                    )
            else:
                print("[model] final: %s" % message.content)
        elif isinstance(message, ToolMessage):
            preview = str(message.content).replace("\n", " | ")[:320]
            print("[tool:%s] %s" % (message.name or "?", preview))
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lesson 0008 LangGraph agent")
    parser.add_argument(
        "question",
        nargs="?",
        default="order-service 的连接池上限是多少？根因是什么？",
        help="要问 Agent 的问题",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="使用脚本化 LLM（默认就是离线 demo，保留此参数便于对照第 3 课）",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=6,
        help="应用层最大模型轮数；超过后走 budget_exceeded 节点",
    )
    parser.add_argument(
        "--mermaid",
        action="store_true",
        help="打印图结构（Mermaid），不运行 Agent",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    registry = build_registry(root / "sandbox")
    llm = ScriptedLLM(DEMO_TURNS)
    graph = build_graph(llm, registry)

    if args.mermaid:
        print(graph.get_graph().draw_mermaid())
        return

    print("question: %s" % args.question)
    result = run_agent(args.question, llm=llm, registry=registry, max_rounds=args.max_rounds)
    print_trace(result["messages"])

    print("=== final answer ===")
    print(result["answer"])
    print()
    print(
        "rounds=%d  stop_reason=%s  tool_calls=%d"
        % (result["rounds"], result["stop_reason"], len(result["tool_events"]))
    )


if __name__ == "__main__":
    main()
