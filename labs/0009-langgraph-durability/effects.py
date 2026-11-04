"""A tiny external ledger for side effects.

The graph checkpoint is *not* a transaction around the outside world. If a node
calls `restart_service` and the process dies before the node returns, the
checkpoint knows nothing about the restart. On resume the node runs again.

This file models the external system: a JSON ledger keyed by an idempotency
key. A real implementation would use a unique constraint in Postgres/Redis.

Lesson 0009 deliberately keeps this dependency-free and file-based so the
tests can run offline. It is not safe for concurrent writers; that is a
production concern discussed in the lesson.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class EffectStore:
    """File-backed side-effect ledger with idempotent restart."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, items: List[Dict[str, Any]]) -> None:
        self.path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def items(self) -> List[Dict[str, Any]]:
        return list(self._read())

    def count(self) -> int:
        return len(self._read())

    def restart(
        self,
        service: str,
        approval_id: str,
        idempotent: bool = True,
    ) -> Dict[str, Any]:
        """Record a restart, or return the previous result for the same key.

        `idempotent=False` exists only for the lab's contrast test: it shows
        what happens when a resumed node repeats an unkeyed side effect.
        """
        key = "restart:%s:%s" % (service, approval_id)
        items = self._read()

        if idempotent:
            for item in items:
                if item.get("key") == key:
                    return {
                        "status": "noop",
                        "service": service,
                        "approval_id": approval_id,
                        "idempotency_key": key,
                        "message": "side effect already recorded; not repeated",
                    }

        item = {
            "key": key,
            "action": "restart_service",
            "service": service,
            "approval_id": approval_id,
            "sequence": len(items) + 1,
        }
        items.append(item)
        self._write(items)
        return {
            "status": "restarted",
            "service": service,
            "approval_id": approval_id,
            "idempotency_key": key,
            "message": "restart_service executed and recorded",
        }
