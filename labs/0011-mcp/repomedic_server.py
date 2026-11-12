"""Lesson 0011: a RepoMedic MCP server.

The server exposes the same evidence domains used in Lesson 0010, but this
time over the Model Context Protocol instead of as Python functions wired
directly into an agent:

  tools       query_logs, check_health, search_runbook, restart_service
  resources   runbook://incidents/... and repo://{path}
  prompt      triage

Everything is deterministic and local: no API key, no network, no real
service is restarted. The point is the protocol boundary and the client
policy around it, not the fixtures.

Run directly for the stdio transport:

    python3 repomedic_server.py

The process speaks JSON-RPC on stdout; diagnostics go to stderr.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------- fixtures

SERVICES: dict[str, dict] = {
    "order-service": {
        "pool_active": 48,
        "pool_max": 50,
        "error_rate_5m": 0.8,
        "p95_ms": 2400,
        "restarts": 0,
    },
    "payment-service": {
        "pool_active": 12,
        "pool_max": 30,
        "error_rate_5m": 0.1,
        "p95_ms": 380,
        "restarts": 0,
    },
}

# One flat log store keeps the fixture easy to reason about. Real servers
# would query a log backend; the MCP surface is the same either way.
LOGS: list[dict[str, str]] = [
    {
        "service": "order-service",
        "timestamp": "2026-11-10T20:05:12Z",
        "level": "WARN",
        "message": (
            "HikariPool-1 - Connection is not available, request timed out "
            "after 30000ms (total=50, active=48, idle=2, waiting=7)"
        ),
    },
    {
        "service": "order-service",
        "timestamp": "2026-11-10T20:05:40Z",
        "level": "WARN",
        "message": "HikariPool-1 - pool=48/50 error_rate_5m=0.8% p95=2.4s",
    },
    {
        "service": "order-service",
        "timestamp": "2026-11-10T20:06:02Z",
        "level": "WARN",
        "message": "OrderQueryService.findRecentOrders took 1840ms rows=1200",
    },
    {
        "service": "order-service",
        "timestamp": "2026-11-10T20:06:20Z",
        "level": "ERROR",
        "message": "GET /orders/recent failed: 500 SQLTimeoutException after 30s",
    },
    {
        "service": "order-service",
        "timestamp": "2026-11-10T20:07:11Z",
        "level": "WARN",
        "message": (
            "HikariPool-1 - Connection is not available, request timed out "
            "after 30000ms (total=50, active=48, idle=2, waiting=7)"
        ),
    },
    {
        "service": "order-service",
        "timestamp": "2026-11-10T20:08:03Z",
        "level": "INFO",
        "message": "OrderQueryService.findRecentOrders rows=1200 elapsed=1790ms",
    },
    {
        "service": "payment-service",
        "timestamp": "2026-11-10T20:05:30Z",
        "level": "INFO",
        "message": "payment pool=12/30 p95=380ms",
    },
]

RUNBOOKS: list[dict] = [
    {
        "id": "2026-10-24-order-pool",
        "title": "order-service 连接池耗尽（N+1）",
        "snippet": "先按 on-call 审批重启缓解，再修 findRecentOrders 的 N+1；不要先调大连接池上限。",
        "uri": "runbook://incidents/2026-10-24-order-pool",
        "keywords": ["连接池", "pool", "n+1", "order-service", "重启"],
    },
    {
        "id": "2026-09-18-pool-limit",
        "title": "连接池上限过低复盘",
        "snippet": "上限 50 在流量峰值下不足，但先修 N+1 后未复现；调上限只能作为临时缓解。",
        "uri": "runbook://incidents/2026-09-18-pool-limit",
        "keywords": ["连接池", "pool", "上限", "容量"],
    },
]

RUNBOOK_TEXT: dict[str, str] = {
    "2026-10-24-order-pool": """# order-service 连接池耗尽（2026-10-24）

## 症状
- HikariPool 获取连接 30s 超时，pool 48/50，等待队列 7。
- 错误率 5 分钟 0.8%，p95 2.4s。

## 根因
`OrderQueryService.findRecentOrders` 在循环内逐行查询 order items，并对每行开启事务，
形成 N+1，连接占用时间被放大。

## 处置
1. 先由 on-call 审批后重启，缓解连接池耗尽（重启是临时缓解）。
2. 再发布修复：批量查询 order items，去掉逐行事务。
3. 不要先调大连接池上限；上限只是把问题推迟。

