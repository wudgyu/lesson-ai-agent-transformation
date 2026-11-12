"""Lesson 0011 CLI: connect to the RepoMedic MCP server and apply host policy.

Examples
--------
    python3 mcp_client.py discover
    python3 mcp_client.py tools --task triage
    python3 mcp_client.py call query_logs --args '{"service":"order-service","start":"2026-11-10T20:00:00Z","end":"2026-11-10T20:10:00Z"}'
    python3 mcp_client.py resource runbook://incidents/2026-10-24-order-pool
    python3 mcp_client.py prompt triage --args '{"service":"order-service","symptom":"连接池耗尽"}'
    python3 mcp_client.py triage

The default transport is stdio: this process launches repomedic_server.py as a
child process. Pass --url http://127.0.0.1:8000/mcp to use Streamable HTTP
after starting the server with `python3 repomedic_server.py --http` (see README).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from mcp_gateway import (
    ApprovalRequired,
    RepoMedicGateway,
    ToolNotAllowed,
    estimate_schema_tokens,
)

HERE = Path(__file__).resolve().parent
SERVER = HERE / "repomedic_server.py"


def target_for(args: argparse.Namespace) -> Any:
    if args.url:
        return args.url
    return StdioServerParameters(command=sys.executable, args=[str(SERVER)])


def parse_json(raw: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("invalid JSON for %s: %s" % (what, exc))
    if not isinstance(value, dict):
        raise SystemExit("%s must be a JSON object" % what)
    return value


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def cmd_discover(gateway: RepoMedicGateway, args: argparse.Namespace) -> int:
    info = await gateway.discover()
    if args.json:
        print_json(info)
    else:
        print("protocol:", info["protocol_version"])
        print("server:", info["server_name"], info["server_version"])
        print("capabilities:", ", ".join(sorted(info["capabilities"])))
        print("instructions:", info["instructions"])
    return 0


async def cmd_tools(gateway: RepoMedicGateway, args: argparse.Namespace) -> int:
    tools = await gateway.list_tools()
    task = None if args.all else args.task
    projected = gateway.project_tools(tools, task=task)
    all_projected = gateway.project_tools(tools, task=None)

    if args.json:
        print_json(
            {
                "discovered": [tool.name for tool in tools],
                "projected": [item["function"]["name"] for item in projected],
                "schema_tokens_all": estimate_schema_tokens(all_projected),
                "schema_tokens_scoped": estimate_schema_tokens(projected),
            }
        )
        return 0

    print("discovered (%d):" % len(tools))
    for tool in tools:
        annotations = tool.annotations
        flags = []
        if annotations:
            if annotations.read_only_hint:
                flags.append("read-only")
            if annotations.destructive_hint:
                flags.append("destructive")
            if annotations.idempotent_hint:
                flags.append("idempotent")
        print("  - %-16s %s" % (tool.name, ", ".join(flags) or "no annotations"))
        print("    %s" % (tool.description or "").split("\n")[0])

    print("\nprojected for task=%s (%d):" % (task or "all", len(projected)))
    for item in projected:
        print("  - %s" % item["function"]["name"])
    print(
        "\nschema tokens: all=%d scoped=%d"
        % (estimate_schema_tokens(all_projected), estimate_schema_tokens(projected))
    )
    return 0


async def cmd_call(gateway: RepoMedicGateway, args: argparse.Namespace) -> int:
    arguments = parse_json(args.args, "--args")
    try:
        outcome = await gateway.call(
            args.tool,
            arguments,
            approval="approved" if args.approve else None,
        )
    except ApprovalRequired as exc:
        print("BLOCKED: %s" % exc)
        print("This is the host policy, not the MCP server. Re-run with --approve after a human confirms.")
        return 2
    except ToolNotAllowed as exc:
        print("BLOCKED: %s" % exc)
        return 2

    print("tool:", outcome.tool)
    print("ok:", outcome.ok)
    print("approved:", outcome.approved)
    if outcome.error:
        print("error:", outcome.error)
    print("\nmodel-facing block:\n")
    print(gateway.render_for_model(outcome))
    return 0 if outcome.ok else 1


async def cmd_resource(gateway: RepoMedicGateway, args: argparse.Namespace) -> int:
    text = await gateway.read_resource(args.uri)
    if args.json:
        print_json({"uri": args.uri, "text": text})
    else:
        print(text)
    return 0


async def cmd_prompt(gateway: RepoMedicGateway, args: argparse.Namespace) -> int:
    arguments = parse_json(args.args, "--args")
    messages = await gateway.get_prompt(args.name, arguments)
    if args.json:
        print_json({"name": args.name, "messages": messages})
    else:
        for message in messages:
            print("[%s] %s" % (message["role"], message["text"]))
    return 0


async def cmd_triage(gateway: RepoMedicGateway, args: argparse.Namespace) -> int:
    """A scripted end-to-end run. No model call; the control flow is the point."""
    info = await gateway.discover()
    tools = await gateway.list_tools()
    projected = gateway.project_tools(tools, task="triage")
    all_projected = gateway.project_tools(tools, task=None)

    print("== 1. discover ==")
    print("protocol=%s server=%s" % (info["protocol_version"], info["server_name"]))
    print("capabilities=%s" % ", ".join(sorted(info["capabilities"])))

    print("\n== 2. project tools for this task ==")
    print("discovered:", ", ".join(tool.name for tool in tools))
    print("exposed to model:", ", ".join(item["function"]["name"] for item in projected))
    print(
        "schema tokens: all=%d scoped=%d"
        % (estimate_schema_tokens(all_projected), estimate_schema_tokens(projected))
    )
    print("restart_service is discovered but not exposed during triage.")

    print("\n== 3. gather evidence (host-invoked; no model) ==")
    health = await gateway.call("check_health", {"service": "order-service"})
    print(gateway.render_for_model(health))

    logs = await gateway.call(
        "query_logs",
        {
            "service": "order-service",
            "start": "2026-11-10T20:00:00Z",
            "end": "2026-11-10T20:10:00Z",
            "keyword": "HikariPool",
            "limit": 3,
        },
    )
    print(gateway.render_for_model(logs))

    runbooks = await gateway.call("search_runbook", {"query": "连接池"})
    print(gateway.render_for_model(runbooks))

    if runbooks.structured and runbooks.structured.get("hits"):
        uri = runbooks.structured["hits"][0]["uri"]
        print("\n== 4. read the full runbook resource ==")
        print("uri:", uri)
        print(await gateway.read_resource(uri))

    print("\n== 5. draft (scripted, not a model) ==")
    print(
        "根因假设：findRecentOrders 的 N+1 拖长连接占用，连接池 48/50、30s 超时。\n"
        "证据：query_logs 的 HikariPool timeout；check_health 的 pool/error-rate；"
        "runbook 的 2026-10-24 复盘。\n"
        "缓解：先由 on-call 审批重启，再发布批量查询修复；不要先调大连接池上限。\n"
        "仍缺：findRecentOrders 的调用量与发布变更记录。"
    )
    print("\n== 6. audit ==")
    for item in gateway.audit:
        print(item)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RepoMedic MCP client (Lesson 0011)")
    parser.add_argument("--url", help="Streamable HTTP MCP endpoint; default is stdio")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="call server/discover through the client")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("tools", help="list discovered tools and the task-scoped projection")
    p.add_argument("--task", default="triage", choices=["triage", "code", "restart"])
    p.add_argument("--all", action="store_true", help="project every discovered tool")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("call", help="call one tool through the host policy")
    p.add_argument("tool")
    p.add_argument("--args", default="{}", help="JSON object of arguments")
    p.add_argument("--approve", action="store_true", help="record explicit human approval")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("resource", help="read a resource URI")
    p.add_argument("uri")
    p.set_defaults(func=cmd_resource)

    p = sub.add_parser("prompt", help="render a prompt template")
    p.add_argument("name")
    p.add_argument("--args", default="{}", help="JSON object of prompt arguments")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("triage", help="scripted end-to-end triage flow")
    p.set_defaults(func=cmd_triage)
    return parser


async def run(args: argparse.Namespace) -> int:
    async with Client(target_for(args)) as client:
        gateway = RepoMedicGateway(client)
        return await args.func(gateway, args)


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
