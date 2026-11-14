"""Durable state for approvals, audit events and external effects.

The JSON file is intentionally boring: this lab runs offline and wants the
crash window to be visible. In production, replace it with a transactional
table (request row + unique constraint on the idempotency key) and do the
conditional update with optimistic locking.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from models import ApprovalRequest


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = {
            "seq": 0,
            "requests": {},
            "effects": {},
            "audit": [],
        }
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            loaded = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise RuntimeError("cannot read state file %s: %s" % (self.path, exc))
        if not isinstance(loaded, dict):
            raise RuntimeError("state file %s is not a JSON object" % self.path)
        self.data = {
            "seq": int(loaded.get("seq", 0)),
            "requests": dict(loaded.get("requests") or {}),
            "effects": dict(loaded.get("effects") or {}),
            "audit": list(loaded.get("audit") or []),
        }

    def save(self) -> None:
        """Write atomically: a crash leaves either the old or the new file."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(str(tmp), str(self.path))

    # ---------- requests ----------

    def next_request_id(self) -> str:
        self.data["seq"] = int(self.data["seq"]) + 1
        return "APR-%04d" % self.data["seq"]

    def put_request(self, request: ApprovalRequest) -> None:
        self.data["requests"][request.request_id] = request.to_dict()

    def get_request(self, request_id: str) -> Optional[ApprovalRequest]:
        raw = self.data["requests"].get(request_id)
        if raw is None:
            return None
        return ApprovalRequest.from_dict(raw)

    def all_requests(self) -> List[ApprovalRequest]:
        requests = [ApprovalRequest.from_dict(raw) for raw in self.data["requests"].values()]
        return sorted(requests, key=lambda item: item.created_at)

    # ---------- effects ----------

    def get_effect(self, key: str) -> Optional[Dict[str, Any]]:
        value = self.data["effects"].get(key)
        return dict(value) if value is not None else None

    def put_effect(self, key: str, value: Dict[str, Any]) -> None:
        self.data["effects"][key] = value

    def all_effects(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.data["effects"])

    # ---------- audit ----------

    def append_audit(self, at: str, event: str, **fields: Any) -> Dict[str, Any]:
        record = {"at": at, "event": event}
        record.update(fields)
        self.data["audit"].append(record)
        return record

    def audit_events(self) -> List[Dict[str, Any]]:
        return list(self.data["audit"])

    def reset(self) -> None:
        self.data = {"seq": 0, "requests": {}, "effects": {}, "audit": []}
        if self.path.exists():
            self.path.unlink()
