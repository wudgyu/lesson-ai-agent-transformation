"""Client-side policy for a RepoMedic MCP connection.

MCP standardizes discovery and transport. It deliberately does not decide:

  - which servers you trust;
  - which discovered tools you expose to the model;
  - which calls require human approval;
  - how tool output is framed for the model.

Those are host responsibilities. This module makes them explicit so the lab
can test them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from mcp import Client


# A task-scoped allowlist is the cheapest defense against a server that
# exposes forty tools when the current task needs three. In a real host this
# would be a router/model decision plus a hard policy check.
TASK_TOOLS: dict[str, list[str]] = {
    "triage": ["query_logs", "check_health", "search_runbook"],
    "code": ["search_runbook"],
    "restart": ["restart_service"],
}

WRITE_TOOLS = {"restart_service"}


class ToolNotAllowed(PermissionError):
    """The host policy refuses to expose or call this tool."""


class ApprovalRequired(PermissionError):
    """A destructive tool was called without an explicit human approval."""


@dataclass
class CallOutcome:
    tool: str
    ok: bool
    text: str = ""
    structured: dict[str, Any] | None = None
    error: str | None = None
    approved: bool = False


@dataclass
class RepoMedicGateway:
    """A policy layer between one MCP client and the model."""

    client: Client
    allowed_tools: set[str] | None = None
    approval_required: set[str] = field(default_factory=lambda: set(WRITE_TOOLS))
    max_output_chars: int = 4000
    audit: list[dict[str, Any]] = field(default_factory=list)

    async def discover(self) -> dict[str, Any]:
        """Read what the server advertises. Discovery is not trust.

        The high-level Client negotiates on `async with` entry (server/discover
        on 2026-07-28, or initialize on legacy servers) and exposes the result
        here; there is no need to hand-roll the RPC for this lab.
        """
        info = self.client.server_info
        capabilities = self.client.server_capabilities
        return {
            "protocol_version": self.client.protocol_version,
            "server_name": getattr(info, "name", None),
            "server_version": getattr(info, "version", None),
            "instructions": self.client.instructions,
            "capabilities": capabilities.model_dump(exclude_none=True)
            if capabilities is not None
            else {},
        }

    async def list_tools(self) -> list[Any]:
        result = await self.client.list_tools()
        return list(result.tools)

    def project_tools(self, tools: Sequence[Any], task: str | None = None) -> list[dict[str, Any]]:
        """Turn MCP tool definitions into model-facing function schemas.

        `task=None` means "all discovered tools"; a known task selects its
        allowlist; an unknown task falls back to read-only tools only.
        """
        if task in TASK_TOOLS:
            names = set(TASK_TOOLS[task])
        elif task is None:
            names = {tool.name for tool in tools}
        else:
            names = {
                tool.name
                for tool in tools
                if tool.annotations is not None and tool.annotations.read_only_hint
            }

        projected: list[dict[str, Any]] = []
        for tool in tools:
            if tool.name not in names:
                continue
            if self.allowed_tools is not None and tool.name not in self.allowed_tools:
                continue
            projected.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.input_schema,
                    },
                }
            )
        return projected

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        approval: str | None = None,
    ) -> CallOutcome:
        """Call a tool after applying host policy.

        Raises ToolNotAllowed / ApprovalRequired *before* hitting the server.
        Tool execution errors come back as `ok=False`, not exceptions.
        """
        if self.allowed_tools is not None and name not in self.allowed_tools:
            self.audit.append({"tool": name, "ok": False, "blocked": "not_allowed"})
            raise ToolNotAllowed("tool not in host allowlist: %s" % name)

        if name in self.approval_required and approval != "approved":
            self.audit.append({"tool": name, "ok": False, "blocked": "approval_required"})
            raise ApprovalRequired(
                "tool %s is destructive; pass approval='approved' after a human confirms" % name
            )

        result = await self.client.call_tool(name, arguments)
        text = "\n".join(
            getattr(block, "text", "")
            for block in result.content
            if getattr(block, "text", "")
        )
        outcome = CallOutcome(
            tool=name,
            ok=not result.is_error,
            text=text,
            structured=result.structured_content,
            error=text if result.is_error else None,
            approved=approval == "approved",
        )
        self.audit.append(
            {
                "tool": name,
                "ok": outcome.ok,
                "approved": outcome.approved,
                "blocked": None,
            }
        )
        return outcome

    def render_for_model(self, outcome: CallOutcome) -> str:
        """Frame tool output as untrusted data, not instructions.

        This is provenance marking, not sanitization. A prompt injection can
        still live inside the block; the host must also constrain what the
        model can do with it.
        """
        if outcome.structured is not None:
            body = json.dumps(outcome.structured, ensure_ascii=False, indent=2)
        else:
            body = outcome.text

        if len(body) > self.max_output_chars:
            body = body[: self.max_output_chars] + "\n…[truncated by host]"

        return (
            '<untrusted-tool-output source="repomedic" tool="%s">\n'
            "%s\n"
            "</untrusted-tool-output>"
        ) % (outcome.tool, body)

    async def read_resource(self, uri: str) -> str:
        result = await self.client.read_resource(uri)
        return "\n".join(
            getattr(content, "text", "")
            for content in result.contents
            if getattr(content, "text", "")
        )

    async def get_prompt(self, name: str, arguments: dict[str, str]) -> list[dict[str, str]]:
        result = await self.client.get_prompt(name, arguments)
        return [
            {"role": message.role, "text": getattr(message.content, "text", "")}
            for message in result.messages
        ]


def estimate_schema_tokens(projected: Sequence[dict[str, Any]]) -> int:
    """Cheap deterministic proxy for the cost of putting schemas in context.

    The course consistently uses `chars // 2`; real tokenizers differ. Use the
    ratio, not the absolute number.
    """
    return max(1, len(json.dumps(projected, ensure_ascii=False)) // 2)
