"""工具注册表：schema 与实现放在一起，方便模型“看见”和你的代码“执行”。

设计要点（第 1 课会展开）：
- 工具名用 snake_case，动词开头；
- description 是给模型看的 prompt，不是给编译器看的注释；
- 参数尽量少、类型明确，错误返回可读的文本而不是抛栈；
- 所有文件访问都被限制在 sandbox/ 内。
"""
from __future__ import annotations

import ast
import operator
from pathlib import Path
from typing import Any, Callable, Dict, List


class ToolError(Exception):
    """可预期的工具错误：会以 ERROR: ... 的形式返回给模型。"""


_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ToolError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.BinOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ToolError("operator not allowed")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1_000_000):
            raise ToolError("exponent or base too large")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ToolError("operator not allowed")
        return op(_eval_node(node.operand))
    raise ToolError("expression contains unsupported syntax")


def safe_calculate(expression: str) -> str:
    """只允许数字、括号和算术运算符，避免 eval 带来的代码执行风险。"""
    if len(expression) > 200:
        raise ToolError("expression is too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError("invalid expression: %s" % exc)
    value = _eval_node(tree)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "%s = %s" % (expression, value)


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
        """转成 OpenAI-compatible function calling 的 tools 参数。"""
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
        except Exception as exc:  # 工具不允许把进程打崩
            return "ERROR: tool '%s' failed: %s: %s" % (name, type(exc).__name__, exc)


def build_registry(sandbox: Path) -> ToolRegistry:
    sandbox = Path(sandbox).resolve()
    registry = ToolRegistry()

    def _resolve(path: str) -> Path:
        candidate = (sandbox / path).resolve()
        try:
            candidate.relative_to(sandbox)
        except ValueError:
            raise ToolError("path escapes the sandbox: %s" % path)
        return candidate

    def list_files() -> str:
        files = sorted(
            str(p.relative_to(sandbox)) for p in sandbox.rglob("*") if p.is_file()
        )
        return "\n".join(files) if files else "(empty sandbox)"

    def read_file(path: str) -> str:
        target = _resolve(path)
        if not target.is_file():
            raise ToolError("file not found: %s" % path)
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > 4000:
            return text[:4000] + "\n...[truncated at 4000 chars]"
        return text

    def calculator(expression: str) -> str:
        return safe_calculate(expression)

    registry.register(
        "list_files",
        "列出 sandbox 目录下的所有文件（相对路径）。当你不确定有哪些文件可读时，先调用它。",
        {"type": "object", "properties": {}, "required": []},
        list_files,
    )
    registry.register(
        "read_file",
        "读取 sandbox 内某个文本文件的内容。path 必须是相对路径，例如 notes.txt；"
        "超过 4000 字符会被截断。不要用它读取 sandbox 之外的文件。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对 sandbox 的文件路径，例如 notes.txt",
                }
            },
            "required": ["path"],
        },
        read_file,
    )
    registry.register(
        "calculator",
        "计算一个算术表达式，支持 + - * / ** % 和括号。需要精确计算时使用，不要心算。",
        {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的表达式，例如 (23 * 17) + 4",
                }
            },
            "required": ["expression"],
        },
        calculator,
    )
    return registry
