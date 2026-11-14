"""Risk policy for RepoMedic actions.

The policy answers one question: for this *exact* action, what level of
human involvement is required? It deliberately looks at arguments, not just
tool names: restarting payment-service is not the same operation as
restarting a dev service.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional

from models import (
    APPROVAL,
    AUTO,
    DENY,
    FOUR_EYES,
    NOTIFY,
    TIER_LABELS,
    Action,
    PolicyDecision,
)


POLICY_VERSION = "2026-11-14.1"

READ_ONLY_TOOLS = frozenset(
    {
        "query_logs",
        "check_health",
        "search_runbook",
        "read_file",
        "git_log",
        "list_incidents",
    }
)

NEVER_ALLOWED_TOOLS = frozenset(
    {
        "modify_firewall",
        "iam_attach_policy",
        "drop_database",
    }
)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ApproverDirectory:
    """Who is allowed to approve which tier.

    In production this would be backed by IAM/on-call groups, not a literal
    set. The point is that "approver" is an authenticated identity with
    authorization, not a free-text name.
    """

    oncall: FrozenSet[str]
    senior: FrozenSet[str]

    def authorize(self, approver: str, tier: int) -> bool:
        name = (approver or "").strip()
        if not name:
            return False
        if tier == FOUR_EYES:
            return name in self.senior
        if tier == APPROVAL:
            return name in self.oncall or name in self.senior
        return False


DEFAULT_DIRECTORY = ApproverDirectory(
    oncall=frozenset({"alice", "bob", "carol"}),
    senior=frozenset({"alice", "bob"}),
)


class Policy:
    """A small, deterministic rule set with an escape hatch for tests."""

    version = POLICY_VERSION

    def __init__(self, overrides: Optional[Dict[str, PolicyDecision]] = None) -> None:
        self.overrides = dict(overrides or {})

    def evaluate(self, action: Action) -> PolicyDecision:
        if action.tool in self.overrides:
            return self.overrides[action.tool]

        tool = action.tool
        args = action.args or {}

        if tool in READ_ONLY_TOOLS:
            return PolicyDecision(
                AUTO,
                "read-only",
                "%s only reads evidence; no external state changes." % tool,
            )

        if tool in NEVER_ALLOWED_TOOLS:
            return PolicyDecision(
                DENY,
                "never-allowed",
                "%s is outside the agent's authority; use a separate change process." % tool,
            )

        if tool == "restart_service":
            return self._restart(args)

        if tool == "scale_service":
            return self._scale(args)

        if tool == "rollback_deployment":
            return PolicyDecision(
                FOUR_EYES,
                "rollback",
                "Rolling back a deployment changes production behavior for all users.",
            )

        if tool == "delete_volume":
            return PolicyDecision(
                FOUR_EYES,
                "delete-volume",
                "Deleting a volume is irreversible; two senior approvers are required.",
            )

        # Unknown tools fail closed. Do not let a new tool bypass the policy
        # simply because nobody added a rule for it.
        return PolicyDecision(
            DENY,
            "unknown-tool",
            "No policy rule for %s; default is deny." % tool,
        )

    def _restart(self, args: Dict[str, Any]) -> PolicyDecision:
        service = str(args.get("service", "")).strip()
        env = str(args.get("env", "prod")).strip().lower()

        if not service:
            return PolicyDecision(DENY, "restart-missing-target", "restart_service requires a service.")

        if env in ("dev", "development", "staging", "stage"):
            return PolicyDecision(
                NOTIFY,
                "restart-nonprod",
                "Restarting %s in %s is reversible and limited to a non-production environment."
                % (service, env),
                notify=True,
            )

        if service == "payment-service" or env == "prod-payments":
            return PolicyDecision(
                FOUR_EYES,
                "restart-payment",
                "payment-service is on the money path; a bad restart has immediate financial impact.",
                required_approvals=2,
            )

        return PolicyDecision(
            APPROVAL,
            "restart-prod",
            "Restarting %s in production is reversible with effort, but it interrupts live traffic."
            % service,
            required_approvals=1,
        )

    def _scale(self, args: Dict[str, Any]) -> PolicyDecision:
        service = str(args.get("service", "")).strip()
        target = _as_int(args.get("replicas"), -1)
        current = _as_int(args.get("current_replicas"), -1)

        if not service or target < 0:
            return PolicyDecision(DENY, "scale-invalid", "scale_service requires a service and replicas.")

        if current < 0:
            return PolicyDecision(
                APPROVAL,
                "scale-unknown-current",
                "Current replica count is missing; a human should confirm the capacity change.",
                required_approvals=1,
            )

        if target == 0:
            return PolicyDecision(
                FOUR_EYES,
                "scale-to-zero",
                "Scaling %s to zero takes it offline; treat it as an outage." % service,
                required_approvals=2,
            )

        delta = abs(target - current)
        if delta > 2:
            return PolicyDecision(
                APPROVAL,
                "scale-large",
                "Changing %s by %d replicas has a material capacity and cost impact." % (service, delta),
                required_approvals=1,
            )

        return PolicyDecision(
            NOTIFY,
            "scale-small",
            "Changing %s by %d replicas is a small, reversible capacity adjustment." % (service, delta),
            notify=True,
        )

    def explain(self) -> List[Dict[str, str]]:
        """A human-readable rule table for the CLI and the lesson."""
        rows = [
            {
                "rule": "read-only",
                "match": ", ".join(sorted(READ_ONLY_TOOLS)),
                "tier": TIER_LABELS[AUTO],
                "note": "execute and audit",
            },
            {
                "rule": "restart-nonprod",
                "match": "restart_service env=dev/staging",
                "tier": TIER_LABELS[NOTIFY],
                "note": "execute and notify on-call",
            },
            {
                "rule": "restart-prod",
                "match": "restart_service env=prod, other services",
                "tier": TIER_LABELS[APPROVAL],
                "note": "one on-call approval",
            },
            {
                "rule": "restart-payment",
                "match": "restart_service service=payment-service",
                "tier": TIER_LABELS[FOUR_EYES],
                "note": "two senior approvers, requester excluded",
            },
            {
                "rule": "scale-small",
                "match": "scale_service delta<=2, replicas>0",
                "tier": TIER_LABELS[NOTIFY],
                "note": "execute and notify",
            },
            {
                "rule": "scale-large",
                "match": "scale_service delta>2",
                "tier": TIER_LABELS[APPROVAL],
                "note": "one on-call approval",
            },
            {
                "rule": "scale-unknown-current",
                "match": "scale_service without current_replicas",
                "tier": TIER_LABELS[APPROVAL],
                "note": "human confirms the capacity change",
            },
            {
                "rule": "scale-to-zero",
                "match": "scale_service replicas=0",
                "tier": TIER_LABELS[FOUR_EYES],
                "note": "treat as an outage",
            },
            {
                "rule": "rollback",
                "match": "rollback_deployment",
                "tier": TIER_LABELS[FOUR_EYES],
                "note": "two senior approvers",
            },
            {
                "rule": "delete-volume",
                "match": "delete_volume",
                "tier": TIER_LABELS[FOUR_EYES],
                "note": "irreversible",
            },
            {
                "rule": "never-allowed",
                "match": ", ".join(sorted(NEVER_ALLOWED_TOOLS)),
                "tier": TIER_LABELS[DENY],
                "note": "outside agent authority",
            },
            {
                "rule": "unknown-tool",
                "match": "anything else",
                "tier": TIER_LABELS[DENY],
                "note": "fail closed",
            },
        ]
        for tool, decision in sorted(self.overrides.items()):
            rows.append(
                {
                    "rule": "override:%s" % tool,
                    "match": tool,
                    "tier": TIER_LABELS[decision.tier],
                    "note": decision.reason,
                }
            )
        return rows
