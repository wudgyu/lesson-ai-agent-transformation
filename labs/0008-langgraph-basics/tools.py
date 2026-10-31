"""RepoMedic tools for the LangGraph lesson.

The registry is the same shape as lesson 0003: schemas for the model,
implementations for the graph. Tool errors are returned as observations
("ERROR: ...") instead of crashing the node.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List


class ToolError(Exception):
    """An expected tool error that should be shown to the model."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        func: Callable[..., Any],
    ) -> None:
        self._tools[name] = {
            "description": description,
            "parameters": parameters,
            "func": func,
        }

    def schemas(self) -> List[Dict[str, Any]]:
        """OpenAI-compatible function-calling schemas."""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for name, tool in self._tools.items()
        ]

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        if name not in self._tools:
            return "ERROR: unknown tool '%s'. Available: %s" % (
                name,
                ", ".join(sorted(self._tools)),
            )
        try:
            return str(self._tools[name]["func"](**arguments))
        except ToolError as exc:
            return "ERROR: %s" % exc
        except TypeError as exc:
            return "ERROR: bad arguments for '%s': %s" % (name, exc)
        except Exception as exc:  # a bad tool must not take down the graph
            return "ERROR: tool '%s' failed: %s: %s" % (name, type(exc).__name__, exc)


def build_registry(sandbox: Path) -> ToolRegistry:
    sandbox = Path(sandbox).resolve()
    registry = ToolRegistry()

    def get_service_health(service: str) -> str:
        path = sandbox / "health.json"
        if not path.is_file():
            raise ToolError("health.json not found in sandbox")
        data = json.loads(path.read_text(encoding="utf-8"))
        if service not in data:
            raise ToolError(
                "unknown service '%s'. Known: %s"
                % (service, ", ".join(sorted(data)))
            )
        item = data[service]
        return "\n".join("%s=%s" % (key, item[key]) for key in sorted(item))

    def search_runbooks(query: str) -> str:
        query = query.strip().lower()
        if not query:
            raise ToolError("query must not be empty")
        hits: List[str] = []
        for path in sorted((sandbox / "runbooks").glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if query in line.lower():
                    hits.append(
                        "%s:%d: %s"
                        % (path.relative_to(sandbox), lineno, line.strip())
                    )
        if not hits:
            return "(no matches for '%s')" % query
        return "\n".join(hits)

    registry.register(
        "get_service_health",
        "读取某个服务的健康快照：状态、错误率、P95 延迟、连接池使用量与版本。"
        "排查任何服务问题时，先调用它建立事实基线。",
        {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "服务名，例如 order-service 或 payment-service",
                }
            },
            "required": ["service"],
        },
        get_service_health,
    )
    registry.register(
        "search_runbooks",
        "在 runbook 目录中按关键词搜索（不区分大小写），返回文件名、行号与原句。"
        "用于查找已知故障模式与处置步骤；只返回匹配行，不返回整篇文档。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词，例如 connection pool、N+1、rollback",
                }
            },
            "required": ["query"],
        },
        search_runbooks,
    )
    return registry
