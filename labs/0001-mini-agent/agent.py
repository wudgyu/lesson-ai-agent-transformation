"""第 1 课：150 行以内的 Agent 循环。

运行：
    python3 agent.py --demo          # 不需要 API key，使用脚本化的 FakeLLM
    python3 agent.py "问题..."        # 需要 .env 中的 LLM_API_KEY

这个文件刻意不依赖 LangGraph / OpenAI Agents SDK。第 2 周你会用框架重建它，
那时你应该能一眼看出框架替你做了什么。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm import FakeLLM, OpenAICompatibleLLM, load_dotenv
from tools import ToolRegistry, build_registry

SYSTEM_PROMPT = """你是一个谨慎的助手。你可以使用工具来回答问题。

规则：
1. 需要外部信息或精确计算时，先调用工具，不要猜。
2. 每次只调用必要的工具；拿到观察结果后，再决定下一步。
3. 工具返回 ERROR 时，解释错误并尝试其他方法，不要假装成功。
4. 最终回答用中文，简洁，并说明你依据了哪些工具结果。
"""


class AgentLoopError(RuntimeError):
    """循环没有在预算内结束。"""


def run_agent(
    question: str,
    llm: Any,
    registry: ToolRegistry,
    max_steps: int = 8,
    verbose: bool = True,
) -> Dict[str, Any]:
    """最小的 ReAct 循环：模型决策 → 执行工具 → 观察 → 再决策。"""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace: List[Dict[str, Any]] = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    for step in range(1, max_steps + 1):
        response = llm.complete(messages, registry.schemas())
        content = response.get("content")
        tool_calls = response.get("tool_calls") or []

        for key, value in (response.get("usage") or {}).items():
            if key in usage_totals:
                usage_totals[key] += int(value or 0)

        if verbose:
            kind = "tool_calls" if tool_calls else "final"
            print("\n[step %d] model → %s" % (step, kind))
            if content:
                print("  thought: %s" % content)

        # 把 assistant 的这一步写回历史。OpenAI-compatible API 要求
        # tool_calls 与随后的 tool 消息通过 id 一一对应。
        assistant_message: Dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ]
        messages.append(assistant_message)

        trace.append(
            {
                "step": step,
                "type": "model",
                "content": content,
                "tool_calls": tool_calls,
            }
        )

        if not tool_calls:
            return {
                "answer": content or "",
                "messages": messages,
                "trace": trace,
                "usage": usage_totals,
            }

        for call in tool_calls:
            result = registry.execute(call["name"], call["arguments"])
            if verbose:
                print(
                    "  action: %s(%s)"
                    % (call["name"], json.dumps(call["arguments"], ensure_ascii=False))
                )
                print("  observation: %s" % result.replace("\n", " | ")[:300])
            trace.append(
                {
                    "step": step,
                    "type": "tool",
                    "name": call["name"],
                    "arguments": call["arguments"],
                    "result": result,
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )

    raise AgentLoopError(
        "agent did not finish within %d steps. Inspect the trace and raise the "
        "budget or fix the prompt/tools." % max_steps
    )


def demo_script() -> List[Dict[str, Any]]:
    """离线演示：两次工具调用，然后给出最终答案。"""
    return [
        {
            "content": "我先看看 sandbox 里有哪些文件。",
            "tool_calls": [{"id": "call_1", "name": "list_files", "arguments": {}}],
        },
        {
            "content": "找到了 notes.txt，我读取它的内容。",
            "tool_calls": [
                {
                    "id": "call_2",
                    "name": "read_file",
                    "arguments": {"path": "notes.txt"},
                }
            ],
        },
        {
            "content": "notes.txt 记录的部署端口是 8081。依据：list_files 确认了文件存在，"
            "read_file 返回的内容中写明“端口: 8081”。",
            "tool_calls": [],
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal ReAct agent (lesson 0001)")
    parser.add_argument("question", nargs="?", help="要问 Agent 的问题")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="使用脚本化的 FakeLLM，不需要 API key",
    )
    parser.add_argument("--max-steps", type=int, default=8, help="循环步数上限")
    parser.add_argument("--trace", help="把完整 trace 写成 JSON 文件")
    args = parser.parse_args()

    load_dotenv()
    sandbox = Path(__file__).resolve().parent / "sandbox"
    registry = build_registry(sandbox)

    if args.demo:
        question = args.question or (
            "sandbox/notes.txt 里记录的部署端口是多少？先确认文件，再读取内容。"
        )
        llm: Any = FakeLLM(demo_script())
    else:
        if not args.question:
            parser.error("请提供问题，或使用 --demo 运行离线演示")
        question = args.question
        llm = OpenAICompatibleLLM()

    print("question: %s" % question)
    try:
        result = run_agent(question, llm, registry, max_steps=args.max_steps)
    except AgentLoopError as exc:
        print("\n[loop stopped] %s" % exc)
        raise SystemExit(1)

    print("\n=== final answer ===")
    print(result["answer"])
    if any(result["usage"].values()):
        print(
            "\nusage: prompt=%d completion=%d total=%d"
            % (
                result["usage"]["prompt_tokens"],
                result["usage"]["completion_tokens"],
                result["usage"]["total_tokens"],
            )
        )
    if args.trace:
        Path(args.trace).write_text(
            json.dumps(result["trace"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("trace written to %s" % args.trace)


if __name__ == "__main__":
    main()
