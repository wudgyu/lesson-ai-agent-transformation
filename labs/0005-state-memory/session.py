"""A small, deterministic RepoMedic session agent for lesson 0005.

The model is scripted on purpose: this lab is about state, compaction,
long-term memory and checkpoint/resume, not about model quality. Swap the
scripted policy for an LLM later and the state machinery still applies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from compaction import CompactionResult, compact_if_needed, truncate_oldest
from memory import MemoryItem, MemoryStore
from state import AgentState, load_checkpoint, save_checkpoint


class ContextOverflow(RuntimeError):
    pass


@dataclass
class SessionConfig:
    max_context_tokens: int = 1200
    compact_threshold: float = 0.70
    keep_recent: int = 4
    enable_compaction: bool = True
    enable_longterm: bool = True
    checkpoint_path: Optional[Path] = None


@dataclass
class TurnResult:
    turn: int
    action: str
    answer: Optional[str] = None
    tool: Optional[str] = None
    tokens: int = 0
    compacted: bool = False
    compaction: Optional[CompactionResult] = None
    memory_hits: int = 0
    dropped_messages: int = 0
    error: Optional[str] = None


class RepoMedicTools:
    def __init__(self, state: AgentState) -> None:
        self.state = state

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "get_service_health":
            return self.health(arguments.get("service", "order-service"))
        if name == "query_logs":
            return self.logs(
                arguments.get("service", "order-service"),
                arguments.get("level", "ERROR"),
                int(arguments.get("since_minutes", 30)),
            )
        if name == "search_runbooks":
            return self.runbook(arguments.get("query", "连接池"))
        if name == "restart_service":
            return self.restart(
                arguments.get("service", "order-service"),
                arguments.get("approval_id", ""),
            )
        return {"error": "unknown tool: %s" % name}

    def health(self, service: str) -> Dict[str, Any]:
        result = {
            "service": service,
            "status": "degraded",
            "error_rate_5m": 0.18,
            "p99_latency_ms": 2400,
            "db_pool": {"used": 48, "max": 50},
            "recent_deploy": {
                "version": "v2.14.1",
                "commit": "a1b2c3d",
            },
        }
        self.state.set_fact("db_pool_used", "48", "tool:get_service_health", 0.9)
        self.state.set_fact("db_pool_max", "50", "tool:get_service_health", 0.9)
        self.state.set_fact("p99_latency_ms", "2400", "tool:get_service_health", 0.9)
        self.state.set_fact(
            "deploy_version", "v2.14.1", "tool:get_service_health", 0.9
        )
        return result

    def logs(self, service: str, level: str, since_minutes: int) -> Dict[str, Any]:
        result = {
            "service": service,
            "level": level,
            "since_minutes": since_minutes,
            "matched": 13,
            "shown": 3,
            "lines": [
                "20:12:03 ERROR HikariPool-1 - Connection is not available, request timed out after 3000ms",
                "20:12:04 ERROR GET /api/orders 500 30012ms",
                "20:12:07 WARN slow query: OrderQueryService.findRecentOrders 28700ms",
            ],
        }
        self.state.set_fact(
            "error_code", "HikariPool timeout", "tool:query_logs", 0.9
        )
        self.state.resolve_open_question("日志里有什么错误？")
        return result

    def runbook(self, query: str) -> Dict[str, Any]:
        result = {
            "query": query,
            "source": "incidents/2026-10-24-order-pool.md",
            "summary": (
                "事故复盘：v2.14.1 在 OrderQueryService.findRecentOrders 中把批量查询"
                "改成循环查询，形成 N+1；连接池 48/50 被耗尽。处置：回滚或恢复批量查询，"
                "不要只重启。"
            ),
        }
        self.state.set_fact(
            "root_cause",
            "N+1 in OrderQueryService.findRecentOrders",
            "tool:search_runbooks",
            0.9,
        )
        self.state.set_fact(
            "runbook_source",
            "incidents/2026-10-24-order-pool.md",
            "tool:search_runbooks",
            0.9,
        )
        self.state.add_decision("不要只重启；先修 N+1 或回滚")
        self.state.resolve_open_question("根因是什么？")
        self.state.add_open_question("是否已经回滚或恢复批量查询？")
        return result

    def restart(self, service: str, approval_id: str) -> Dict[str, Any]:
        key = "restart:%s:%s" % (service, approval_id)
        if self.state.has_side_effect(key):
            return {
                "status": "noop",
                "message": "approval already used; restart not repeated",
                "idempotency_key": key,
            }
        self.state.record_side_effect(key)
        return {
            "status": "restarted",
            "service": service,
            "approval_id": approval_id,
            "idempotency_key": key,
        }


class SessionAgent:
    def __init__(
        self,
        state: AgentState,
        tools: RepoMedicTools,
        memory: MemoryStore,
        config: SessionConfig,
    ) -> None:
        self.state = state
        self.tools = tools
        self.memory = memory
        self.config = config
        self._last_memories: List[Tuple[MemoryItem, float]] = []
        self.last_compaction: Optional[CompactionResult] = None
        self.dropped_messages = 0

    @classmethod
    def new(
        cls,
        session_id: str,
        memory: MemoryStore,
        config: Optional[SessionConfig] = None,
    ) -> "SessionAgent":
        state = AgentState(session_id=session_id)
        state.add_message(
            "system",
            "你是 RepoMedic。只依据工具结果回答；写操作必须审批；证据不足时明确说明。",
        )
        state.add_open_question("根因是什么？")
        state.add_open_question("日志里有什么错误？")
        return cls(state, RepoMedicTools(state), memory, config or SessionConfig())

    @classmethod
    def resume(
        cls,
        checkpoint_path: Path,
        memory: MemoryStore,
        config: Optional[SessionConfig] = None,
    ) -> "SessionAgent":
        state = load_checkpoint(checkpoint_path)
        return cls(state, RepoMedicTools(state), memory, config or SessionConfig())

    def run_turn(self, user_input: str) -> TurnResult:
        self.state.turn += 1
        self.state.add_message("user", user_input)
        memory_hits = self._inject_memories(user_input)

        compacted = False
        compaction: Optional[CompactionResult] = None
        try:
            compaction = self._maybe_compact()
        except ContextOverflow as exc:
            return TurnResult(
                turn=self.state.turn,
                action="overflow",
                tokens=self.state.estimate_tokens(),
                memory_hits=memory_hits,
                error=str(exc),
            )
        if compaction is not None:
            compacted = True
            self.last_compaction = compaction

        action, payload = self._decide(user_input)
        if action == "tool":
            tool_name, arguments = payload
            self.state.add_message(
                "assistant",
                "calling %s(%s)" % (tool_name, arguments),
                kind="tool_call",
            )
            result = self.tools.execute(tool_name, arguments)
            self.state.tool_results.append(
                {
                    "tool": tool_name,
                    "arguments": arguments,
                    "summary": self._summarize_result(tool_name, result),
                }
            )
            self.state.add_message(
                "tool",
                self._summarize_result(tool_name, result),
                kind="tool_result",
            )
            self.state.phase = "investigating"
            self._checkpoint()
            return TurnResult(
                turn=self.state.turn,
                action="tool",
                tool=tool_name,
                tokens=self.state.estimate_tokens(),
                compacted=compacted,
                compaction=compaction,
                memory_hits=memory_hits,
                dropped_messages=self.dropped_messages,
            )

        answer = str(payload)
        self.state.add_message("assistant", answer)
        self.state.phase = "answered"
        self._checkpoint()
        return TurnResult(
            turn=self.state.turn,
            action="answer",
            answer=answer,
            tokens=self.state.estimate_tokens(),
            compacted=compacted,
            compaction=compaction,
            memory_hits=memory_hits,
            dropped_messages=self.dropped_messages,
        )

    def _inject_memories(self, user_input: str) -> int:
        if not self.config.enable_longterm:
            return 0
        results = self.memory.search(user_input, top_k=2)
        self._last_memories = results
        if not results:
            return 0
        context = self.memory.format_for_context(results)
        if not any(
            message.kind == "memory" and message.content == context
            for message in self.state.messages
        ):
            self.state.add_message("system", context, kind="memory")
        for item, _ in results:
            match = re.search(r"db_pool_max\s*=\s*(\d+)", item.text)
            if match and "db_pool_max" not in self.state.facts:
                self.state.set_fact(
                    "db_pool_max",
                    match.group(1),
                    "memory:%s" % item.memory_id,
                    0.5,
                )
        return len(results)

    def _maybe_compact(self) -> Optional[CompactionResult]:
        if self.config.enable_compaction:
            return compact_if_needed(
                self.state,
                self.config.max_context_tokens,
                self.config.compact_threshold,
                self.config.keep_recent,
            )
        if self.state.estimate_tokens() > self.config.max_context_tokens:
            self.dropped_messages += truncate_oldest(
                self.state, self.config.max_context_tokens
            )
        if self.state.estimate_tokens() > self.config.max_context_tokens:
            raise ContextOverflow(
                "context budget exceeded: %d > %d"
                % (self.state.estimate_tokens(), self.config.max_context_tokens)
            )
        return None

    def _decide(self, user_input: str) -> Tuple[str, Any]:
        text = user_input.lower()

        if "记住" in user_input or "以后" in user_input:
            if self.config.enable_longterm:
                self.memory.remember(
                    "semantic",
                    user_input,
                    source="user",
                    tags=["preference", "runbook", "order-service", "排查"],
                    confidence=0.9,
                )
                return "answer", "已记住这条偏好。下次会话会通过长期记忆检索到它。"
            return "answer", "长期记忆未启用，这条偏好只在本会话有效。"

        negated_restart = any(
            phrase in user_input for phrase in ("不要重启", "别重启", "不能重启")
        )
        if negated_restart:
            self.state.add_decision("不要重启；先查根因")
        if "重启" in user_input and not negated_restart:
            if any(
                command in user_input
                for command in ("帮我重启", "执行重启", "重启服务", "现在重启")
            ):
                return "tool", (
                    "restart_service",
                    {"service": "order-service", "approval_id": "APR-1"},
                )
            root = self.state.get_fact("root_cause")
            return "answer", (
                "不要只重启。%s；重启只能暂时缓解，慢查询还在就会再次耗尽。"
                % ("当前根因是 %s" % root.value if root else "先确认根因")
            )

        if "上限" in user_input or "max" in text:
            fact = self.state.get_fact("db_pool_max")
            if fact:
                return "answer", "连接池上限是 %s（来源：%s）。" % (
                    fact.value,
                    fact.source,
                )

        if "部署" in user_input:
            fact = self.state.get_fact("deploy_version")
            if fact:
                return "answer", "最近一次部署是 %s（来源：%s）。" % (
                    fact.value,
                    fact.source,
                )

        if "没确认" in user_input or "未解决" in user_input:
            questions = "；".join(self.state.open_questions) or "没有未解决问题"
            return "answer", "未解决问题：%s。" % questions

        if self._prefers_runbook() and "root_cause" not in self.state.facts:
            return "tool", ("search_runbooks", {"query": user_input})

        if "db_pool_used" not in self.state.facts:
            return "tool", ("get_service_health", {"service": "order-service"})

        if "error_code" not in self.state.facts:
            return "tool", (
                "query_logs",
                {"service": "order-service", "level": "ERROR", "since_minutes": 30},
            )

        if "root_cause" not in self.state.facts:
            return "tool", (
                "search_runbooks",
                {"query": "连接池 耗尽 根因 N+1"},
            )

        return "answer", self._final_answer()

    def _prefers_runbook(self) -> bool:
        return any(
            "runbook" in item.text.lower() or "先看" in item.text
            for item, _ in self._last_memories
        )

    def _final_answer(self) -> str:
        root = self.state.get_fact("root_cause")
        source = self.state.get_fact("runbook_source")
        used = self.state.get_fact("db_pool_used")
        maximum = self.state.get_fact("db_pool_max")
        deploy = self.state.get_fact("deploy_version")
        questions = "；".join(self.state.open_questions) or "无"
        if questions.endswith(("？", "。")):
            questions = questions[:-1]
        return (
            "根因是 %s（来源：%s）。连接池 %s/%s，最近部署 %s。"
            "未解决问题：%s。"
            % (
                root.value if root else "未知",
                source.value if source else "未知",
                used.value if used else "?",
                maximum.value if maximum else "?",
                deploy.value if deploy else "?",
                questions,
            )
        )

    @staticmethod
    def _summarize_result(tool_name: str, result: Dict[str, Any]) -> str:
        if tool_name == "get_service_health":
            return (
                "%s %s, db_pool %s/%s, p99 %sms, deploy %s"
                % (
                    result.get("service"),
                    result.get("status"),
                    result.get("db_pool", {}).get("used"),
                    result.get("db_pool", {}).get("max"),
                    result.get("p99_latency_ms"),
                    result.get("recent_deploy", {}).get("version"),
                )
            )
        if tool_name == "query_logs":
            return "matched=%s shown=%s; %s" % (
                result.get("matched"),
                result.get("shown"),
                " | ".join(result.get("lines", [])[:2]),
            )
        if tool_name == "search_runbooks":
            return "source=%s; %s" % (
                result.get("source"),
                result.get("summary", ""),
            )
        if tool_name == "restart_service":
            return "status=%s key=%s" % (
                result.get("status"),
                result.get("idempotency_key"),
            )
        return str(result)

    def _checkpoint(self) -> None:
        if self.config.checkpoint_path:
            save_checkpoint(self.state, self.config.checkpoint_path)
