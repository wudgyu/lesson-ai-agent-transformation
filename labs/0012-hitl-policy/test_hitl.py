"""Offline tests for the Lesson 0012 HITL policy gateway.

Run:
    python3 -m unittest test_hitl -v

No API key, no network, no wall-clock sleeps: every test uses FakeClock and
a temporary JSON state file.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway import ApprovalGateway, SimulatedCrash
from models import (
    APPROVAL,
    APPROVED,
    AUTO,
    CONSUMED,
    DENY,
    EXPIRED,
    FOUR_EYES,
    NOTIFY,
    PENDING,
    REJECTED,
    Action,
    FakeClock,
    PolicyDecision,
)
from policy import Policy
from store import Store


class GatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.store = Store(self.state_path)
        self.clock = FakeClock()
        self.gateway = ApprovalGateway(store=self.store, clock=self.clock)

    # ---------- helpers ----------

    def submit(self, tool: str, args: dict, requested_by: str = "agent/repomedic", task: str = "INC-1"):
        return self.gateway.submit(Action(tool, args, requested_by, task))

    def pending(self, tool: str, args: dict, requested_by: str = "agent/repomedic", task: str = "INC-1"):
        submission = self.submit(tool, args, requested_by, task)
        self.assertEqual(submission.status, "pending", submission.message)
        return submission.request

    def approve(self, request_id: str, approver: str, note: str = ""):
        return self.gateway.approve(request_id, approver, note)

    # ---------- policy tiers ----------

    def test_read_only_action_auto_executes(self):
        submission = self.submit("query_logs", {"service": "order-service", "limit": 3})

        self.assertEqual(submission.status, "executed")
        self.assertEqual(submission.decision.tier, AUTO)
        self.assertIsNone(submission.request)
        self.assertEqual(submission.execution.status, "executed")
        # A read is not a side effect, so it does not go into the effect ledger.
        self.assertEqual(len(self.store.all_effects()), 0)

    def test_unknown_tool_fails_closed(self):
        submission = self.submit("exec_shell", {"cmd": "rm -rf /"})

        self.assertEqual(submission.status, "blocked")
        self.assertEqual(submission.decision.tier, DENY)
        self.assertEqual(submission.decision.rule_id, "unknown-tool")
        self.assertEqual(len(self.store.all_effects()), 0)

    def test_never_allowed_tool_is_denied(self):
        submission = self.submit("modify_firewall", {"rule": "allow 0.0.0.0/0:22"})

        self.assertEqual(submission.status, "blocked")
        self.assertEqual(submission.decision.rule_id, "never-allowed")

    def test_prod_restart_requires_one_approval(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})

        self.assertEqual(request.tier, APPROVAL)
        self.assertEqual(request.required_approvals, 1)
        self.assertEqual(request.status, PENDING)
        self.assertTrue(request.digest)

    def test_dev_restart_executes_and_notifies(self):
        submission = self.submit("restart_service", {"service": "order-service", "env": "dev"})

        self.assertEqual(submission.status, "executed")
        self.assertEqual(submission.decision.tier, NOTIFY)
        self.assertIsNone(submission.request)
        self.assertEqual(submission.execution.status, "executed")
        # A low-risk write still gets an idempotency key.
        self.assertEqual(len(self.store.all_effects()), 1)
        events = [event["event"] for event in self.gateway.audit()]
        self.assertIn("notification.sent", events)

    def test_payment_restart_requires_four_eyes(self):
        request = self.pending("restart_service", {"service": "payment-service", "env": "prod"})

        self.assertEqual(request.tier, FOUR_EYES)
        self.assertEqual(request.required_approvals, 2)

    def test_scale_thresholds_use_arguments(self):
        small = self.submit(
            "scale_service",
            {"service": "order-service", "replicas": 5, "current_replicas": 3},
            task="SCALE-1",
        )
        large = self.submit(
            "scale_service",
            {"service": "order-service", "replicas": 6, "current_replicas": 3},
            task="SCALE-2",
        )
        zero = self.submit(
            "scale_service",
            {"service": "order-service", "replicas": 0, "current_replicas": 3},
            task="SCALE-3",
        )

        self.assertEqual(small.decision.tier, NOTIFY)
        self.assertEqual(small.status, "executed")
        self.assertEqual(large.decision.tier, APPROVAL)
        self.assertEqual(large.status, "pending")
        self.assertEqual(zero.decision.tier, FOUR_EYES)
        self.assertEqual(zero.status, "pending")

    # ---------- human decisions ----------

    def test_cannot_execute_pending_request(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})

        result = self.gateway.execute(request.request_id)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(self.store.all_effects()), 0)

    def test_single_approval_executes_once(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})
        decision = self.approve(request.request_id, "alice")

        self.assertEqual(decision.status, "approved")
        result = self.gateway.execute(request.request_id)

        self.assertEqual(result.status, "executed")
        self.assertEqual(len(self.store.all_effects()), 1)
        self.assertEqual(self.store.get_request(request.request_id).status, CONSUMED)

    def test_requester_cannot_approve_own_request(self):
        request = self.pending(
            "restart_service",
            {"service": "inventory-service", "env": "prod"},
            requested_by="carol",
        )

        result = self.approve(request.request_id, "carol")

        self.assertEqual(result.status, "blocked")
        self.assertIn("cannot approve their own", result.message)
        self.assertEqual(self.store.get_request(request.request_id).status, PENDING)

    def test_unauthorized_approver_is_blocked(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})

        result = self.approve(request.request_id, "dave")

        self.assertEqual(result.status, "blocked")
        self.assertIn("not authorized", result.message)

    def test_duplicate_approver_is_blocked(self):
        request = self.pending("restart_service", {"service": "payment-service", "env": "prod"})

        first = self.approve(request.request_id, "alice")
        second = self.approve(request.request_id, "alice")

        self.assertEqual(first.status, "pending_more")
        self.assertEqual(second.status, "blocked")
        self.assertEqual(len(self.store.get_request(request.request_id).approvals), 1)

    def test_four_eyes_requires_two_distinct_senior_approvers(self):
        request = self.pending("restart_service", {"service": "payment-service", "env": "prod"})

        first = self.approve(request.request_id, "alice")
        second = self.approve(request.request_id, "bob")

        self.assertEqual(first.status, "pending_more")
        self.assertEqual(second.status, "approved")
        result = self.gateway.execute(request.request_id)
        self.assertEqual(result.status, "executed")
        self.assertEqual(len(self.store.all_effects()), 1)

    def test_four_eyes_rejects_non_senior_approver(self):
        request = self.pending("restart_service", {"service": "payment-service", "env": "prod"})

        result = self.approve(request.request_id, "carol")

        self.assertEqual(result.status, "blocked")
        self.assertIn("not authorized", result.message)
        self.assertEqual(len(self.store.get_request(request.request_id).approvals), 0)

    def test_four_eyes_requester_cannot_be_second_approver(self):
        request = self.pending(
            "restart_service",
            {"service": "payment-service", "env": "prod"},
            requested_by="alice",
        )

        first = self.approve(request.request_id, "bob")
        second = self.approve(request.request_id, "alice")

        self.assertEqual(first.status, "pending_more")
        self.assertEqual(second.status, "blocked")
        self.assertEqual(len(self.store.get_request(request.request_id).approvals), 1)

    def test_rejection_is_terminal(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})

        rejected = self.gateway.reject(request.request_id, "bob", "not during peak hours")
        approval = self.approve(request.request_id, "alice")
        execution = self.gateway.execute(request.request_id)

        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(approval.status, "blocked")
        self.assertEqual(execution.status, "blocked")
        self.assertEqual(self.store.get_request(request.request_id).status, REJECTED)

    # ---------- binding, expiry, replay ----------

    def test_expired_request_cannot_execute(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})

        self.clock.advance(901)
        result = self.gateway.execute(request.request_id)

        self.assertEqual(result.status, "blocked")
        self.assertIn("expired", result.message)
        self.assertEqual(self.store.get_request(request.request_id).status, EXPIRED)
        self.assertIn("approval.expired", [event["event"] for event in self.gateway.audit()])

    def test_approval_after_expiry_is_rejected(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})

        self.clock.advance(901)
        result = self.approve(request.request_id, "alice")

        self.assertEqual(result.status, "expired")
        self.assertEqual(self.store.get_request(request.request_id).status, EXPIRED)

    def test_inbox_hides_expired_and_metrics_count_them(self):
        self.pending("restart_service", {"service": "order-service", "env": "prod"})

        self.clock.advance(901)
        self.assertEqual(self.gateway.inbox(), [])

        metrics = self.gateway.metrics()
        self.assertEqual(metrics["pending"], 0)
        self.assertEqual(metrics["expired"], 1)
        self.assertEqual(metrics["approval_rate"], 0.0)

    def test_action_substitution_is_blocked_by_digest(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})
        self.approve(request.request_id, "alice")

        swapped = Action(
            "restart_service",
            {"service": "payment-service", "env": "prod"},
            "agent/repomedic",
            "INC-1",
        )
        blocked = self.gateway.execute(request.request_id, action=swapped)

        self.assertEqual(blocked.status, "blocked")
        self.assertIn("digest", blocked.message)
        self.assertEqual(len(self.store.all_effects()), 0)

        accepted = self.gateway.execute(request.request_id)
        self.assertEqual(accepted.status, "executed")
        self.assertEqual(len(self.store.all_effects()), 1)

    def test_policy_escalation_blocks_old_approval(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})
        self.approve(request.request_id, "alice")

        strict = Policy(
            overrides={
                "restart_service": PolicyDecision(DENY, "freeze", "change freeze in effect")
            }
        )
        strict_gateway = ApprovalGateway(policy=strict, store=self.store, clock=self.clock)
        result = strict_gateway.execute(request.request_id)

        self.assertEqual(result.status, "blocked")
        self.assertIn("policy changed", result.message)
        self.assertEqual(self.store.get_request(request.request_id).status, APPROVED)
        self.assertEqual(len(self.store.all_effects()), 0)

    def test_crash_after_effect_retry_does_not_double_execute(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})
        self.approve(request.request_id, "alice")

        with self.assertRaises(SimulatedCrash):
            self.gateway.execute(request.request_id, crash_after_effect=True)

        self.assertEqual(len(self.store.all_effects()), 1)
        self.assertEqual(self.store.get_request(request.request_id).status, APPROVED)

        retry = self.gateway.execute(request.request_id)

        self.assertEqual(retry.status, "duplicate")
        self.assertTrue(retry.duplicate)
        self.assertEqual(len(self.store.all_effects()), 1)
        self.assertEqual(self.store.get_request(request.request_id).status, CONSUMED)

    def test_replay_of_consumed_approval_returns_recorded_effect(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})
        self.approve(request.request_id, "alice")
        first = self.gateway.execute(request.request_id)
        second = self.gateway.execute(request.request_id)

        self.assertEqual(first.status, "executed")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(first.result, second.result)
        self.assertEqual(len(self.store.all_effects()), 1)

    def test_two_approvals_for_same_business_action_do_not_double_execute(self):
        first = self.pending("restart_service", {"service": "order-service", "env": "prod"}, task="INC-1")
        self.approve(first.request_id, "alice")
        self.gateway.execute(first.request_id)

        second = self.pending("restart_service", {"service": "order-service", "env": "prod"}, task="INC-1")
        self.approve(second.request_id, "bob")
        result = self.gateway.execute(second.request_id)

        self.assertEqual(result.status, "duplicate")
        self.assertEqual(len(self.store.all_effects()), 1)

    def test_request_survives_process_reload(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})
        self.approve(request.request_id, "alice")

        reloaded_store = Store(self.state_path)
        reloaded = ApprovalGateway(store=reloaded_store, clock=self.clock)
        result = reloaded.execute(request.request_id)

        self.assertEqual(result.status, "executed")
        self.assertEqual(len(reloaded_store.all_effects()), 1)

    # ---------- audit and metrics ----------

    def test_audit_trail_covers_request_decision_execution(self):
        request = self.pending("restart_service", {"service": "order-service", "env": "prod"})
        self.approve(request.request_id, "alice")
        self.gateway.execute(request.request_id)

        events = [event["event"] for event in self.gateway.audit()]

        for expected in (
            "policy.evaluated",
            "approval.requested",
            "approval.approved",
            "execution.completed",
            "execution.consumed",
        ):
            self.assertIn(expected, events)
        for event in self.gateway.audit():
            self.assertTrue(event.get("at"))

    def test_metrics_flag_rubber_stamping(self):
        first = self.pending("restart_service", {"service": "order-service", "env": "prod"}, task="M-1")
        self.approve(first.request_id, "alice")

        second = self.pending("restart_service", {"service": "catalog-service", "env": "prod"}, task="M-2")
        self.clock.advance(60)
        self.approve(second.request_id, "alice")

        metrics = self.gateway.metrics(rubber_stamp_seconds=5)

        self.assertEqual(metrics["requests"], 2)
        self.assertEqual(metrics["approved"], 2)
        self.assertEqual(metrics["rubber_stamps"], 1)
        self.assertEqual(metrics["approval_rate"], 1.0)
        self.assertEqual(metrics["median_decision_seconds"], 30.0)
        self.assertEqual(metrics["effects"], 0)


if __name__ == "__main__":
    unittest.main()
