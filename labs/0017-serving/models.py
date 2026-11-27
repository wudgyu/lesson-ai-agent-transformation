"""Core data types for the RepoMedic serving lab.

The lab keeps the *request* separate from the *run*:

- a request is short-lived and lives on one HTTP connection;
- a run is the long-lived unit of work with its own id, event log and result.

Everything else in the lab (admission, streaming, retry, shutdown) follows from
that split.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class RunRequest:
    """The immutable input of one run, plus the tenant that owns it."""

    tenant: str
    service: str
    incident: str
    idempotency_key: str
    priority: str = "normal"  # normal | critical

    def payload(self) -> Dict[str, str]:
        """The part that a fingerprint covers (key and tenant excluded)."""
        return {
            "service": self.service,
            "incident": self.incident,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class Event:
    """One entry in a run's append-only event log."""

    seq: int
    type: str
    data: Dict[str, Any]
    ts: float = 0.0


@dataclass
class Run:
    """The server-side state of one run.

    ``checkpoint`` is deliberately a plain dict: a real implementation would
    persist it in Redis/Postgres, but the lab wants the *shape* to be visible.
    """

    run_id: str
    request: RunRequest
    status: str = "queued"  # queued | running | succeeded | failed | cancelled
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    attempts: int = 0
    checkpoint: Dict[str, Any] = field(default_factory=dict)
    events: List[Event] = field(default_factory=list)
    last_seq: int = 0
    cancel_requested: bool = False
    resumable: bool = False

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def public(self) -> Dict[str, Any]:
        """A JSON-safe view for the HTTP API (no event log, no checkpoint)."""
        out = {
            "run_id": self.run_id,
            "status": self.status,
            "service": self.request.service,
            "incident": self.request.incident,
            "tenant": self.request.tenant,
            "attempts": self.attempts,
            "last_event_id": self.last_seq,
        }
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error
        if self.checkpoint:
            out["phase_index"] = self.checkpoint.get("phase_index", 0)
        return out


@dataclass(frozen=True)
class Admission:
    """The outcome of asking the service to create a run.

    ``status`` uses HTTP status codes on purpose: the core service and the HTTP
    adapter should agree on one vocabulary instead of inventing two.
    """

    ok: bool
    status: int
    code: str
    message: str = ""
    run_id: Optional[str] = None
    retry_after: Optional[int] = None


@dataclass
class IdempotencyRecord:
    fingerprint: str
    run_id: str
    created_at: float
    expires_at: float


@dataclass
class Subscription:
    """A live subscriber for one run's events."""

    sub_id: int
    queue: Any  # asyncio.Queue, kept untyped for Python 3.9 friendliness
    closed: bool = False