## 证据
- `OrderQueryService.java:142` 的 per-row query。
- `OrderQueryService.java:150` 的 per-row transaction。
""",
    "2026-09-18-pool-limit": """# 连接池上限过低复盘（2026-09-18）

## 结论
上限 50 在流量峰值下不足，但修掉 N+1 后未再复现。调大上限只能作为临时缓解，
不能替代查询修复。

## 处置
先修 N+1，再根据压测结果评估上限。
""",
}

SOURCE_FILES: dict[str, str] = {
    "OrderQueryService.java": """// OrderQueryService.java (excerpt)
public List<OrderView> findRecentOrders(int limit) {
    List<Order> orders = orderRepository.findRecent(limit);
    for (Order order : orders) {
        // line 142: per-row query -> N+1
        List<Item> items = itemRepository.findByOrderId(order.getId());
        order.setItems(items);
        // line 150: per-row transaction
        auditRepository.save(new AuditEvent(order.getId()));
    }
    return orders.stream().map(OrderView::from).toList();
}
""",
    "HikariConfig.java": """// HikariConfig.java (excerpt)
HikariConfig config = new HikariConfig();
config.setMaximumPoolSize(50);
config.setConnectionTimeout(30_000);
config.setLeakDetectionThreshold(60_000);
""",
}

# A real server would persist this; the lab keeps it in memory so tests can
# assert that a write reached the server exactly once.
RESTART_REQUESTS: list[dict[str, str]] = []

# ------------------------------------------------------------------ models


class LogEvent(BaseModel):
    timestamp: str
    level: str
    message: str


class LogQueryResult(BaseModel):
    service: str
    window: str
    events: list[LogEvent]
    total_matched: int
    truncated: bool


class HealthResult(BaseModel):
    service: str
    pool_active: int
    pool_max: int
    error_rate_5m: float
    p95_ms: int
    restarts: int


class RunbookHit(BaseModel):
    id: str
    title: str
    snippet: str
    uri: str


class RunbookSearchResult(BaseModel):
    query: str
    hits: list[RunbookHit]
    total_matched: int


class RestartResult(BaseModel):
    accepted: bool
    service: str
    idempotency_key: str
    duplicate: bool
    note: str


# ------------------------------------------------------------------ server

mcp = MCPServer(
    "repomedic",
    title="RepoMedic",
    version="0.1.0",
    instructions=(
        "RepoMedic exposes read-only incident evidence and one destructive "
        "remediation tool. Start with logs and health, then search runbooks. "
        "Never call restart_service during diagnosis; it requires human "
        "approval in the host."
    ),
)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def query_logs(
    service: Annotated[str, Field(description="Service name, e.g. order-service.")],
    start: Annotated[str, Field(description="Inclusive ISO-8601 start, e.g. 2026-11-10T20:00:00Z.")],
    end: Annotated[str, Field(description="Inclusive ISO-8601 end, e.g. 2026-11-10T20:10:00Z.")],
    keyword: Annotated[str, Field(description="Case-insensitive substring filter; empty means all.")] = "",
    limit: Annotated[int, Field(ge=1, le=10, description="Maximum events to return.")] = 3,
) -> LogQueryResult:
    """Query structured application logs for one service in a time window.

    Returns at most `limit` events plus `total_matched` and `truncated`, so the
    caller can tell whether it is looking at a sample. Read-only and cheap.
    """
    if service not in SERVICES:
        raise ToolError("unknown service: %s (known: %s)" % (service, ", ".join(sorted(SERVICES))))
    if start > end:
        raise ToolError("start must be <= end")

    matched = [
        event
        for event in LOGS
        if event["service"] == service
        and start <= event["timestamp"] <= end
        and (not keyword or keyword.lower() in event["message"].lower())
    ]
    events = [
        LogEvent(timestamp=event["timestamp"], level=event["level"], message=event["message"])
        for event in matched[:limit]
    ]
    return LogQueryResult(
        service=service,
        window="%s/%s" % (start, end),
        events=events,
        total_matched=len(matched),
        truncated=len(matched) > limit,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def check_health(
    service: Annotated[str, Field(description="Service name, e.g. order-service.")],
) -> HealthResult:
    """Read current pool, error-rate, and latency metrics for one service.

    This is the cheapest way to confirm whether a symptom is still active.
    """
    if service not in SERVICES:
        raise ToolError("unknown service: %s" % service)
    metrics = SERVICES[service]
    return HealthResult(service=service, **metrics)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def search_runbook(
    query: Annotated[str, Field(description="Symptom or keyword, e.g. 连接池 or N+1.")],
) -> RunbookSearchResult:
    """Search incident runbooks by symptom or keyword.

    Returns incident IDs, snippets, and resource URIs. Use resources/read on a
    returned URI to fetch the full runbook; search results are only an index.
    """
    needle = query.strip().lower()
    if not needle:
        raise ToolError("query must not be empty")
    hits = [
        RunbookHit(id=item["id"], title=item["title"], snippet=item["snippet"], uri=item["uri"])
        for item in RUNBOOKS
        if needle in item["title"].lower()
        or needle in item["snippet"].lower()
        or any(needle in keyword.lower() for keyword in item["keywords"])
    ]
    return RunbookSearchResult(query=query, hits=hits, total_matched=len(hits))


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def restart_service(
    service: Annotated[str, Field(description="Service name to restart.")],
    idempotency_key: Annotated[str, Field(description="Stable key for this remediation attempt.")],
    reason: Annotated[str, Field(description="Why a restart is needed; recorded for audit.")] = "",
) -> RestartResult:
    """Schedule a service restart. DESTRUCTIVE and approval-gated.

    The host must obtain human approval before calling this tool. Repeating
    the same idempotency_key is safe: the server records the first request and
    returns `duplicate=true` instead of scheduling a second restart.
    """
    if service not in SERVICES:
        raise ToolError("unknown service: %s" % service)
    if not idempotency_key.strip():
        raise ToolError("idempotency_key must not be empty")

    existing = next(
        (item for item in RESTART_REQUESTS if item["idempotency_key"] == idempotency_key),
        None,
    )
    if existing is not None:
        return RestartResult(
            accepted=True,
            service=service,
            idempotency_key=idempotency_key,
            duplicate=True,
            note="idempotent replay; no second restart scheduled",
        )

    RESTART_REQUESTS.append(
        {
            "service": service,
            "idempotency_key": idempotency_key,
            "reason": reason,
        }
    )
    return RestartResult(
        accepted=True,
        service=service,
        idempotency_key=idempotency_key,
        duplicate=False,
        note="simulated: no process was actually restarted",
    )


@mcp.resource(
    "runbook://incidents/2026-10-24-order-pool",
    name="order-pool-2026-10-24",
    title="order-service 连接池耗尽复盘",
    mime_type="text/markdown",
)
def order_pool_runbook() -> str:
    """Full runbook for the 2026-10-24 order-service pool exhaustion."""
    return RUNBOOK_TEXT["2026-10-24-order-pool"]


@mcp.resource(
    "runbook://incidents/2026-09-18-pool-limit",
    name="pool-limit-2026-09-18",
    title="连接池上限过低复盘",
    mime_type="text/markdown",
)
def pool_limit_runbook() -> str:
    """Full runbook for the 2026-09-18 pool-limit incident."""
    return RUNBOOK_TEXT["2026-09-18-pool-limit"]


@mcp.resource(
    "repo://{path}",
    name="source-file",
    title="Repository source excerpt",
    mime_type="text/x-java",
)
def source_file(path: str) -> str:
    """Read a Java source excerpt from the RepoMedic fixture repository."""
    name = path.rsplit("/", 1)[-1]
    if name not in SOURCE_FILES:
        raise ValueError("source file not found: %s" % path)
    return SOURCE_FILES[name]


@mcp.prompt(title="事故分诊")
def triage(
    service: Annotated[str, Field(description="Service under investigation.")],
    symptom: Annotated[str, Field(description="Observed symptom, e.g. pool exhaustion.")],
) -> str:
    """Generate the RepoMedic triage prompt for one incident."""
    return (
        "你是 RepoMedic 的排障助手。\n"
        "服务：%s\n症状：%s\n\n"
        "按以下顺序工作：\n"
        "1. 用 query_logs 找 3 条直接证据，保留时间戳与错误码。\n"
        "2. 用 check_health 确认当前指标。\n"
        "3. 用 search_runbook 找同类事故；需要时读取对应 runbook 资源。\n"
        "4. 输出：根因假设、证据、缓解步骤、仍缺什么。\n"
        "不要执行 restart_service；它需要人工审批。"
    ) % (service, symptom)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RepoMedic MCP server")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8000)
    options = parser.parse_args()

    if options.http:
        mcp.run(transport="streamable-http", host="127.0.0.1", port=options.port)
    else:
        mcp.run()  # stdio; clients launch this file as a subprocess
