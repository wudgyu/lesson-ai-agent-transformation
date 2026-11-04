"""Offline tests for lesson 0009. No API key required.

Run:
    python3 -m unittest test_durability -v
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from effects import EffectStore
from graph_agent import (
    SimulatedCrash,
    build_graph,
    config_for,
    initial_state,
)

THREAD = "incident-42"
APPROVE = {
    "approved": True,
    "approver": "alice",
    "approval_id": "APR-7",
    "note": "同意先重启缓解",
}
REJECT = {
    "approved": False,
    "approver": "bob",
    "approval_id": "",
    "note": "先修 N+1，不要重启",
}


class DurabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "checkpoints.db"
        self.effects_path = Path(self.tmp.name) / "effects.json"
        self.config = config_for(THREAD)
        self.graph = self._open_graph()

    def _open_graph(self, crash_after_effect: bool = False):
        conn = sqlite3.connect(str(self.db), check_same_thread=False)
        self.addCleanup(conn.close)
        return build_graph(
            EffectStore(self.effects_path),
            SqliteSaver(conn),
            crash_after_effect=crash_after_effect,
        )

    def _start(self, graph: Any = None) -> Dict[str, Any]:
        graph = graph or self.graph
        return graph.invoke(initial_state(), self.config, durability="sync")

    def _resume(self, decision: Dict[str, Any], graph: Any = None) -> Dict[str, Any]:
        graph = graph or self.graph
        return graph.invoke(Command(resume=decision), self.config, durability="sync")

    # ---------------------------------------------------------------- interrupt
    def test_first_run_pauses_at_approval(self) -> None:
        result = self._start()

        self.assertEqual(result["diagnose_runs"], 1)
        self.assertEqual(len(result["__interrupt__"]), 1)
        payload = result["__interrupt__"][0].value
        self.assertEqual(payload["type"], "approval_required")
        self.assertEqual(payload["proposal"]["action"], "restart_service")
        json.dumps(payload)  # the payload must cross a process boundary

        snapshot = self.graph.get_state(self.config)
        self.assertEqual(snapshot.next, ("approval",))
        self.assertEqual(snapshot.tasks[0].name, "approval")
        self.assertEqual(snapshot.tasks[0].interrupts[0].value, payload)

    def test_approve_executes_the_side_effect_once(self) -> None:
        self._start()
        result = self._resume(APPROVE)

        self.assertEqual(result["stop_reason"], "executed")
        self.assertEqual(result["approval"]["approval_id"], "APR-7")
        self.assertEqual(len(result["effects"]), 1)
        self.assertEqual(result["effects"][0]["status"], "restarted")
        self.assertEqual(EffectStore(self.effects_path).count(), 1)

    def test_reject_skips_the_side_effect(self) -> None:
        self._start()
        result = self._resume(REJECT)

        self.assertEqual(result["stop_reason"], "rejected")
        self.assertEqual(result["effects"], [])
        self.assertEqual(result["execute_runs"], 0)
        self.assertEqual(EffectStore(self.effects_path).count(), 0)

    def test_approval_without_approver_or_id_is_invalid(self) -> None:
        self._start()
        result = self._resume(
            {"approved": True, "approver": "alice", "approval_id": ""}
        )

        self.assertEqual(result["stop_reason"], "invalid_approval")
        self.assertEqual(result["execute_runs"], 0)
        self.assertEqual(EffectStore(self.effects_path).count(), 0)

    def test_a_new_thread_does_not_inherit_the_parked_one(self) -> None:
        self._start()
        other = config_for("incident-99")
        result = self.graph.invoke(initial_state("INC-99"), other, durability="sync")

        self.assertEqual(result["diagnose_runs"], 1)
        self.assertTrue(result.get("__interrupt__"))
        self.assertEqual(self.graph.get_state(self.config).next, ("approval",))

    # ------------------------------------------------------------ crash + resume
    def test_crash_after_side_effect_resumes_without_duplicating_it(self) -> None:
        self._start()
        crashing = self._open_graph(crash_after_effect=True)
        with self.assertRaises(SimulatedCrash):
            self._resume(APPROVE, graph=crashing)

        snapshot = crashing.get_state(self.config)
        self.assertEqual(snapshot.next, ("execute",))
        self.assertEqual(snapshot.values["diagnose_runs"], 1)
        self.assertEqual(EffectStore(self.effects_path).count(), 1)

        # A new graph + new SQLite connection models a new process.
        restarted = self._open_graph(crash_after_effect=False)
        result = restarted.invoke(None, self.config, durability="sync")

        self.assertEqual(result["stop_reason"], "already_executed")
        self.assertEqual(result["diagnose_runs"], 1)  # completed node not replayed
        self.assertEqual(result["execute_runs"], 1)  # interrupted node replayed
        self.assertEqual(len(result["effects"]), 1)
        self.assertEqual(result["effects"][0]["status"], "noop")
        self.assertEqual(EffectStore(self.effects_path).count(), 1)

    def test_duplicate_effect_appears_when_idempotency_is_disabled(self) -> None:
        store = EffectStore(self.effects_path)
        store.restart("order-service", "APR-1", idempotent=False)
        store.restart("order-service", "APR-1", idempotent=False)
        self.assertEqual(store.count(), 2)

        self.effects_path.unlink()
        store = EffectStore(self.effects_path)
        self.assertEqual(
            store.restart("order-service", "APR-1")["status"], "restarted"
        )
        self.assertEqual(store.restart("order-service", "APR-1")["status"], "noop")
        self.assertEqual(store.count(), 1)

    # ------------------------------------------------------------------- stream
    def test_stream_emits_node_updates_and_custom_progress(self) -> None:
        parts = list(
            self.graph.stream(
                initial_state(),
                self.config,
                stream_mode=["updates", "custom"],
                version="v2",
                durability="sync",
            )
        )
        custom_nodes = [p["data"]["node"] for p in parts if p["type"] == "custom"]
        self.assertEqual(custom_nodes, ["diagnose", "approval"])

        update_nodes = [
            node
            for p in parts
            if p["type"] == "updates"
            for node in p["data"]
        ]
        self.assertIn("diagnose", update_nodes)
        self.assertIn("__interrupt__", update_nodes)

        resumed = list(
            self.graph.stream(
                Command(resume=APPROVE),
                self.config,
                stream_mode=["updates", "custom"],
                version="v2",
                durability="sync",
            )
        )
        resumed_updates = [
            node
            for p in resumed
            if p["type"] == "updates"
            for node in p["data"]
        ]
        self.assertEqual(resumed_updates, ["approval", "execute", "report"])
        resumed_custom = [p["data"]["node"] for p in resumed if p["type"] == "custom"]
        self.assertEqual(resumed_custom, ["approval", "execute"])

    # -------------------------------------------------------------- state history
    def test_history_exposes_the_interrupted_checkpoint(self) -> None:
        self._start()
        self._resume(APPROVE)

        history = list(self.graph.get_state_history(self.config))
        self.assertGreaterEqual(len(history), 5)
        self.assertEqual(history[0].next, ())  # newest first

        interrupted = next(
            state
            for state in history
            if state.next == ("approval",)
            and state.tasks
            and any(task.interrupts for task in state.tasks)
        )
        self.assertEqual(
            interrupted.tasks[0].interrupts[0].value["type"],
            "approval_required",
        )


if __name__ == "__main__":
    unittest.main()
