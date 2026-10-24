"""Shared tool plumbing for lesson 0002.

工具是「确定性代码」与「非确定性模型」之间的契约。这个文件只负责两件事：
- 把工具 schema 暴露给模型；
- 把执行错误转成模型能读懂的文本，而不是让进程崩掉。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List


class ToolError(Exception):
    """可预期的工具错误：以 ERROR: ... 的形式返回给模型。"""


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

    def names(self) -> List[str]:
        return sorted(self._tools)

    def schemas(self) -> List[Dict[str, Any]]:
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
                ", ".join(self.names()),
            )
        try:
            return str(self._tools[name]["func"](**arguments))
        except ToolError as exc:
            return "ERROR: %s" % exc
        except TypeError as exc:
            return "ERROR: bad arguments for '%s': %s" % (name, exc)
        except Exception as exc:
            return "ERROR: tool '%s' failed: %s: %s" % (name, type(exc).__name__, exc)
