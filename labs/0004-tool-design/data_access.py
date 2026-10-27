"""Domain data access for the lesson 0004 lab.

刻意把「领域逻辑」与「工具包装」分开：同一份数据，baseline 工具用七个薄包装去读，
refactored 工具用四个高层工具去读。工具设计的好坏，不应该改变底层数据访问。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from tool_base import ToolError

ROOT = Path(__file__).resolve().parent
SANDBOX = ROOT / "sandbox"

# 固定“现在”，让日志窗口与审批过期在每次运行时都可复现。
NOW = datetime(2026, 10, 24, 20, 40, 0, tzinfo=timezone(timedelta(hours=8)))

_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00)\s+"
    r"(?P<level>ERROR|WARN|INFO)\s+(?P<msg>.*)$"
)


def _read_json(name: str) -> Any:
    path = SANDBOX / name
    if not path.is_file():
        raise ToolError("data file missing: %s" % name)
    return json.loads(path.read_text(encoding="utf-8"))


def services() -> Dict[str, Dict[str, Any]]:
    return _read_json("services.json")


def available_services() -> List[str]:
    return sorted(services())


def service_record(name: str) -> Dict[str, Any]:
    name = (name or "").strip()
    data = services()
    if name not in data:
        raise ToolError(
            "unknown service '%s'. Available: %s" % (name, ", ".join(sorted(data)))
        )
    return data[name]


def read_logs(service: str) -> List[Dict[str, Any]]:
    service_record(service)  # validate before touching the filesystem
    path = SANDBOX / "logs" / ("%s.log" % service)
    if not path.is_file():
        raise ToolError(
            "no log file for '%s'. Available: %s"
            % (service, ", ".join(sorted(p.stem for p in (SANDBOX / "logs").glob("*.log"))))
        )
    entries: List[Dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = _LOG_RE.match(raw.strip())
        if not match:
            continue
        entries.append(
            {
                "ts": datetime.fromisoformat(match.group("ts")),
                "level": match.group("level"),
                "message": match.group("msg"),
                "line": raw.strip(),
            }
        )
    return entries


def runbooks() -> List[Tuple[str, List[str]]]:
    docs: List[Tuple[str, List[str]]] = []
    for path in sorted((SANDBOX / "docs").glob("*.md")):
        rel = str(path.relative_to(SANDBOX))
        docs.append((rel, path.read_text(encoding="utf-8").splitlines()))
    return docs


def approvals() -> Dict[str, Dict[str, Any]]:
    return _read_json("approvals.json")


def restart_raw(service: str, reason: str) -> Dict[str, Any]:
    """底层重启动作。没有权限校验——那是工具层必须补上的。"""
    service_record(service)
    digest = hashlib.sha1(
        ("%s|%s|%s" % (service, reason, NOW.isoformat())).encode("utf-8")
    ).hexdigest()[:8]
    return {
        "status": "restarted",
        "service": service,
        "reason": reason,
        "at": NOW.isoformat(),
        "receipt_id": "RST-%s-%s" % (service, digest),
    }
