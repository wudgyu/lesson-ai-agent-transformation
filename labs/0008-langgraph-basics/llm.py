"""Deterministic stand-in for a tool-calling chat model.

The graph in this lab only needs the message protocol, not a real API:
every "model call" returns an AIMessage with zero or more tool_calls.
Swapping in a real OpenAI-compatible client is deliberately left as an
exercise; the graph does not change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from langchain_core.messages import AIMessage


@dataclass(frozen=True)
class Turn:
    """One scripted model response."""

    content: str = ""
    tool_calls: Sequence[Dict[str, Any]] = field(default_factory=tuple)


class ScriptedLLM:
    """Returns pre-written turns, one per ``invoke`` call.

    It is intentionally dumb: no network, no randomness, no prompt parsing.
    That keeps the lab focused on the graph, not on model quality.
    """

    def __init__(self, turns: Sequence[Turn]) -> None:
        if not turns:
            raise ValueError("script must contain at least one turn")
        self._turns: List[Turn] = list(turns)
        self.calls = 0
        self.last_tools: List[Dict[str, Any]] = []

    def invoke(self, messages: Any, tools: Any = None) -> AIMessage:
        """Mimic the small slice of a chat model that the agent needs."""
        if self.calls >= len(self._turns):
            raise RuntimeError(
                "script exhausted after %d turns; add a final turn or raise "
                "the number of scripted responses" % self.calls
            )
        turn = self._turns[self.calls]
        self.calls += 1
        self.last_tools = list(tools or [])

        tool_calls = [
            {
                "name": call["name"],
                "args": dict(call.get("args") or {}),
                "id": call["id"],
            }
            for call in turn.tool_calls
        ]
        return AIMessage(content=turn.content, tool_calls=tool_calls)


DEMO_TURNS: Sequence[Turn] = (
    Turn(
        content="先查 order-service 的健康状态，确认连接池与错误率。",
        tool_calls=(
            {
                "name": "get_service_health",
                "args": {"service": "order-service"},
                "id": "call_health",
            },
        ),
    ),
    Turn(
        content="健康数据里 db_pool_max=50、使用 47，错误率 0.8%。再查 runbook 找根因。",
        tool_calls=(
            {
                "name": "search_runbooks",
                "args": {"query": "connection pool"},
                "id": "call_runbook",
            },
        ),
    ),
    Turn(
        content=(
            "结论：order-service 连接池上限是 50，当前使用 47，已接近上限；"
            "runbook 指向 OrderQueryService.findRecentOrders 的 N+1 查询。"
            "建议先回滚或修 N+1，不要只重启。"
        )
    ),
)


class RepeatingLLM:
    """Always asks for the same tool call. Used to demonstrate recursion limits."""

    def __init__(self, tool_name: str = "get_service_health", service: str = "order-service") -> None:
        self.tool_name = tool_name
        self.service = service
        self.calls = 0

    def invoke(self, messages: Any, tools: Any = None) -> AIMessage:
        self.calls += 1
        return AIMessage(
            content="再查一次。",
            tool_calls=[
                {
                    "name": self.tool_name,
                    "args": {"service": self.service},
                    "id": "call_repeat_%d" % self.calls,
                }
            ],
        )
