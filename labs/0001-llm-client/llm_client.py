"""A small OpenAI-compatible LLM client for lesson 0001.

The client is deliberately thin:

- streaming text deltas + a final usage record
- non-streaming completion with normalized tool calls
- a FakeStreamingLLM so the whole lab runs offline

The point is to make the protocol visible, not to hide it behind a framework.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence


def load_dotenv(path: str = ".env") -> None:
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


def estimate_tokens(text: str) -> int:
    """Rough budget estimate for mixed Chinese/English text."""
    if not text:
        return 0
    return max(1, len(text) // 3)


@dataclass
class LLMConfig:
    api_key: str
    base_url: Optional[str] = None
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: Optional[int] = None
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        load_dotenv()
        key = os.getenv("LLM_API_KEY", "")
        if not key:
            raise RuntimeError(
                "LLM_API_KEY is not set. Copy .env.example to .env, "
                "or run the offline --demo."
            )
        return cls(
            api_key=key,
            base_url=os.getenv("LLM_BASE_URL") or None,
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        )


def build_messages(system: str, user: str) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


class OpenAICompatibleClient:
    """Any endpoint that implements OpenAI Chat Completions."""

    def __init__(self, config: LLMConfig) -> None:
        from openai import OpenAI

        self.config = config
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=2,
        )

    def stream_chat(
        self,
        messages: Sequence[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        include_usage: bool = True,
    ) -> Iterator[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "stream": True,
            "temperature": (
                self.config.temperature if temperature is None else temperature
            ),
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if include_usage:
            kwargs["stream_options"] = {"include_usage": True}

        stream = self.client.chat.completions.create(**kwargs)
        parts: List[str] = []
        usage: Dict[str, int] = {}
        finish_reason: Optional[str] = None

        for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                usage = {
                    "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
                }
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            content = getattr(delta, "content", None) or ""
            if content:
                parts.append(content)
                yield {"type": "delta", "content": content}

        yield {
            "type": "done",
            "content": "".join(parts),
            "finish_reason": finish_reason,
            "usage": usage,
        }

    def complete(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": (
                self.config.temperature if temperature is None else temperature
            ),
        }
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        response = self.client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        tool_calls = []
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
            "content": message.content or "",
            "tool_calls": tool_calls,
            "finish_reason": response.choices[0].finish_reason,
            "usage": usage,
        }


class FakeStreamingLLM:
    """Scripted stream for offline demos and tests."""

    def __init__(
        self,
        chunks: Sequence[str],
        usage: Optional[Dict[str, int]] = None,
        complete_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.chunks = list(chunks)
        self.usage = dict(usage or {})
        self.complete_response = complete_response or {
            "content": "{}",
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": dict(self.usage),
        }

    def stream_chat(self, messages, **kwargs) -> Iterator[Dict[str, Any]]:
        parts = []
        for chunk in self.chunks:
            parts.append(chunk)
            yield {"type": "delta", "content": chunk}
        yield {
            "type": "done",
            "content": "".join(parts),
            "finish_reason": "stop",
            "usage": dict(self.usage),
        }

    def complete(self, messages, **kwargs) -> Dict[str, Any]:
        return dict(self.complete_response)


def main() -> None:
    parser = argparse.ArgumentParser(description="Streaming LLM client (lesson 0001)")
    parser.add_argument("--demo", action="store_true", help="offline fake stream")
    parser.add_argument("--live", action="store_true", help="use the real API")
    parser.add_argument("--prompt", default="用一句话解释 token 预算。")
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--system", default="你是一个简洁的工程助教。")
    args = parser.parse_args()

    messages = build_messages(args.system, args.prompt)
    if args.demo or not args.live:
        client = FakeStreamingLLM(
            ["Token ", "预算 ", "决定 ", "了一次 ", "调用能看多少上下文。"],
            usage={"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21},
        )
        print("[demo] fake stream")
    else:
        config = LLMConfig.from_env()
        if args.model:
            config.model = args.model
        config.temperature = args.temperature
        client = OpenAICompatibleClient(config)
        print("[live] model=%s" % config.model)

    full = ""
    for event in client.stream_chat(messages):
        if event["type"] == "delta":
            print(event["content"], end="", flush=True)
            full += event["content"]
        elif event["type"] == "done":
            print()
            print(
                "usage: %s · estimated completion tokens: %d"
                % (event["usage"], estimate_tokens(full))
            )


if __name__ == "__main__":
    main()
