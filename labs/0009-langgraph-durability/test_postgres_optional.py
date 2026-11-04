"""Optional Postgres tests for lesson 0009.

They are skipped unless TEST_POSTGRES_URL is set. Run them with:

    TEST_POSTGRES_URL=postgresql://repomedic:repomedic@localhost:5432/repomedic \
        python3 -m unittest test_postgres_optional -v

The tests create their own thread IDs, so they can share a database with the
CLI demo. They use the same effects ledger semantics as the SQLite lab.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from effects import EffectStore
from graph_agent import (
    SimulatedCrash,
    build_graph,
    config_for,
    initial_state,
)

DATABASE_URL = os.environ.get("TEST_POSTGRES_URL") or ""
if not DATABASE_URL.startswith(("postgresql://", "postgres://")):
    DATABASE_URL = ""

APPROVE = {
    "approved": True,
    "approver": "alice",
    "approval_id": "APR-PG-7",
    "note": "同意重启",
}


@unittest.skipUnless(
    DATABASE_URL,
    "set TEST_POSTGRES_URL to run the optional Postgres tests",
)
class PostgresCheckpointerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.effects_path = Path(self.tmp.name) / "effects.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.saver = self._new_saver()
        self.saver.setup()
        self.thread = "pg-test-" + uuid.uuid4().hex[:10]
        self.config = config_for(self.thread)
        self.graph = self._new_graph()

    def _new_saver(self) -> PostgresSaver:
        cm = PostgresSaver.from_conn_string(DATABASE_URL)
        return self.stack.enter_context(cm)

    def _new_graph(self, crash_after_effect: bool = False) -> Any:
        saver = self._new_saver()
        saver.setup()
        return build_graph(
            EffectStore(self.effects_path),
            saver,
            crash_after_effect=crash_after_effect,
        )

    def _start(self, graph: Any = None) -> Dict[str, Any]:
        graph = graph or self.graph
        return graph.invoke(initial_state(), self.config, durability="sync")

    def _resume(self, decision: Dict[str, Any], graph: Any = None) -> Dict[str, Any]:
        graph = graph or self.graph
        return graph.invoke(Command(resume=decision), self.config, durability="sync")

    def test_setup_is_idempotent(self) -> None:
        self.saver.setup()
        self.saver.setup()

    def test_interrupt_and_resume_across_connections(self) -> None:
        result = self._start()
        self.assertTrue(result.get("__interrupt__"))
        self.assertEqual(self.graph.get_state(self.config).next, ("approval",))

        # A separate connection models a second API instance.
        other = self._new_graph()
        resumed = other.invoke(Command(resume=APPROVE), self.config, durability="sync")

        self.assertEqual(resumed["stop_reason"], "executed")
        self.assertEqual(resumed["diagnose_runs"], 1)
        self.assertEqual(EffectStore(self.effects_path).count(), 1)

    def test_threads_do_not_share_checkpoints(self) -> None:
        self._start()

        other_thread = "pg-test-" + uuid.uuid4().hex[:10]
        other_config = config_for(other_thread)
        other_result = self.graph.invoke(
            initial_state("INC-PG-99"), other_config, durability="sync"
        )
        self.assertTrue(other_result.get("__interrupt__"))

        self._resume(APPROVE)
        self.assertEqual(EffectStore(self.effects_path).count(), 1)
        self.assertEqual(self.graph.get_state(other_config).next, ("approval",))

    def test_crash_resume_is_idempotent_across_connections(self) -> None:
        self._start()
        crashing = self._new_graph(crash_after_effect=True)
        with self.assertRaises(SimulatedCrash):
            self._resume(APPROVE, graph=crashing)

        self.assertEqual(self.graph.get_state(self.config).next, ("execute",))
        self.assertEqual(EffectStore(self.effects_path).count(), 1)

        restarted = self._new_graph()
        result = restarted.invoke(None, self.config, durability="sync")

        self.assertEqual(result["stop_reason"], "already_executed")
        self.assertEqual(result["diagnose_runs"], 1)
        self.assertEqual(result["effects"][0]["status"], "noop")
        self.assertEqual(EffectStore(self.effects_path).count(), 1)

    def test_delete_thread_removes_history(self) -> None:
        self._start()
        self.assertGreater(len(list(self.saver.list(self.config))), 0)

        self.saver.delete_thread(self.thread)

        self.assertEqual(list(self.saver.list(self.config)), [])
        self.assertEqual(self.graph.get_state(self.config).next, ())


if __name__ == "__main__":
    unittest.main()
