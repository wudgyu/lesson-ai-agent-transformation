"""Offline tests for lesson 0007. No API key required.

    python3 -m unittest -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from compaction import (
    build_summary,
    compact_state,
    should_compact,
    truncate_oldest,
)
from memory import MemoryStore, tokenize_simple
from session import SessionAgent, SessionConfig, ContextOverflow
from state import AgentState, Fact, Message, estimate_tokens, load_checkpoint, save_checkpoint


class StateTests(unittest.TestCase):
    def test_message_ids_stay_monotonic_after_compaction(self):
        state = AgentState("s1")
        for i in range(10):
            state.add_message("user", "message %d" % i)
        compact_state(state, keep_recent=2)
        state.add_message("user", "after compaction")
        ids = [message.message_id for message in state.messages]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("m0011", ids)

    def test_set_fact_supersedes_conflict(self):
        state = AgentState("s1")
        state.set_fact("db_pool_max", "40", "memory:M1", 0.5)
        state.set_fact("db_pool_max", "50", "tool:health", 0.9)
        self.assertEqual(state.facts["db_pool_max"].value, "50")
        self.assertEqual(len(state.fact_history), 1)
        self.assertEqual(state.fact_history[0].status, "superseded")
        self.assertEqual(state.fact_history[0].value, "40")

    def test_same_fact_updates_without_history(self):
        state = AgentState("s1")
        state.set_fact("db_pool_max", "50", "a", 0.5)
        state.set_fact("db_pool_max", "50", "b", 0.9)
        self.assertEqual(len(state.fact_history), 0)
        self.assertEqual(state.facts["db_pool_max"].source, "b")

    def test_side_effect_is_idempotent(self):
        state = AgentState("s1")
        self.assertTrue(state.record_side_effect("restart:APR-1"))
        self.assertFalse(state.record_side_effect("restart:APR-1"))
        self.assertTrue(state.has_side_effect("restart:APR-1"))

    def test_checkpoint_roundtrip(self):
        state = AgentState("s1")
        state.add_message("user", "hello")
        state.set_fact("x", "1", "test")
        state.add_open_question("why?")
        state.record_side_effect("restart:1")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            save_checkpoint(state, path)
            loaded = load_checkpoint(path)
        self.assertEqual(loaded.session_id, "s1")
        self.assertEqual(loaded.messages[0].content, "hello")
        self.assertEqual(loaded.facts["x"].value, "1")
        self.assertEqual(loaded.open_questions, ["why?"])
        self.assertTrue(loaded.has_side_effect("restart:1"))

    def test_token_estimate(self):
        self.assertGreater(estimate_tokens("数据库连接池耗尽"), 0)
        self.assertEqual(estimate_tokens(""), 0)


class CompactionTests(unittest.TestCase):
    def make_state(self) -> AgentState:
        state = AgentState("s1")
        state.add_message("system", "system prompt")
        state.add_message("user", "order-service 5xx")
        state.set_fact("db_pool_max", "50", "tool:health", 0.9)
        state.add_decision("不要只重启")
        state.add_open_question("是否回滚？")
        for i in range(12):
            state.add_message("assistant", "long turn %d " % i + "x" * 80)
        state.tool_results.append(
            {"tool": "get_service_health", "summary": "degraded, 48/50"}
        )
        return state

    def test_should_compact(self):
        state = self.make_state()
        self.assertTrue(should_compact(state, max_tokens=100, threshold=0.5))
        self.assertFalse(should_compact(state, max_tokens=100000, threshold=0.5))

    def test_compact_preserves_structured_state(self):
        state = self.make_state()
        result = compact_state(state, keep_recent=3)
        self.assertIn("db_pool_max", result.preserved_facts)
        self.assertEqual(state.facts["db_pool_max"].value, "50")
        self.assertIn("不要只重启", state.decisions)
        self.assertIn("是否回滚？", state.open_questions)
        self.assertGreater(result.dropped_messages, 0)
        self.assertLess(result.tokens_after, result.tokens_before)

    def test_summary_contains_key_sections(self):
        state = self.make_state()
        summary = build_summary(state)
        self.assertIn("Confirmed facts", summary)
        self.assertIn("db_pool_max = 50", summary)
        self.assertIn("不要只重启", summary)
        self.assertIn("是否回滚？", summary)

    def test_compaction_keeps_first_user_message(self):
        state = self.make_state()
        compact_state(state, keep_recent=2)
        user_messages = [m.content for m in state.messages if m.role == "user"]
        self.assertIn("order-service 5xx", user_messages)

    def test_truncate_oldest(self):
        state = self.make_state()
        dropped = truncate_oldest(state, max_tokens=120)
        self.assertGreater(dropped, 0)
        self.assertLessEqual(state.estimate_tokens(), 120)


class MemoryTests(unittest.TestCase):
    def test_tokenize_simple(self):
        tokens = tokenize_simple("数据库连接池 runbook")
        self.assertIn("连接", tokens)
        self.assertIn("runbook", tokens)

    def test_remember_deduplicates(self):
        store = MemoryStore()
        first = store.remember("semantic", "先看 runbook", "user")
        second = store.remember("semantic", "先看  runbook", "user")
        self.assertEqual(first.memory_id, second.memory_id)
        self.assertEqual(len(store.all()), 1)

    def test_search_matches_and_ranks(self):
        store = MemoryStore()
        store.remember("semantic", "order-service 先看 runbook", "user", tags=["order-service"])
        store.remember("semantic", "payment-service 的证书过期", "incident")
        results = store.search("order-service 怎么排查", top_k=2)
        self.assertTrue(results)
        self.assertIn("runbook", results[0][0].text)

    def test_ttl_expiry(self):
        store = MemoryStore()
        item = store.remember("semantic", "old fact", "test", ttl_days=1)
        old = datetime.now(timezone.utc) + timedelta(days=2)
        self.assertEqual(store.expire(old), 1)
        self.assertEqual(store.all(), [])

    def test_forget_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            store = MemoryStore(path)
            item = store.remember("episodic", "incident 2026-10", "incident")
            reloaded = MemoryStore(path)
            self.assertEqual([x.memory_id for x in reloaded.all()], [item.memory_id])
            self.assertTrue(reloaded.forget(item.memory_id))
            self.assertEqual(MemoryStore(path).all(), [])

    def test_format_for_context(self):
        store = MemoryStore()
        store.remember("semantic", "先看 runbook", "user")
        context = store.format_for_context(store.search("runbook"))
        self.assertIn("[M001]", context)
        self.assertIn("LONG-TERM MEMORY", context)


class SessionTests(unittest.TestCase):
    def make_agent(self, tmp: str, **overrides) -> SessionAgent:
        memory = MemoryStore(Path(tmp) / "memory.json")
        config = SessionConfig(
            checkpoint_path=Path(tmp) / "checkpoint.json", **overrides
        )
        return SessionAgent.new("s1", memory, config)

    def test_investigation_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            first = agent.run_turn("order-service 大量 5xx，帮我查一下。")
            second = agent.run_turn("日志里有什么错误？")
            third = agent.run_turn("根因是什么？")
            self.assertEqual(first.tool, "get_service_health")
            self.assertEqual(second.tool, "query_logs")
            self.assertEqual(third.tool, "search_runbooks")
            final = agent.run_turn("给我最终结论。")
            self.assertEqual(final.action, "answer")
            self.assertIn("N+1", final.answer)
            self.assertIn("48/50", final.answer)

    def test_restart_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            first = agent.run_turn("帮我重启 order-service")
            second = agent.run_turn("帮我重启 order-service")
            self.assertEqual(first.tool, "restart_service")
            self.assertEqual(second.tool, "restart_service")
            self.assertEqual(agent.state.side_effects, ["restart:order-service:APR-1"])
            self.assertIn("noop", agent.state.messages[-1].content)

    def test_longterm_preference_across_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory.json")
            config = SessionConfig(
                checkpoint_path=Path(tmp) / "c1.json", max_context_tokens=5000
            )
            session1 = SessionAgent.new("s1", memory, config)
            session1.run_turn("记住：以后排查先看 runbook，再看日志。")

            session2 = SessionAgent.new(
                "s2", memory, SessionConfig(max_context_tokens=5000)
            )
            turn = session2.run_turn("order-service 大量 5xx，按我的偏好来。")
            self.assertEqual(turn.tool, "search_runbooks")
            self.assertGreaterEqual(turn.memory_hits, 1)

    def test_stale_memory_is_superseded_by_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory.json")
            memory.remember(
                "semantic",
                "order-service 的连接池上限是 db_pool_max=40",
                "old incident",
                tags=["order-service"],
            )
            agent = SessionAgent.new(
                "s1", memory, SessionConfig(max_context_tokens=5000)
            )
            agent.run_turn("order-service 5xx，帮我查一下。")
            agent.run_turn("日志里有什么错误？")
            agent.run_turn("根因是什么？")
            final = agent.run_turn("给我最终结论。")
            self.assertEqual(agent.state.facts["db_pool_max"].value, "50")
            self.assertTrue(
                any(
                    fact.value == "40" and fact.status == "superseded"
                    for fact in agent.state.fact_history
                )
            )
            self.assertIn("48/50", final.answer)

    def test_checkpoint_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            agent.run_turn("order-service 大量 5xx，帮我查一下。")
            agent.run_turn("日志里有什么错误？")
            checkpoint = Path(tmp) / "checkpoint.json"
            resumed = SessionAgent.resume(checkpoint, MemoryStore(Path(tmp) / "memory.json"))
            self.assertEqual(resumed.state.facts["error_code"].value, "HikariPool timeout")
            turn = resumed.run_turn("根因是什么？")
            self.assertEqual(turn.tool, "search_runbooks")

    def test_context_overflow_without_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(
                tmp,
                enable_compaction=False,
                max_context_tokens=10,
            )
            result = agent.run_turn("order-service 大量 5xx，帮我查一下。")
            self.assertEqual(result.action, "overflow")


if __name__ == "__main__":
    unittest.main()
