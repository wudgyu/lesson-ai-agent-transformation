"""Agent state, facts, and checkpoints for lesson 0005.

State is what the agent knows and is doing *within* a session. It is not the
same thing as the conversation history:

- messages: the raw turn-by-turn record
- facts: structured, deduplicated claims with provenance
- open_questions / decisions: the parts a human would put on a whiteboard
- side_effects: idempotency keys for actions that must not repeat

The state is serializable, so it can be checkpointed and resumed.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text: str) -> int:
    """Rough token estimate for mixed Chinese/English text.

    Chinese is roughly 1.5–2 characters per token; English is roughly 4.
    Use 3 as a conservative middle ground. This is a budgeting tool, not a
    replacement for a real tokenizer.
    """
    if not text:
        return 0
    return max(1, len(text) // 3)


@dataclass
class Message:
    message_id: str
    role: str  # system | user | assistant | tool
    content: str
    kind: str = "text"  # text | tool_call | tool_result | summary | memory
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = now_iso()


@dataclass
class Fact:
    key: str
    value: str
    source: str
    confidence: float = 0.8
    status: str = "active"  # active | superseded
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = now_iso()


@dataclass
class AgentState:
    session_id: str
    messages: List[Message] = field(default_factory=list)
    facts: Dict[str, Fact] = field(default_factory=dict)
    fact_history: List[Fact] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    side_effects: List[str] = field(default_factory=list)
    turn: int = 0
    phase: str = "start"
    compaction_count: int = 0
    notes: str = ""
    message_seq: int = 0

    def add_message(
        self,
        role: str,
        content: str,
        kind: str = "text",
        message_id: Optional[str] = None,
    ) -> Message:
        self.message_seq += 1
        message = Message(
            message_id=message_id or "m%04d" % self.message_seq,
            role=role,
            content=content,
            kind=kind,
        )
        self.messages.append(message)
        return message

    def set_fact(
        self,
        key: str,
        value: str,
        source: str,
        confidence: float = 0.8,
    ) -> Fact:
        existing = self.facts.get(key)
        if existing is not None and existing.value != value:
            existing.status = "superseded"
            existing.updated_at = now_iso()
            self.fact_history.append(existing)
        fact = Fact(
            key=key,
            value=value,
            source=source,
            confidence=confidence,
        )
        self.facts[key] = fact
        return fact

    def get_fact(self, key: str) -> Optional[Fact]:
        return self.facts.get(key)

    def add_open_question(self, question: str) -> None:
        if question not in self.open_questions:
            self.open_questions.append(question)

    def resolve_open_question(self, question: str) -> None:
        self.open_questions = [
            item for item in self.open_questions if item != question
        ]

    def add_decision(self, decision: str) -> None:
        if decision not in self.decisions:
            self.decisions.append(decision)

    def record_side_effect(self, idempotency_key: str) -> bool:
        """Return True if this is the first time the side effect is recorded."""
        if idempotency_key in self.side_effects:
            return False
        self.side_effects.append(idempotency_key)
        return True

    def has_side_effect(self, idempotency_key: str) -> bool:
        return idempotency_key in self.side_effects

    def estimate_tokens(self) -> int:
        # `notes` mirrors the summary message; counting both would
        # double-charge the context budget.
        parts = [message.content for message in self.messages]
        for fact in self.facts.values():
            parts.append("%s=%s source=%s" % (fact.key, fact.value, fact.source))
        parts.extend(self.open_questions)
        parts.extend(self.decisions)
        return sum(estimate_tokens(part) for part in parts if part)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "messages": [asdict(message) for message in self.messages],
            "facts": {key: asdict(value) for key, value in self.facts.items()},
            "fact_history": [asdict(fact) for fact in self.fact_history],
            "open_questions": list(self.open_questions),
            "decisions": list(self.decisions),
            "tool_results": list(self.tool_results),
            "side_effects": list(self.side_effects),
            "turn": self.turn,
            "phase": self.phase,
            "compaction_count": self.compaction_count,
            "notes": self.notes,
            "message_seq": self.message_seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentState":
        state = cls(session_id=data["session_id"])
        state.messages = [Message(**item) for item in data.get("messages", [])]
        state.facts = {
            key: Fact(**item) for key, item in data.get("facts", {}).items()
        }
        state.fact_history = [
            Fact(**item) for item in data.get("fact_history", [])
        ]
        state.open_questions = list(data.get("open_questions", []))
        state.decisions = list(data.get("decisions", []))
        state.tool_results = list(data.get("tool_results", []))
        state.side_effects = list(data.get("side_effects", []))
        state.turn = int(data.get("turn", 0))
        state.phase = data.get("phase", "start")
        state.compaction_count = int(data.get("compaction_count", 0))
        state.notes = data.get("notes", "")
        state.message_seq = int(data.get("message_seq", 0))
        if state.message_seq < len(state.messages):
            state.message_seq = len(state.messages)
        return state


def save_checkpoint(state: AgentState, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_checkpoint(path: Path) -> AgentState:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return AgentState.from_dict(data)
