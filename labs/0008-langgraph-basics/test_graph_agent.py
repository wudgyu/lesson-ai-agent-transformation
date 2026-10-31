"""Offline tests for lesson 0008. No API key required.

Run:
    python3 -m unittest -v
"""
from __future__ import annotations

import operator
import unittest
from pathlib import Path
from typing import Annotated, List, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from graph_agent import build_graph, initial_state, run_agent
from llm import DEMO_TURNS, RepeatingLLM, ScriptedLLM, Turn
from tools import build_registry

ROOT = Path(__file__).resolve().parent
QUESTION = "order-service 的连接池上限是多少？根因是什么？"


def make_registry():
    return build_registry(ROOT / "sandbox")


def run_demo(max_rounds: int = 6):
    return run_agent(
        QUESTION,
        llm=ScriptedLLM(DEMO_TURNS),
        registry=make_registry(),
        max_rounds=max_rounds,
    )


class DemoGraphTest(unittest.TestCase):
    def test_demo_reaches_a_final_answer(self):
        result = run_demo()
        self.assertEqual(result["stop_reason"], "done")
        self.assertIn("50", result["answer"])
        self.assertIn("N+1", result["answer"])

    def test_demo_uses_two_tools_and_three_model_rounds(self):
        result = run_demo()
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(len(result["tool_events"]), 2)
        self.assertEqual(result["tool_events"][0]["tool"], "get_service_health")
        self.assertEqual(result["tool_events"][1]["tool"], "search_runbooks")

    def test_messages_follow_the_tool_protocol(self):
        result = run_demo()
        roles = [type(message).__name__ for message in result["messages"]]
        self.assertEqual(
            roles,
            [
                "SystemMessage",
                "HumanMessage",
                "AIMessage",
                "ToolMessage",
                "AIMessage",
                "ToolMessage",
                "AIMessage",
            ],
        )

        requested = []
        answered = []
        for message in result["messages"]:
            if isinstance(message, AIMessage):
                requested.extend(call["id"] for call in message.tool_calls)
            elif isinstance(message, ToolMessage):
                answered.append(message.tool_call_id)
        self.assertEqual(requested, answered)

    def test_final_answer_stops_without_calling_tools(self):
        result = run_agent(
            QUESTION,
            llm=ScriptedLLM([Turn(content="直接回答")]),
            registry=make_registry(),
        )
        self.assertEqual(result["stop_reason"], "done")
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["tool_events"], [])
        self.assertEqual(result["answer"], "直接回答")

    def test_tool_error_is_an_observation_not_a_crash(self):
        script = [
            Turn(
                content="先查一个不存在的服务。",
                tool_calls=(
                    {
                        "name": "get_service_health",
                        "args": {"service": "does-not-exist"},
                        "id": "call_bad",
                    },
                ),
            ),
            Turn(
                content="工具报错，改用 order-service。",
                tool_calls=(
                    {
                        "name": "get_service_health",
                        "args": {"service": "order-service"},
                        "id": "call_good",
                    },
                ),
            ),
            Turn(content="最终答案：连接池上限 50。"),
        ]
        result = run_agent(
            QUESTION,
            llm=ScriptedLLM(script),
            registry=make_registry(),
        )
        self.assertTrue(result["tool_events"][0]["result"].startswith("ERROR:"))
        self.assertEqual(result["stop_reason"], "done")
        self.assertIn("50", result["answer"])

    def test_unknown_tool_is_reported_to_the_model(self):
        script = [
            Turn(
                content="调用一个不存在的工具。",
                tool_calls=(
                    {"name": "drop_database", "args": {}, "id": "call_unknown"},
                ),
            ),
            Turn(content="工具不存在，我停止。"),
        ]
        result = run_agent(
            QUESTION,
            llm=ScriptedLLM(script),
            registry=make_registry(),
        )
        self.assertIn("unknown tool", result["tool_events"][0]["result"])
        self.assertEqual(result["stop_reason"], "done")


class BudgetTest(unittest.TestCase):
    def test_budget_stops_the_loop_and_skips_pending_tools(self):
        script = [
            Turn(
                content="第一轮",
                tool_calls=(
                    {
                        "name": "get_service_health",
                        "args": {"service": "order-service"},
                        "id": "call_1",
                    },
                ),
            ),
            Turn(
                content="第二轮",
                tool_calls=(
                    {
                        "name": "get_service_health",
                        "args": {"service": "order-service"},
                        "id": "call_2",
                    },
                ),
            ),
            Turn(content="不应到达"),
        ]
        result = run_agent(
            QUESTION,
            llm=ScriptedLLM(script),
            registry=make_registry(),
            max_rounds=2,
        )

        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["stop_reason"], "max_rounds")
        self.assertEqual(len(result["tool_events"]), 1)
        self.assertIn("最大轮数 2", result["answer"])

        skipped = [
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
            and str(message.content).startswith("SKIPPED:")
        ]
        self.assertEqual([message.tool_call_id for message in skipped], ["call_2"])

    def test_recursion_limit_catches_a_router_that_never_stops(self):
        graph = build_graph(RepeatingLLM(), make_registry())
        with self.assertRaises(GraphRecursionError):
            graph.invoke(
                initial_state(QUESTION, max_rounds=100),
                config={"recursion_limit": 6},
            )


class ReducerTest(unittest.TestCase):
    def test_default_channel_overwrites_the_old_list(self):
        class State(TypedDict):
            value: List[str]

        def replace(state: State):
            return {"value": ["b"]}

        builder = StateGraph(State)
        builder.add_node("replace", replace)
        builder.add_edge(START, "replace")
        builder.add_edge("replace", END)
        result = builder.compile().invoke({"value": ["a"]})
        self.assertEqual(result["value"], ["b"])

    def test_operator_add_merges_the_old_list(self):
        class State(TypedDict):
            value: Annotated[List[str], operator.add]

        def append(state: State):
            return {"value": ["b"]}

        builder = StateGraph(State)
        builder.add_node("append", append)
        builder.add_edge(START, "append")
        builder.add_edge("append", END)
        result = builder.compile().invoke({"value": ["a"]})
        self.assertEqual(result["value"], ["a", "b"])

    def test_add_messages_appends_new_ids(self):
        class State(TypedDict):
            messages: Annotated[list, add_messages]

        def add(state: State):
            return {"messages": [AIMessage(content="second", id="m2")]}

        builder = StateGraph(State)
        builder.add_node("add", add)
        builder.add_edge(START, "add")
        builder.add_edge("add", END)
        result = builder.compile().invoke(
            {"messages": [AIMessage(content="first", id="m1")]}
        )
        self.assertEqual([message.id for message in result["messages"]], ["m1", "m2"])

    def test_add_messages_updates_an_existing_id(self):
        class State(TypedDict):
            messages: Annotated[list, add_messages]

        def update(state: State):
            return {"messages": [AIMessage(content="new", id="m1")]}

        builder = StateGraph(State)
        builder.add_node("update", update)
        builder.add_edge(START, "update")
        builder.add_edge("update", END)
        result = builder.compile().invoke(
            {"messages": [AIMessage(content="old", id="m1")]}
        )
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0].content, "new")

    def test_graph_can_be_rendered_as_mermaid(self):
        graph = build_graph(ScriptedLLM(DEMO_TURNS), make_registry())
        mermaid = graph.get_graph().draw_mermaid()
        for name in ("model", "tools", "finalize", "budget_exceeded"):
            self.assertIn(name, mermaid)


if __name__ == "__main__":
    unittest.main()
