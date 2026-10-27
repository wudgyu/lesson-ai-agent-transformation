"""Long-term memory store for lesson 0005.

Short-term memory lives in AgentState and is thread-scoped. Long-term memory
lives here and survives sessions. The store is deliberately small:

- typed memories: semantic / episodic / procedural
- provenance and confidence
- deduplication
- TTL
- keyword recall with recency and confidence priors

It is not a vector database. The point is the write/read policy, not the index.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MEMORY_TYPES = ("semantic", "episodic", "procedural")
LATIN_RE = re.compile(r"[a-z0-9_]+")
CJK_RE = re.compile(r"[一-鿿]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tokenize_simple(text: str) -> List[str]:
    """Lowercase Latin words + overlapping CJK bigrams.

    Same spirit as the lesson 3 tokenizer, without the code-identifier rules.
    """
    lowered = (text or "").lower()
    tokens: List[str] = []
    for match in LATIN_RE.finditer(lowered):
        tokens.append(match.group(0))
    for match in CJK_RE.finditer(lowered):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class MemoryItem:
    memory_id: str
    type: str
    text: str
    source: str
    tags: List[str] = field(default_factory=list)
    confidence: float = 0.8
    created_at: str = ""
    last_used_at: str = ""
    ttl_days: Optional[int] = None

    def __post_init__(self) -> None:
        if self.type not in MEMORY_TYPES:
            raise ValueError("unknown memory type: %s" % self.type)
        if not self.created_at:
            self.created_at = now_iso()
        if not self.last_used_at:
            self.last_used_at = self.created_at

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.ttl_days is None:
            return False
        now = now or datetime.now(timezone.utc)
        created = datetime.fromisoformat(self.created_at)
        age_days = (now - created).total_seconds() / 86400.0
        return age_days > self.ttl_days


class MemoryStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self.items: Dict[str, MemoryItem] = {}
        self._next = 1
        if self.path and self.path.exists():
            self.load()

    def remember(
        self,
        type: str,
        text: str,
        source: str,
        tags: Optional[Sequence[str]] = None,
        confidence: float = 0.8,
        ttl_days: Optional[int] = None,
    ) -> MemoryItem:
        normalized = _normalize(text)
        for item in self.items.values():
            if item.type == type and _normalize(item.text) == normalized:
                item.confidence = max(item.confidence, confidence)
                item.last_used_at = now_iso()
                item.tags = sorted(set(item.tags) | set(tags or []))
                self.save()
                return item

        memory_id = "M%03d" % self._next
        self._next += 1
        item = MemoryItem(
            memory_id=memory_id,
            type=type,
            text=text.strip(),
            source=source,
            tags=list(dict.fromkeys(tags or [])),
            confidence=confidence,
            ttl_days=ttl_days,
        )
        self.items[memory_id] = item
        self.save()
        return item

    def search(
        self,
        query: str,
        top_k: int = 3,
        now: Optional[datetime] = None,
    ) -> List[Tuple[MemoryItem, float]]:
        self.expire(now)
        query_terms = set(tokenize_simple(query))
        if not query_terms:
            return []
        scored: List[Tuple[MemoryItem, float]] = []
        now = now or datetime.now(timezone.utc)
        for item in self.items.values():
            item_terms = set(tokenize_simple(item.text + " " + " ".join(item.tags)))
            overlap = len(query_terms & item_terms) / len(query_terms)
            if overlap <= 0:
                continue
            created = datetime.fromisoformat(item.created_at)
            age_days = max(0.0, (now - created).total_seconds() / 86400.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score = 0.60 * overlap + 0.20 * recency + 0.20 * item.confidence
            scored.append((item, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].memory_id))
        return scored[:top_k]

    def expire(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        expired = [
            memory_id
            for memory_id, item in self.items.items()
            if item.is_expired(now)
        ]
        for memory_id in expired:
            del self.items[memory_id]
        if expired:
            self.save()
        return len(expired)

    def forget(self, memory_id: str) -> bool:
        if memory_id not in self.items:
            return False
        del self.items[memory_id]
        self.save()
        return True

    def all(self) -> List[MemoryItem]:
        return sorted(self.items.values(), key=lambda item: item.memory_id)

    def format_for_context(
        self, results: Sequence[Tuple[MemoryItem, float]]
    ) -> str:
        if not results:
            return ""
        lines = ["[LONG-TERM MEMORY]"]
        for item, _ in results:
            lines.append(
                "[%s] (%s, source=%s) %s"
                % (item.memory_id, item.type, item.source, item.text)
            )
        return "\n".join(lines)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "next": self._next,
            "items": [asdict(item) for item in self.all()],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self._next = int(payload.get("next", 1))
        self.items = {}
        for raw in payload.get("items", []):
            item = MemoryItem(**raw)
            self.items[item.memory_id] = item


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()
