"""Baseline toolset: seven thin wrappers, terse descriptions, unsafe write.

这是一份刻意“能用但不好用”的工具集，用来做对照实验：
- 每个后端能力都单独暴露，模型必须自己组合；
- 参数名是缩写，描述只有一句话；
- 日志没有级别 / 时间 / 条数过滤；
- 没有 runbook 检索；
- 重启没有审批与幂等。
"""
from __future__ import annotations

import json

from data_access import (
    available_services,
    read_logs,
    restart_raw,
    service_record,
)
from tool_base import ToolRegistry


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def list_services() -> str:
        return "\n".join(available_services())

    def get_service(svc: str) -> str:
        record = service_record(svc)
        return json.dumps(
            {
                "name": svc,
                "port": record["port"],
                "owner": record["owner"],
                "dependencies": record["dependencies"],
            },
            ensure_ascii=False,
        )

    def get_metrics(svc: str) -> str:
        record = service_record(svc)
        return json.dumps(
            {
                "name": svc,
                "status": record["status"],
                "error_rate_5m": record["error_rate_5m"],
                "p99_latency_ms": record["p99_latency_ms"],
                "db_pool_used": record["db_pool"]["used"],
                "db_pool_max": record["db_pool"]["max"],
            },
            ensure_ascii=False,
        )

    def get_deploys(svc: str) -> str:
        record = service_record(svc)
        return json.dumps([record["recent_deploy"]], ensure_ascii=False)

    def list_logs(svc: str) -> str:
        service_record(svc)
        return "%s.log" % svc

    def grep_logs(svc: str, p: str) -> str:
        entries = read_logs(svc)
        needle = (p or "").lower()
        hits = [entry["line"] for entry in entries if needle in entry["line"].lower()]
        if not hits:
            return "no matches"
        return "\n".join(hits)[:8000]

    def restart_service(svc: str) -> str:
        # 没有 approval_id，也没有幂等键：这是 baseline 最危险的地方。
        return json.dumps(restart_raw(svc, "unspecified"), ensure_ascii=False)

    registry.register("list_services", "List services.", {"type": "object", "properties": {}, "required": []}, list_services)
    registry.register(
        "get_service",
        "Get service.",
        {"type": "object", "properties": {"svc": {"type": "string"}}, "required": ["svc"]},
        get_service,
    )
    registry.register(
        "get_metrics",
        "Get metrics.",
        {"type": "object", "properties": {"svc": {"type": "string"}}, "required": ["svc"]},
        get_metrics,
    )
    registry.register(
        "get_deploys",
        "Get deploys.",
        {"type": "object", "properties": {"svc": {"type": "string"}}, "required": ["svc"]},
        get_deploys,
    )
    registry.register(
        "list_logs",
        "List logs.",
        {"type": "object", "properties": {"svc": {"type": "string"}}, "required": ["svc"]},
        list_logs,
    )
    registry.register(
        "grep_logs",
        "Grep logs.",
        {
            "type": "object",
            "properties": {
                "svc": {"type": "string"},
                "p": {"type": "string"},
            },
            "required": ["svc", "p"],
        },
        grep_logs,
    )
    registry.register(
        "restart_service",
        "Restart service.",
        {"type": "object", "properties": {"svc": {"type": "string"}}, "required": ["svc"]},
        restart_service,
    )
    return registry
