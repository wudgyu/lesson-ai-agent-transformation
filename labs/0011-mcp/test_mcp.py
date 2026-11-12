"""Offline tests for the RepoMedic MCP lab.

Run:
    python3 -m unittest test_mcp -v

All tests use the in-memory transport except `test_stdio_transport`, which
launches the server as a real subprocess and speaks JSON-RPC over stdin/stdout.
No API key and no network are needed.

Each test owns one `asyncio.run(...)` call. The MCP client opens anyio task
groups, so keeping connect/use/close inside one task avoids cross-task cancel
scope errors.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

from mcp import Client, StdioServerParameters

from mcp_gateway import (
    ApprovalRequired,
    RepoMedicGateway,
    ToolNotAllowed,
    estimate_schema_tokens,
)
from repomedic_server import RESTART_REQUESTS, mcp

HERE = Path(__file__).resolve().parent
SERVER = HERE / "repomedic_server.py"

ARGS = {
    "service": "order-service",
    "start": "2026-11-10T20:00:00Z",
    "end": "2026-11-10T20:10:00Z",
}


def result_text(result) -> str:
    return "\n".join(
        getattr(block, "text", "")
        for block in result.content
        if getattr(block, "text", "")
    )


class MCPTestCase(unittest.TestCase):
    def setUp(self) -> None:
        RESTART_REQUESTS.clear()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_discover_reports_protocol_and_capabilities(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                info = await gateway.discover()
                self.assertEqual(info["protocol_version"], "2026-07-28")
                self.assertEqual(info["server_name"], "repomedic")
                self.assertIn("tools", info["capabilities"])
                self.assertIn("resources", info["capabilities"])
                self.assertIn("prompts", info["capabilities"])
                self.assertIn("restart_service", info["instructions"])

        self.run_async(scenario())

    def test_tools_are_deterministic_and_annotated(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                first = [tool.name for tool in await gateway.list_tools()]
                second = [tool.name for tool in await gateway.list_tools()]
                self.assertEqual(first, second)
                self.assertEqual(
                    first,
                    ["query_logs", "check_health", "search_runbook", "restart_service"],
                )
                tools = {tool.name: tool for tool in await gateway.list_tools()}
                self.assertTrue(tools["query_logs"].annotations.read_only_hint)
                self.assertTrue(tools["restart_service"].annotations.destructive_hint)
                self.assertTrue(tools["restart_service"].annotations.idempotent_hint)
                self.assertIsNotNone(tools["query_logs"].input_schema)
                self.assertIsNotNone(tools["query_logs"].output_schema)

        self.run_async(scenario())

    def test_query_logs_returns_structured_evidence(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                result = await client.call_tool("query_logs", ARGS)
                self.assertFalse(result.is_error)
                structured = result.structured_content
                self.assertEqual(structured["service"], "order-service")
                self.assertEqual(len(structured["events"]), 3)
                self.assertEqual(structured["total_matched"], 6)
                self.assertTrue(structured["truncated"])
                self.assertIn("HikariPool", structured["events"][0]["message"])

        self.run_async(scenario())

    def test_argument_validation_is_a_tool_error(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                result = await client.call_tool("query_logs", {**ARGS, "limit": 99})
                self.assertTrue(result.is_error)
                self.assertIn("less than or equal to 10", result_text(result))

        self.run_async(scenario())

    def test_unknown_service_returns_actionable_error(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                result = await client.call_tool(
                    "query_logs", {**ARGS, "service": "nope"}
                )
                self.assertTrue(result.is_error)
                self.assertIn("unknown service: nope", result_text(result))

        self.run_async(scenario())

    def test_unknown_tool_is_an_error_not_an_exception(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                result = await client.call_tool("does_not_exist", {})
                self.assertTrue(result.is_error)
                self.assertIn("Unknown tool", result_text(result))

        self.run_async(scenario())

    def test_read_runbook_resource(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                listed = await client.list_resources()
                uris = {resource.uri for resource in listed.resources}
                self.assertIn("runbook://incidents/2026-10-24-order-pool", uris)
                text = await gateway.read_resource(
                    "runbook://incidents/2026-10-24-order-pool"
                )
                self.assertIn("N+1", text)
                self.assertIn("重启", text)

        self.run_async(scenario())

    def test_resource_template_reads_source(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                templates = await client.list_resource_templates()
                self.assertIn(
                    "repo://{path}",
                    {template.uri_template for template in templates.resource_templates},
                )
                text = await gateway.read_resource("repo://OrderQueryService.java")
                self.assertIn("line 142", text)
                self.assertIn("findRecentOrders", text)

        self.run_async(scenario())

    def test_missing_resource_is_a_protocol_error(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                with self.assertRaises(Exception) as caught:
                    await client.read_resource("runbook://incidents/does-not-exist")
                self.assertIn("Unknown resource", str(caught.exception))

        self.run_async(scenario())

    def test_prompt_renders_with_arguments(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                listed = await client.list_prompts()
                self.assertIn("triage", {prompt.name for prompt in listed.prompts})
                messages = await gateway.get_prompt(
                    "triage", {"service": "order-service", "symptom": "连接池耗尽"}
                )
                self.assertEqual(messages[0]["role"], "user")
                self.assertIn("order-service", messages[0]["text"])
                self.assertIn("连接池耗尽", messages[0]["text"])
                self.assertIn("restart_service", messages[0]["text"])

        self.run_async(scenario())

    def test_stdio_transport_lists_tools(self) -> None:
        async def scenario():
            params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
            async with Client(params) as client:
                tools = await client.list_tools()
                self.assertIn("query_logs", {tool.name for tool in tools.tools})
                result = await client.call_tool("query_logs", ARGS)
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["total_matched"], 6)

        self.run_async(scenario())

    def test_gateway_blocks_unapproved_destructive_tool(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                with self.assertRaises(ApprovalRequired):
                    await gateway.call(
                        "restart_service",
                        {"service": "order-service", "idempotency_key": "incident-42"},
                    )
                self.assertEqual(gateway.audit[-1]["blocked"], "approval_required")
                self.assertEqual(RESTART_REQUESTS, [])

        self.run_async(scenario())

    def test_approved_write_reaches_server_once(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                arguments = {
                    "service": "order-service",
                    "idempotency_key": "incident-42",
                }
                first = await gateway.call(
                    "restart_service", arguments, approval="approved"
                )
                second = await gateway.call(
                    "restart_service", arguments, approval="approved"
                )
                self.assertTrue(first.ok)
                self.assertEqual(first.structured["duplicate"], False)
                self.assertEqual(second.structured["duplicate"], True)
                self.assertEqual(len(RESTART_REQUESTS), 1)

        self.run_async(scenario())

    def test_gateway_allowlist_blocks_unlisted_tool(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client, allowed_tools={"query_logs"})
                with self.assertRaises(ToolNotAllowed):
                    await gateway.call("check_health", {"service": "order-service"})
                self.assertEqual(gateway.audit[-1]["blocked"], "not_allowed")

        self.run_async(scenario())

    def test_projection_is_smaller_and_has_openai_shape(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                tools = await gateway.list_tools()
                all_projected = gateway.project_tools(tools, task=None)
                triage_projected = gateway.project_tools(tools, task="triage")

                self.assertEqual(len(all_projected), 4)
                self.assertEqual(
                    [item["function"]["name"] for item in triage_projected],
                    ["query_logs", "check_health", "search_runbook"],
                )
                self.assertLess(
                    estimate_schema_tokens(triage_projected),
                    estimate_schema_tokens(all_projected),
                )

                item = triage_projected[0]
                self.assertEqual(item["type"], "function")
                self.assertEqual(item["function"]["name"], "query_logs")
                self.assertTrue(item["function"]["description"])
                source = next(tool for tool in tools if tool.name == "query_logs")
                self.assertEqual(item["function"]["parameters"], source.input_schema)

        self.run_async(scenario())

    def test_tool_output_is_framed_as_untrusted_and_truncated(self) -> None:
        async def scenario():
            async with Client(mcp, raise_exceptions=True) as client:
                gateway = RepoMedicGateway(client)
                outcome = await gateway.call("query_logs", ARGS)
                rendered = gateway.render_for_model(outcome)
                self.assertTrue(rendered.startswith("<untrusted-tool-output"))
                self.assertIn('tool="query_logs"', rendered)
                self.assertIn("HikariPool", rendered)

                gateway.max_output_chars = 40
                truncated = gateway.render_for_model(outcome)
                self.assertIn("truncated by host", truncated)

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
