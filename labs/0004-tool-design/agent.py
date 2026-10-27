"""第 4 课 Lab：同一套循环，两种工具设计。

    python3 agent.py --demo --tools baseline
    python3 agent.py --demo --tools refactored
    python3 agent.py "order-service 现在健康吗？" --tools refactored
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from llm import FakeLLM, OpenAICompatibleLLM, load_dotenv
from tool_base import ToolRegistry

SYSTEM_PROMPT = """你是 SRE on-call 助手。你可以使用工具来回答问题。

规则：
1. 先收集证据，再下结论。最终回答要引用工具结果（服务名、日志行、runbook 来源）。
2. 能用一次调用拿到聚合信息时，不要拆成多次；查询日志时注意级别与时间窗口。
3. 重启、回滚、改配置都是写操作：先确认用户意图，并遵守工具要求的审批流程。
4. 工具返回 ERROR 时，解释错误并尝试其他方法，不要编造结果。
5. 最终回答用中文，简洁，说明你依据了哪些证据。
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


def demo_script(toolset: str) -> List[Dict[str, Any]]:
    if toolset == "baseline":
        return [
            {
                "content": "先拿服务配置。",
                "tool_calls": [
                    {"id": "c1", "name": "get_service", "arguments": {"svc": "order-service"}}
                ],
            },
            {
                "content": "再看指标。",
                "tool_calls": [
                    {"id": "c2", "name": "get_metrics", "arguments": {"svc": "order-service"}}
                ],
            },
            {
                "content": "最后看部署。",
                "tool_calls": [
                    {"id": "c3", "name": "get_deploys", "arguments": {"svc": "order-service"}}
                ],
            },
            {
                "content": "order-service 处于 degraded：5xx 18%，p99 2400ms，"
                "连接池 48/50，最近部署 v2.14.1。依据：get_metrics 与 get_deploys。",
                "tool_calls": [],
            },
        ]
    return [
        {
            "content": "用一次聚合健康快照。",
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "get_service_health",
                    "arguments": {"service": "order-service"},
                }
            ],
        },
        {
            "content": "order-service 处于 degraded：5xx 18%，p99 2400ms，连接池 48/50，"
            "最近部署 v2.14.1。依据：get_service_health 的 summary。",
            "tool_calls": [],
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Tool design lab (lesson 0004)")
    parser.add_argument("question", nargs="?", help="要问 Agent 的问题")
    parser.add_argument("--tools", choices=["baseline", "refactored"], default="refactored")
    parser.add_argument("--demo", action="store_true", help="离线演示，不需要 API key")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--trace", help="把完整 trace 写成 JSON 文件")
    args = parser.parse_args()

    load_dotenv()
    if args.tools == "baseline":
        from tools_baseline import build_registry
    else:
        from tools_refactored import build_registry
    registry = build_registry()

    if args.demo:
        question = args.question or "order-service 现在健康吗？"
        llm: Any = FakeLLM(demo_script(args.tools))
    else:
        if not args.question:
            parser.error("请提供问题，或使用 --demo")
        question = args.question
        llm = OpenAICompatibleLLM()

    print("tools: %s" % args.tools)
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
