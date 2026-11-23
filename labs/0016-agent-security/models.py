"""Core data types for the RepoMedic security lab.

The lab separates four things that are usually mixed together in agent code:

- a *tool call* (the model's proposal),
- the *provenance* of each argument (where the value came from),
- the *policy decision* (allow / deny / needs approval),
- the *effect* (what the simulated world actually did).

Keeping them separate is what makes the security properties testable.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

TRUSTED = "trusted"
UNTRUSTED = "untrusted"
SECRET = "secret"


@dataclass(frozen=True)
class Origin:
    """A piece of context and the trust we assign to it.

    ``content`` is what the model would see.  It is deliberately kept short in
    fixtures; the point is the label, not the prose.
    """

    id: str
    trust: str
    description: str
    content: str = ""


@dataclass(frozen=True)
class Arg:
    """A tool argument plus the context origins it was derived from.

    Empty ``origins`` means a literal chosen by the trusted plan (for example
    ``"feature/fix-npe"``).  Any non-empty tuple is resolved against the
    ``TaintTracker``; an unknown origin fails closed as untrusted.
    """

    value: Any
    origins: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool: str
    args: Dict[str, Arg]
    note: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: str
    sink: Optional[str] = None  # None | "egress" | "exec" | "write"
    requires_approval: bool = False
    arg_types: Dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class Task:
    """Capabilities granted by the trusted operator for one task."""

    id: str
    capabilities: FrozenSet[str]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    layer: str
    reason: str
    detail: str = ""
    requires_approval: bool = False
    digest: str = ""


@dataclass(frozen=True)
class Approval:
    digest: str
    approver: str
    note: str = ""


@dataclass
class Effect:
    call_id: str
    kind: str  # fs_read | net_get | exec | repo_write | comment | repo_delete
    target: str
    payload: str = ""
    facts: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditEvent:
    seq: int
    call_id: str
    tool: str
    layer: str
    outcome: str  # allow | deny | approval_required | approval_granted | redact
    reason: str
    detail: str = ""
    origins: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    id: str
    kind: str  # "attack" | "benign"
    category: str
    title: str
    user_request: str
    origins: Tuple[Origin, ...]
    calls: Tuple[ToolCall, ...]
    capabilities: FrozenSet[str]
    harm_check: str = ""
    expected_layer: str = ""
    expect_effect: str = ""
    notes: str = ""


@dataclass
class CaseRun:
    case_id: str
    kind: str
    category: str
    blocked: bool
    blocked_by: str
    harm: bool
    utility: bool
    false_positive: bool
    redactions: int
    approvals: int
    effects: List[Effect]
    audit: List[AuditEvent]


def action_digest(call: ToolCall) -> str:
    """Stable digest binding an approval to one concrete action.

    The digest covers the tool name and every argument *value* and *origin*.
    Changing the target branch, the diff, or where the value came from changes
    the digest and invalidates a previously granted approval.
    """

    import hashlib
    import json

    payload = {
        "tool": call.tool,
        "args": {
            name: {"value": arg.value, "origins": list(arg.origins)}
            for name, arg in sorted(call.args.items())
        },
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
