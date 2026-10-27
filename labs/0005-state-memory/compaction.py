"""Compaction and naive truncation for lesson 0005.

Compaction is not "summarize for a human". It is a state transfer: decide what
must survive into the next context window, and make that explicit.

This module prefers structured state (facts / decisions / open questions) over
free-form summarization. That is deliberate: a summary that only says "we
talked about the connection pool" is not enough to resume work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from state import AgentState, Message, estimate_tokens


@dataclass
class CompactionResult:
    tokens_before: int
    tokens_after: int
    dropped_messages: int
    preserved_facts: List[str]
    summary: str


def should_compact(
    state: AgentState,
    max_tokens: int,
    threshold: float = 0.75,
) -> bool:
    if max_tokens <= 0:
        return True
    return state.estimate_tokens() >= int(max_tokens * threshold)


def build_summary(state: AgentState, max_tool_results: int = 2) -> str:
    # The first user message is always kept by compact_state; do not duplicate
    # the task here.
    lines: List[str] = ["# Compacted session state"]

    if state.facts:
        lines.extend(["", "## Confirmed facts"])
        for key in sorted(state.facts):
            fact = state.facts[key]
            lines.append(
                "- %s = %s (source: %s, confidence: %.2f)"
                % (fact.key, fact.value, fact.source, fact.confidence)
            )

    if state.decisions:
        lines.extend(["", "## Decisions / constraints"])
        for decision in state.decisions:
            lines.append("- %s" % decision[:180])

    if state.open_questions:
        lines.extend(["", "## Open questions"])
        for question in state.open_questions:
            lines.append("- %s" % question[:180])

    recent = state.tool_results[-max_tool_results:]
    if recent:
        lines.extend(["", "## Recent tool evidence"])
        for item in recent:
            name = item.get("tool", "tool")
            summary = item.get("summary", "")
            if summary:
                lines.append("- %s: %s" % (name, summary[:160]))

    return "\n".join(lines).strip()


def compact_state(
    state: AgentState,
    keep_recent: int = 4,
    summary: Optional[str] = None,
) -> CompactionResult:
    tokens_before = state.estimate_tokens()
    summary_text = summary if summary is not None else build_summary(state)
    preserved_facts = sorted(state.facts)

    system_messages = [
        message
        for message in state.messages
        if message.role == "system" and message.kind != "summary"
    ]
    first_user = _first_user_message_obj(state)
    recent = state.messages[-keep_recent:] if keep_recent > 0 else []

    kept: List[Message] = []
    seen = set()
    for message in system_messages + ([first_user] if first_user else []) + recent:
        if message.message_id in seen:
            continue
        seen.add(message.message_id)
        kept.append(message)

    summary_message = Message(
        message_id="summary-%04d" % (state.compaction_count + 1),
        role="system",
        content="[COMPACTED SESSION STATE]\n" + summary_text,
        kind="summary",
    )
    # Put the summary after the system/task messages, before the recent turns.
    insert_at = len(system_messages) + (1 if first_user else 0)
    kept.insert(insert_at, summary_message)

    dropped = len(state.messages) - len(
        [message for message in state.messages if message.message_id in seen]
    )
    state.messages = kept
    state.notes = summary_text
    state.compaction_count += 1

    return CompactionResult(
        tokens_before=tokens_before,
        tokens_after=state.estimate_tokens(),
        dropped_messages=max(0, dropped),
        preserved_facts=preserved_facts,
        summary=summary_text,
    )


def compact_if_needed(
    state: AgentState,
    max_tokens: int,
    threshold: float = 0.75,
    keep_recent: int = 4,
) -> Optional[CompactionResult]:
    if not should_compact(state, max_tokens, threshold):
        return None
    return compact_state(state, keep_recent=keep_recent)


def truncate_oldest(state: AgentState, max_tokens: int) -> int:
    """Naive fallback: drop oldest non-system messages until under budget.

    This is intentionally lossy. It is the baseline that compaction should
    beat on fact/constraint retention, not a recommended production strategy.
    """
    dropped = 0
    while state.estimate_tokens() > max_tokens and len(state.messages) > 2:
        index = next(
            (
                i
                for i, message in enumerate(state.messages)
                if message.role != "system"
            ),
            None,
        )
        if index is None:
            break
        state.messages.pop(index)
        dropped += 1
    return dropped


def _first_user_message_obj(state: AgentState) -> Optional[Message]:
    for message in state.messages:
        if message.role == "user":
            return message
    return None
