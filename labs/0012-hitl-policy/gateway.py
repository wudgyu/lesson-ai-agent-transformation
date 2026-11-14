"""The approval gateway: policy -> request -> human decision -> idempotent effect.

The important ordering is:

    1. evaluate policy on the exact action
    2. create a request bound to the action digest
    3. accept decisions from authorized, distinct humans
    4. re-evaluate policy and expiry at execution time
    5. write the external effect under a business idempotency key
    6. mark the approval consumed

A crash between 5 and 6 is safe because 5 is idempotent. A crash before 5
leaves the approval usable; that is why expiry and revalidation exist.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import median
from typing import Any, Dict, List, Optional

from models import (
    APPROVAL,
    APPROVED,
    AUTO,
    CONSUMED,
    EXPIRED,
    FOUR_EYES,
    NOTIFY,
    PENDING,
    REJECTED,
    Action,
    ApprovalRequest,
    Clock,
    DecisionResult,
    ExecutionResult,
    PolicyDecision,
    Submission,
    from_iso,
    to_iso,
)
from policy import ApproverDirectory, DEFAULT_DIRECTORY, Policy
from store import Store


class SimulatedCrash(RuntimeError):
    """Raised after the effect is durable to imitate a process dying."""


class ApprovalGateway:
    def __init__(
        self,
        policy: Optional[Policy] = None,
        store: Optional[Store] = None,
        clock: Optional[Clock] = None,
        directory: Optional[ApproverDirectory] = None,
        ttl_by_tier: Optional[Dict[int, int]] = None,
    ) -> None:
        if store is None:
            raise ValueError("ApprovalGateway requires a Store")
        self.policy = policy or Policy()
        self.store = store
        self.clock = clock or Clock()
        self.directory = directory or DEFAULT_DIRECTORY
        self.ttl_by_tier = dict(ttl_by_tier or {APPROVAL: 900, FOUR_EYES: 300})

    # ---------- submission ----------

    def submit(self, action: Action) -> Submission:
        decision = self.policy.evaluate(action)
        self._audit(
            "policy.evaluated",
            tool=action.tool,
            digest=action.short_digest(),
            tier=decision.tier,
            rule=decision.rule_id,
            request_id="",
        )

        if decision.blocked:
            self._audit(
                "submission.denied",
                tool=action.tool,
                digest=action.short_digest(),
                reason=decision.reason,
            )
            self.store.save()
            return Submission(
                "blocked",
                "policy %s denies %s: %s" % (decision.rule_id, action.tool, decision.reason),
                decision,
            )

        if decision.tier in (AUTO, NOTIFY):
            # Reads do not belong in the side-effect ledger: repeating them is
            # safe and may need fresh data. Writes still get an idempotency key.
            execution = self._run(action, "", decision, use_ledger=(decision.tier != AUTO))
            if decision.tier == NOTIFY and execution.status == "executed":
                self._audit(
                    "notification.sent",
                    tool=action.tool,
                    request_id="",
                    detail="low-risk change executed; on-call notified",
                )
                self.store.save()
            message = "%s executed at tier %s." % (action.tool, decision.rule_id)
            if decision.tier == NOTIFY:
                message += " The on-call team was notified."
            return Submission("executed", message, decision, execution=execution)

        now = self.clock.now()
        ttl = int(self.ttl_by_tier.get(decision.tier, 900))
        required = decision.required_approvals
        if required <= 0:
            required = 2 if decision.tier == FOUR_EYES else 1
        request = ApprovalRequest(
            request_id=self.store.next_request_id(),
            digest=action.digest(),
            action=action.to_dict(),
            tier=decision.tier,
            rule_id=decision.rule_id,
            reason=decision.reason,
            required_approvals=required,
            created_at=to_iso(now),
            expires_at=to_iso(now + timedelta(seconds=ttl)),
        )
        self.store.put_request(request)
        self._audit(
            "approval.requested",
            request_id=request.request_id,
            tool=action.tool,
            digest=action.short_digest(),
            tier=decision.tier,
            required=required,
            expires_at=request.expires_at,
        )
        self.store.save()
        return Submission(
            "pending",
            "approval required: %s needs %d approver(s) before execution."
            % (action.tool, required),
            decision,
            request=request,
        )

    # ---------- human decisions ----------

    def approve(self, request_id: str, approver: str, note: str = "") -> DecisionResult:
        request = self.store.get_request(request_id)
        if request is None:
            return DecisionResult("blocked", "unknown request %s" % request_id)

        now = self.clock.now()
        if self._expire_if_needed(request, now):
            return DecisionResult("expired", "request %s expired before approval" % request_id, request)

        if request.status == CONSUMED:
            return DecisionResult("blocked", "request %s was already executed" % request_id, request)
        if request.status == REJECTED:
            return DecisionResult("blocked", "request %s was rejected" % request_id, request)
        if request.status == APPROVED:
            return DecisionResult("blocked", "request %s already has enough approvals" % request_id, request)
        if request.status != PENDING:
            return DecisionResult("blocked", "request %s is %s" % (request_id, request.status), request)

        name = (approver or "").strip()
        if not name:
            return DecisionResult("blocked", "approver identity is required", request)
        if name == request.requested_by:
            self._audit(
                "approval.self_approval_blocked",
                request_id=request_id,
                actor=name,
                tool=request.tool,
            )
            self.store.save()
            return DecisionResult(
                "blocked",
                "requester %s cannot approve their own request" % name,
                request,
            )
        if not self.directory.authorize(name, request.tier):
            self._audit(
                "approval.not_authorized",
                request_id=request_id,
                actor=name,
                tier=request.tier,
            )
            self.store.save()
            return DecisionResult(
                "blocked",
                "%s is not authorized to approve tier %s" % (name, request.tier),
                request,
            )
        if name in request.approvers():
            return DecisionResult("blocked", "%s has already approved this request" % name, request)

        request.approvals.append({"approver": name, "at": to_iso(now), "note": note})
        self.store.put_request(request)
        self._audit(
            "approval.approved",
            request_id=request_id,
            actor=name,
            tool=request.tool,
            count=len(request.approvals),
            required=request.required_approvals,
            note=note,
        )

        if len(request.approvals) >= request.required_approvals:
            request.status = APPROVED
            self.store.put_request(request)
            self.store.save()
            return DecisionResult("approved", "request %s is fully approved" % request_id, request)

        self.store.save()
        return DecisionResult(
            "pending_more",
            "approval recorded (%d/%d); waiting for another distinct approver"
            % (len(request.approvals), request.required_approvals),
            request,
        )

    def reject(self, request_id: str, approver: str, note: str = "") -> DecisionResult:
        request = self.store.get_request(request_id)
        if request is None:
            return DecisionResult("blocked", "unknown request %s" % request_id)

        now = self.clock.now()
        if self._expire_if_needed(request, now):
            return DecisionResult("expired", "request %s expired before rejection" % request_id, request)

        if request.status in (CONSUMED, REJECTED):
            return DecisionResult("blocked", "request %s is already %s" % (request_id, request.status), request)

        name = (approver or "").strip()
        if not name:
            return DecisionResult("blocked", "approver identity is required", request)
        if name in request.approvers():
            return DecisionResult("blocked", "%s already approved; ask another approver to reject" % name, request)
        if name != request.requested_by and not self.directory.authorize(name, request.tier):
            return DecisionResult(
                "blocked",
                "%s is not authorized to reject tier %s" % (name, request.tier),
                request,
            )

        request.rejections.append({"approver": name, "at": to_iso(now), "note": note})
        request.status = REJECTED
        self.store.put_request(request)
        self._audit(
            "approval.rejected",
            request_id=request_id,
            actor=name,
            tool=request.tool,
            note=note,
        )
        self.store.save()
        return DecisionResult("rejected", "request %s rejected by %s" % (request_id, name), request)

    # ---------- execution ----------

    def execute(
        self,
        request_id: str,
        action: Optional[Action] = None,
        crash_after_effect: bool = False,
    ) -> ExecutionResult:
        request = self.store.get_request(request_id)
        if request is None:
            return ExecutionResult("blocked", "unknown request %s" % request_id)

        now = self.clock.now()

        # Replay path: the approval was already consumed. Return the recorded
        # effect instead of doing it again.
        if request.status == CONSUMED:
            stored = self.store.get_effect(request.to_action().idempotency_key())
            if stored is not None:
                self._audit(
                    "execution.replay",
                    request_id=request_id,
                    tool=request.tool,
                    detail="approval already consumed; returning recorded effect",
                )
                self.store.save()
                return ExecutionResult(
                    "duplicate",
                    "approval %s was already used; no second effect" % request_id,
                    request_id=request_id,
                    idempotency_key=request.to_action().idempotency_key(),
                    duplicate=True,
                    result=stored,
                )
            return ExecutionResult(
                "blocked",
                "request %s is consumed but has no effect" % request_id,
                request_id=request_id,
            )

        if self._expire_if_needed(request, now):
            return ExecutionResult(
                "blocked",
                "request %s expired before execution" % request_id,
                request_id=request_id,
            )

        if request.status == REJECTED:
            return ExecutionResult(
                "blocked",
                "request %s was rejected" % request_id,
                request_id=request_id,
            )
        if request.status != APPROVED:
            return ExecutionResult(
                "blocked",
                "request %s is %s (%d/%d approvals)"
                % (request_id, request.status, len(request.approvals), request.required_approvals),
                request_id=request_id,
            )

        # The approval is bound to one exact action. If the caller presents
        # different arguments, it is not the action the human approved.
        if action is not None and action.digest() != request.digest:
            self._audit(
                "execution.digest_mismatch",
                request_id=request_id,
                tool=action.tool,
                approved_digest=request.digest[:12],
                presented_digest=action.short_digest(),
            )
            self.store.save()
            return ExecutionResult(
                "blocked",
                "presented action does not match the approved action digest",
                request_id=request_id,
            )

        approved_action = request.to_action()
        fresh = self.policy.evaluate(approved_action)

        # Policies can tighten while a request is parked. Never let an old
        # approval authorize an action the current policy forbids.
        if fresh.blocked:
            self._audit(
                "execution.policy_changed",
                request_id=request_id,
                tool=approved_action.tool,
                rule=fresh.rule_id,
                detail=fresh.reason,
            )
            self.store.save()
            return ExecutionResult(
                "blocked",
                "policy changed since approval: %s" % fresh.reason,
                request_id=request_id,
            )

        required_now = max(request.required_approvals, fresh.required_approvals)
        if len(request.approvals) < required_now:
            self._audit(
                "execution.escalated",
                request_id=request_id,
                tool=approved_action.tool,
                have=len(request.approvals),
                required=required_now,
            )
            self.store.save()
            return ExecutionResult(
                "blocked",
                "policy now requires %d approvals; only %d recorded"
                % (required_now, len(request.approvals)),
                request_id=request_id,
            )

        execution = self._run(
            approved_action,
            request.request_id,
            fresh,
            crash_after_effect=crash_after_effect,
        )

        if execution.status in ("executed", "duplicate"):
            request.status = CONSUMED
            request.consumed_at = to_iso(now)
            self.store.put_request(request)
            self._audit(
                "execution.consumed",
                request_id=request_id,
                tool=request.tool,
                idempotency_key=execution.idempotency_key,
            )
            self.store.save()

        return execution

    # ---------- expiry / views ----------

    def expire(self, request_id: Optional[str] = None) -> List[ApprovalRequest]:
        now = self.clock.now()
        expired: List[ApprovalRequest] = []
        candidates = (
            [self.store.get_request(request_id)] if request_id else self.store.all_requests()
        )
        for request in candidates:
            if request is None:
                continue
            if request.is_actionable() and request.is_expired(now):
                self._mark_expired(request)
                expired.append(request)
        self.store.save()
        return expired

    def inbox(self) -> List[ApprovalRequest]:
        # Lazy expiry: a read view should not show an expired request as
        # actionable just because no one has touched it yet.
        self.expire()
        return [r for r in self.store.all_requests() if r.is_actionable()]

    def audit(self) -> List[Dict[str, Any]]:
        return self.store.audit_events()

    def metrics(self, rubber_stamp_seconds: float = 5.0) -> Dict[str, Any]:
        requests = self.store.all_requests()
        now = self.clock.now()
        counts = {PENDING: 0, APPROVED: 0, REJECTED: 0, EXPIRED: 0, CONSUMED: 0}
        latencies: List[float] = []
        rubber_stamps = 0
        approved_decisions = 0
        decided_requests = 0

        for request in requests:
            status = request.status if request.status in counts else PENDING
            if status in (PENDING, APPROVED) and request.is_expired(now):
                status = EXPIRED
            counts[status] += 1

            # The approval rate measures what humans decided, not what the
            # final request status became. A request that was approved and
            # then expired before execution still counts as an approval.
            if request.approvals:
                approved_decisions += 1
            if request.approvals or request.rejections or status == EXPIRED:
                decided_requests += 1

            decisions = list(request.approvals) + list(request.rejections)
            if decisions:
                first = min(decisions, key=lambda item: item.get("at", ""))
                seconds = (self._parse(first.get("at", request.created_at)) - self._parse(request.created_at)).total_seconds()
                latencies.append(max(0.0, seconds))
                if seconds < rubber_stamp_seconds:
                    rubber_stamps += 1

        approval_rate = (approved_decisions / decided_requests) if decided_requests else None
        pending_ages = [
            (now - self._parse(r.created_at)).total_seconds()
            for r in requests
            if r.is_actionable() and not r.is_expired(now)
        ]

        return {
            "requests": len(requests),
            "pending": counts[PENDING],
            "approved": counts[APPROVED],
            "consumed": counts[CONSUMED],
            "rejected": counts[REJECTED],
            "expired": counts[EXPIRED],
            "approval_rate": approval_rate,
            "median_decision_seconds": median(latencies) if latencies else None,
            "p95_decision_seconds": self._p95(latencies) if latencies else None,
            "rubber_stamps": rubber_stamps,
            "rubber_stamp_threshold_seconds": rubber_stamp_seconds,
            "oldest_pending_seconds": max(pending_ages) if pending_ages else None,
            "effects": len(self.store.all_effects()),
            "audit_events": len(self.store.audit_events()),
        }

    # ---------- internals ----------

    def _run(
        self,
        action: Action,
        request_id: str,
        decision: PolicyDecision,
        crash_after_effect: bool = False,
        use_ledger: bool = True,
    ) -> ExecutionResult:
        if not use_ledger:
            effect = self._effect(action)
            self._audit(
                "execution.completed",
                request_id=request_id,
                tool=action.tool,
                tier=decision.tier,
                idempotency_key="",
                detail="read-only action; no effect ledger entry",
            )
            self.store.save()
            return ExecutionResult(
                "executed",
                "read-only %s executed; no side effect to deduplicate" % action.tool,
                request_id=request_id,
                result=effect,
            )

        key = action.idempotency_key()
        existing = self.store.get_effect(key)
        if existing is not None:
            self._audit(
                "execution.duplicate",
                request_id=request_id,
                tool=action.tool,
                idempotency_key=key,
                detail="effect ledger already has this key",
            )
            self.store.save()
            return ExecutionResult(
                "duplicate",
                "effect %s already applied; returning the recorded result" % key,
                request_id=request_id,
                idempotency_key=key,
                duplicate=True,
                result=existing,
            )

        effect = self._effect(action)
        # Persist the effect before consuming the approval. If the process
        # dies now, the retry sees the ledger and returns duplicate.
        self.store.put_effect(key, effect)
        self.store.save()
        self._audit(
            "execution.completed",
            request_id=request_id,
            tool=action.tool,
            tier=decision.tier,
            idempotency_key=key,
        )
        self.store.save()

        if crash_after_effect:
            raise SimulatedCrash(
                "simulated crash after effect %s was durably recorded" % key
            )

        return ExecutionResult(
            "executed",
            "effect %s applied" % key,
            request_id=request_id,
            idempotency_key=key,
            result=effect,
        )

    def _effect(self, action: Action) -> Dict[str, Any]:
        """Simulated side effect. Real tools would call the target system."""
        tool = action.tool
        args = action.args
        common = {"action": tool, "applied_at": to_iso(self.clock.now())}
        if tool == "restart_service":
            common.update(
                {
                    "service": args.get("service"),
                    "env": args.get("env", "prod"),
                    "restarted": True,
                    "note": "simulated rolling restart; no real process was touched",
                }
            )
        elif tool == "scale_service":
            common.update(
                {
                    "service": args.get("service"),
                    "replicas": args.get("replicas"),
                    "scaled": True,
                }
            )
        elif tool == "rollback_deployment":
            common.update(
                {
                    "service": args.get("service"),
                    "to_revision": args.get("to_revision"),
                    "rolled_back": True,
                }
            )
        elif tool == "delete_volume":
            common.update({"volume": args.get("volume"), "deleted": True})
        else:
            common.update({"read_only": True, "tool": tool})
        return common

    def _expire_if_needed(self, request: ApprovalRequest, now: Any) -> bool:
        if request.is_actionable() and request.is_expired(now):
            self._mark_expired(request)
            self.store.save()
            return True
        return False

    def _mark_expired(self, request: ApprovalRequest) -> None:
        request.status = EXPIRED
        self.store.put_request(request)
        self._audit(
            "approval.expired",
            request_id=request.request_id,
            tool=request.tool,
            expires_at=request.expires_at,
        )

    def _audit(self, event: str, **fields: Any) -> None:
        self.store.append_audit(to_iso(self.clock.now()), event, **fields)

    @staticmethod
    def _parse(value: str) -> Any:
        return from_iso(value)

    @staticmethod
    def _p95(values: List[float]) -> float:
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        index = int(round(0.95 * (len(ordered) - 1)))
        return ordered[index]
