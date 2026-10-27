"""Refactored toolset: four high-level tools designed for an LLM client.

对照 baseline，变化集中在五处：
1. 合并工作流：健康检查从 3 次调用变成 1 次；
2. 命名与边界：service / level / since_minutes / limit 都是明确、可验证的参数；
3. 返回语义化上下文：summary、db_pool、recent_deploy，而不是裸字段；
4. token 效率：日志过滤 + 条数上限 + 单行截断；runbook 只返回 top-k 片段；
5. description 当 prompt：写清何时用、何时不用、失败时怎么办。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Set

from data_access import (
    NOW,
    approvals,
    read_logs,
    restart_raw,
    runbooks,
    service_record,
)
from tool_base import ToolError, ToolRegistry

_RECEIPTS: Dict[str, Dict[str, Any]] = {}


def reset_receipts() -> None:
    """测试用：清空幂等记录。"""
    _RECEIPTS.clear()


def _search_terms(query: str) -> Set[str]:
    """中文没有空格，补一层 2-gram，避免“连接池耗尽”匹配不到“连接池”。"""
    terms: Set[str] = set()
    for chunk in re.split(r"[\s，。、；：！？,.;:!?]+", query):
        chunk = chunk.strip()
        if not chunk:
            continue
        terms.add(chunk.lower())
        if len(chunk) > 2:
            for index in range(len(chunk) - 1):
                pair = chunk[index : index + 2]
                if not pair.isascii():
                    terms.add(pair.lower())
    return terms


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def get_service_health(service: str) -> str:
        record = service_record(service)
        pool = record["db_pool"]
        deploy = record["recent_deploy"]
        status_cn = {"healthy": "健康", "degraded": "降级", "down": "不可用"}.get(
            record["status"], record["status"]
        )
        summary = (
            "%s 当前%s：5xx %.1f%%，p99 %dms，连接池 %d/%d，最近部署 %s（%s）。"
            % (
                service,
                status_cn,
                record["error_rate_5m"] * 100,
                record["p99_latency_ms"],
                pool["used"],
                pool["max"],
                deploy["version"],
                deploy["at"],
            )
        )
        return json.dumps(
            {
                "summary": summary,
                "service": service,
                "status": record["status"],
                "error_rate_5m": record["error_rate_5m"],
                "p99_latency_ms": record["p99_latency_ms"],
                "db_pool": pool,
                "recent_deploy": deploy,
                "dependencies": record["dependencies"],
                "owner": record["owner"],
                "port": record["port"],
            },
            ensure_ascii=False,
            indent=2,
        )

    def query_logs(
        service: str,
        level: str = "ERROR",
        since_minutes: int = 30,
        limit: int = 20,
    ) -> str:
        level = str(level or "ERROR").strip().upper()
        if level not in ("ERROR", "WARN", "INFO", "ALL"):
            raise ToolError(
                "level must be one of ERROR, WARN, INFO, ALL; got '%s'" % level
            )
        try:
            window = int(since_minutes)
            cap = int(limit)
        except (TypeError, ValueError):
            raise ToolError("since_minutes and limit must be integers")
        if not 1 <= window <= 1440:
            raise ToolError("since_minutes must be between 1 and 1440")
        if not 1 <= cap <= 50:
            raise ToolError("limit must be between 1 and 50")

        cutoff = NOW - timedelta(minutes=window)
        entries = [
            entry
            for entry in read_logs(service)
            if cutoff <= entry["ts"] <= NOW
            and (level == "ALL" or entry["level"] == level)
        ]
        matched = len(entries)
        shown = entries[:cap]
        header = "service=%s level=%s window=%dm matched=%d shown=%d" % (
            service,
            level,
            window,
            matched,
            len(shown),
        )
        if not shown:
            return header + "\nno logs matched. Try level=ALL or a wider since_minutes."
        body = "\n".join(entry["line"][:500] for entry in shown)
        return header + "\n" + body

    def search_runbooks(query: str, limit: int = 3) -> str:
        query = (query or "").strip()
        if not query:
            raise ToolError(
                "query is required; pass a short keyword such as 连接池, 回滚, or 部署"
            )
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            raise ToolError("limit must be an integer")
        if not 1 <= cap <= 10:
            raise ToolError("limit must be between 1 and 10")

        terms = _search_terms(query)
        results: List[Dict[str, Any]] = []
        for rel_path, lines in runbooks():
            # 按标题切 section：返回“处置”整段，而不是孤立的命中行。
            sections: List[Dict[str, Any]] = []
            current: Dict[str, Any] = {"heading": "(前言)", "start": 1, "lines": []}
            for lineno, line in enumerate(lines, 1):
                if line.startswith("#"):
                    if current["lines"]:
                        sections.append(current)
                    current = {
                        "heading": line.lstrip("#").strip(),
                        "start": lineno,
                        "lines": [],
                    }
                else:
                    current["lines"].append((lineno, line))
            if current["lines"]:
                sections.append(current)

            for section in sections:
                section_text = (
                    section["heading"]
                    + "\n"
                    + "\n".join(line for _, line in section["lines"])
                )
                lowered = section_text.lower()
                full_hit = query.lower() in lowered
                matched_terms = [term for term in terms if term in lowered]
                # 2-gram 容易误命中（“不存在的词”会命中“存在”），
                # 因此非完整命中时，至少要有两个词项命中。
                if not full_hit and len(matched_terms) < 2:
                    continue
                score = sum(lowered.count(term) for term in terms)
                results.append(
                    {
                        "score": score,
                        "source": rel_path,
                        "line": section["start"],
                        "heading": section["heading"],
                        "text": [
                            line.strip()
                            for _, line in section["lines"]
                            if line.strip()
                        ],
                    }
                )
        if not results:
            return (
                "no runbook matched '%s'. Try a shorter keyword "
                "(连接池 / 回滚 / 部署)." % query
            )
        results.sort(key=lambda item: (-item["score"], item["source"], item["line"]))
        selected = results[:cap]
        out = [
            "runbook matches for '%s' (top %d of %d):"
            % (query, len(selected), len(results))
        ]
        for index, item in enumerate(selected, 1):
            out.append(
                "[%d] %s:%d · %s (score=%d)"
                % (
                    index,
                    item["source"],
                    item["line"],
                    item["heading"],
                    item["score"],
                )
            )
            for line in item["text"][:6]:
                out.append("    " + line[:300])
        return "\n".join(out)

    def restart_service(service: str, reason: str, approval_id: str) -> str:
        service = (service or "").strip()
        reason = (reason or "").strip()
        approval_id = (approval_id or "").strip()
        service_record(service)  # validate service before touching approvals

        if not reason:
            raise ToolError(
                "reason is required for restart; ask the user why the restart is needed"
            )
        if not approval_id:
            raise ToolError(
                "approval_id is required for restart. Ask the on-call lead for a "
                "valid approval; do not guess."
            )

        approval = approvals().get(approval_id)
        if approval is None:
            raise ToolError(
                "approval_id '%s' is not valid. Ask the on-call lead for a valid "
                "approval; do not guess." % approval_id
            )
        if approval.get("service") != service:
            raise ToolError(
                "approval '%s' is for %s, not %s."
                % (approval_id, approval.get("service"), service)
            )
        if approval.get("action") != "restart":
            raise ToolError(
                "approval '%s' is for action '%s', not restart."
                % (approval_id, approval.get("action"))
            )
        expires_at = datetime.fromisoformat(approval["expires_at"])
        if expires_at < NOW:
            raise ToolError(
                "approval '%s' expired at %s. Request a new approval before restarting."
                % (approval_id, approval["expires_at"])
            )

        if approval_id in _RECEIPTS:
            return json.dumps(
                {
                    "status": "noop",
                    "service": service,
                    "message": "approval '%s' was already used; no second restart "
                    "was performed." % approval_id,
                    "previous_receipt": _RECEIPTS[approval_id],
                },
                ensure_ascii=False,
            )

        receipt = restart_raw(service, reason)
        receipt["approval_id"] = approval_id
        _RECEIPTS[approval_id] = receipt
        return json.dumps(receipt, ensure_ascii=False)

    registry.register(
        "get_service_health",
        "查询一个服务最近 5 分钟的聚合健康快照：状态、5xx 比例、p99 延迟、连接池、"
        "最近部署、依赖服务、负责人。用户问“X 健康吗 / 出了什么问题 / 最近部署了什么 / "
        "依赖谁”时优先用它。不要用它查历史日志或 runbook——那些用 query_logs 和 "
        "search_runbooks。",
        {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "服务名，例如 order-service、payment-service",
                }
            },
            "required": ["service"],
        },
        get_service_health,
    )
    registry.register(
        "query_logs",
        "按级别和时间窗口查询某个服务的日志，返回带时间戳的匹配行，并附带 matched/shown "
        "计数。用户问“过去 N 分钟有哪些 ERROR/WARN”“最近的异常是什么”时使用。默认 "
        "level=ERROR、since_minutes=30、limit=20；不要用 level=ALL 拉全量日志。",
        {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "服务名，例如 order-service"},
                "level": {
                    "type": "string",
                    "enum": ["ERROR", "WARN", "INFO", "ALL"],
                    "default": "ERROR",
                    "description": "日志级别；先查 ERROR，需要背景时再查 WARN/INFO",
                },
                "since_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1440,
                    "default": 30,
                    "description": "回看多少分钟；默认 30",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                    "description": "最多返回多少条；默认 20",
                },
            },
            "required": ["service"],
        },
        query_logs,
    )
    registry.register(
        "search_runbooks",
        "在 runbook 中检索处置步骤与排障知识。用户问“怎么处理 / 诊断步骤 / 回滚 / "
        "连接池 / 部署”时使用。返回来源文件与行号；回答时请引用来源。不要用它查服务"
        "实时指标或日志。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "短关键词，例如 连接池、回滚、部署；支持中文",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                    "description": "最多返回多少个片段；默认 3",
                },
            },
            "required": ["query"],
        },
        search_runbooks,
    )
    registry.register(
        "restart_service",
        "重启服务（高风险写操作）。只有在用户明确要求重启、且提供了 on-call 负责人签发的 "
        "有效 approval_id 时才调用；没有审批时先向用户说明需要审批，不要猜测单号。"
        "不要用它“顺便”修复问题。",
        {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "要重启的服务名"},
                "reason": {"type": "string", "description": "重启原因，必须具体"},
                "approval_id": {
                    "type": "string",
                    "description": "审批单号，例如由 on-call 负责人提供；没有就留空并先询问",
                },
            },
            "required": ["service", "reason", "approval_id"],
        },
        restart_service,
    )
    return registry
