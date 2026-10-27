"""模型适配层：把不同供应商的响应归一化成同一种形状。

Agent 循环只依赖：
    response = {
        "content": str | None,
        "tool_calls": [{"id": str, "name": str, "arguments": dict}],
        "usage": {...}
    }

这样循环逻辑与具体 SDK 解耦，测试时可以用 FakeLLM 完全离线跑。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_dotenv(path: str = ".env") -> None:
    """极简 .env 读取：不覆盖已存在的环境变量，避免把 shell 配置顶掉。"""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class FakeLLM:
    """按脚本返回响应，用于离线 demo 和单元测试。"""

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls = 0

    def complete(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        self.calls += 1
        if not self.script:
            return {"content": "(script exhausted)", "tool_calls": []}
        return self.script.pop(0)


class OpenAICompatibleLLM:
    """任何兼容 OpenAI Chat Completions 的端点：OpenAI、DeepSeek、Qwen、GLM、Ollama…"""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        from openai import OpenAI

        key = api_key or os.getenv("LLM_API_KEY")
        if not key:
            raise RuntimeError(
                "LLM_API_KEY is not set. Copy .env.example to .env and fill it in, "
                "or export LLM_API_KEY before running."
            )
        self.model = model or os.getenv("LLM_MODEL", "deepseek-chat")
        self.client = OpenAI(
            api_key=key,
            base_url=base_url or os.getenv("LLM_BASE_URL") or None,
            timeout=60.0,
            max_retries=2,
        )

    def complete(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
        )
        message = response.choices[0].message
        tool_calls: List[Dict[str, Any]] = []
        for call in message.tool_calls or []:
            raw_args = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = {"_raw": raw_args}
            tool_calls.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": arguments,
                }
            )
        usage = {}
        if response.usage is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return {
            "content": message.content,
            "tool_calls": tool_calls,
            "usage": usage,
        }
