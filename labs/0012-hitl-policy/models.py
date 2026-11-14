"""Core data types for the Lesson 0012 human-in-the-loop policy lab.

The point of these types is to keep four things separate:

    Action          what the agent wants to do (tool + exact arguments)
    PolicyDecision  what the policy says should happen (auto / notify / approve / deny)
    ApprovalRequest what a human is asked to authorize (bound to an action digest)
    ExecutionResult what actually happened (executed / duplicate / blocked)

Keeping them separate is what makes approvals auditable and replay-safe.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# ---------- time ----------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    """UTC ISO-8601 with a trailing Z; stable across Python 3.9+."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    """Parse the ISO form emitted by to_iso(), including a trailing Z."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


class Clock:
    """Injectable clock so tests can jump forward without sleeping."""

    def now(self) -> datetime:
        return utcnow()


class FakeClock(Clock):
    def __init__(self, start: Optional[datetime] = None) -> None:
        self._now = start or datetime(2026, 11, 14, 2, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


# ---------- risk tiers ----------

AUTO = 0          # execute, no human involvement
NOTIFY = 1        # execute, but tell a human it happened
APPROVAL = 2      # one authorized human must approve
FOUR_EYES = 3     # two distinct authorized humans must approve
DENY = 4          # never execute through this gateway

TIER_NAMES = {
    AUTO: "auto",
    NOTIFY: "notify",
    APPROVAL: "approval",
    FOUR_EYES: "four-eyes",
    DENY: "deny",
}

TIER_LABELS = {
    AUTO: "T0 auto",
    NOTIFY: "T1 notify",
    APPROVAL: "T2 approval",
    FOUR_EYES: "T3 four-eyes",
    DENY: "T4 deny",
}


@dataclass(frozen=True)
class PolicyDecision:
    tier: int
    rule_id: str
    reason: str
    required_approvals: int = 0
    notify: bool = False

    @property
    def requires_approval(self) -> bool:
        return self.tier in (APPROVAL, FOUR_EYES)

    @property
    def blocked(self) -> bool:
        return self.tier == DENY

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["tier_name"] = TIER_NAMES[self.tier]
        data["tier_label"] = TIER_LABELS[self.tier]
        return data


# ---------- the action ----------

@dataclass(frozen=True)
class Action:
    """A concrete tool call. The digest covers the exact arguments."""

    tool: str
    args: Dict[str, Any]
    requested_by: str
    task_id: str = ""

    def canonical(self) -> str:
        return json.dumps(
            {
                "tool": self.tool,
                "args": self.args,
                "requested_by": self.requested_by,
                "task_id": self.task_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def short_digest(self) -> str:
        return self.digest()[:12]

    def idempotency_key(self) -> str:
        """Business identity of the effect, not the approval identity.

        Two different approvals for the same task + same action should not
        produce two restarts. A new attempt must use a new task_id (or an
        explicit business key) if it really is a new effect.
        """
        task = self.task_id or "adhoc"
        return "%s:%s" % (task, self.digest())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Action":
        return cls(
            tool=str(data["tool"]),
            args=dict(data.get("args") or {}),
            requested_by=str(data.get("requested_by") or ""),
            task_id=str(data.get("task_id") or ""),
        )


# ---------- approval request ----------

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
CONSUMED = "consumed"


@dataclass
class ApprovalRequest:
    request_id: str
    digest: str
    action: Dict[str, Any]
    tier: int
    rule_id: str
    reason: str
    required_approvals: int
    created_at: str
    expires_at: str
    status: str = PENDING
    approvals: List[Dict[str, str]] = field(default_factory=list)
    rejections: List[Dict[str, str]] = field(default_factory=list)
    consumed_at: str = ""

    @property
    def tool(self) -> str:
        return str(self.action.get("tool", ""))

    @property
    def requested_by(self) -> str:
        return str(self.action.get("requested_by", ""))

    @property
    def task_id(self) -> str:
        return str(self.action.get("task_id", ""))

    def is_expired(self, now: datetime) -> bool:
        return now >= from_iso(self.expires_at)

    def is_actionable(self) -> bool:
        return self.status in (PENDING, APPROVED)

    def approval_count(self) -> int:
        return len(self.approvals)

    def approvers(self) -> List[str]:
        return [str(item.get("approver", "")) for item in self.approvals]

    def to_action(self) -> Action:
        return Action.from_dict(self.action)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["tier_name"] = TIER_NAMES[self.tier]
        data["tier_label"] = TIER_LABELS[self.tier]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalRequest":
        return cls(
            request_id=str(data["request_id"]),
            digest=str(data["digest"]),
            action=dict(data["action"]),
            tier=int(data["tier"]),
            rule_id=str(data["rule_id"]),
            reason=str(data.get("reason", "")),
            required_approvals=int(data.get("required_approvals", 1)),
            created_at=str(data["created_at"]),
            expires_at=str(data["expires_at"]),
            status=str(data.get("status", PENDING)),
            approvals=list(data.get("approvals") or []),
            rejections=list(data.get("rejections") or []),
            consumed_at=str(data.get("consumed_at", "")),
        )


# ---------- results ----------

@dataclass
class ExecutionResult:
    status: str                  # executed | duplicate | blocked
    message: str
    request_id: str = ""
    idempotency_key: str = ""
    duplicate: bool = False
    result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Submission:
    status: str                  # executed | pending | blocked
    message: str
    decision: PolicyDecision
    request: Optional[ApprovalRequest] = None
    execution: Optional[ExecutionResult] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "decision": self.decision.to_dict(),
            "request": self.request.to_dict() if self.request else None,
            "execution": self.execution.to_dict() if self.execution else None,
        }


@dataclass
class DecisionResult:
    status: str                  # approved | pending_more | rejected | expired | blocked
    message: str
    request: Optional[ApprovalRequest] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "request": self.request.to_dict() if self.request else None,
        }
