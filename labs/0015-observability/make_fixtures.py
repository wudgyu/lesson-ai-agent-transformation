"""生成确定性的 RepoMedic trace fixtures。

数据是“录制”的，不调用模型；这样成本与延迟分析可以在无 API key、
无网络、无随机性的条件下被测试。运行：

    python3 make_fixtures.py

会写出 `traces.jsonl`、`pricing.json`、`budgets.json`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent


def _usage_attrs(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
) -> Dict[str, int]:
    return {
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.cache_read.input_tokens": cache_read,
        "gen_ai.usage.cache_write.input_tokens": cache_write,
        "gen_ai.usage.reasoning.output_tokens": reasoning,
    }


def span(
    trace_id: str,
    span_id: str,
    parent_id: Optional[str],
    name: str,
    kind: str,
    start_ms: int,
    end_ms: int,
    status: str = "ok",
    attrs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_id": parent_id,
        "name": name,
        "kind": kind,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "status": status,
        "attrs": attrs or {},
    }


def agent_span(
    trace_id: str,
    span_id: str,
    parent_id: Optional[str],
    start_ms: int,
    end_ms: int,
    name: str = "RepoMedic",
    **attrs: Any,
) -> Dict[str, Any]:
    base = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": name,
    }
    base.update(attrs)
    return span(
        trace_id, span_id, parent_id, "invoke_agent %s" % name, "agent", start_ms, end_ms, attrs=base
    )


def llm_span(
    trace_id: str,
    span_id: str,
    parent_id: Optional[str],
    start_ms: int,
    end_ms: int,
    model: str,
    step: int,
    input_tokens: Optional[int],
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
    ttft_ms: Optional[int] = 600,
    attempt: int = 1,
    status: str = "ok",
) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai-compatible",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "app.step": step,
        "app.attempt": attempt,
    }
    if ttft_ms is not None:
        attrs["app.ttft_ms"] = ttft_ms
    if input_tokens is not None:
        attrs.update(
            _usage_attrs(
                input_tokens, output_tokens, cache_read, cache_write, reasoning
            )
        )
    return span(
        trace_id,
        span_id,
        parent_id,
        "chat %s" % model,
        "llm",
        start_ms,
        end_ms,
        status=status,
        attrs=attrs,
    )


def tool_span(
    trace_id: str,
    span_id: str,
    parent_id: Optional[str],
    start_ms: int,
    end_ms: int,
    tool: str,
    tier: str = "T0",
    status: str = "ok",
) -> Dict[str, Any]:
    return span(
        trace_id,
        span_id,
        parent_id,
        "execute_tool %s" % tool,
        "tool",
        start_ms,
        end_ms,
        status=status,
        attrs={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.id": "call-%s-%s" % (trace_id, span_id),
            "app.tool.tier": tier,
        },
    )


def approval_span(
    trace_id: str,
    span_id: str,
    parent_id: Optional[str],
    start_ms: int,
    end_ms: int,
    action: str,
    outcome: str = "approved",
) -> Dict[str, Any]:
    return span(
        trace_id,
        span_id,
        parent_id,
        "approval %s" % action,
        "approval",
        start_ms,
        end_ms,
        attrs={
            "app.approval.action": action,
            "app.approval.outcome": outcome,
            "app.approval.wait_ms": end_ms - start_ms,
        },
    )


def retrieval_span(
    trace_id: str,
    span_id: str,
    parent_id: Optional[str],
    start_ms: int,
    end_ms: int,
    k: int = 5,
) -> Dict[str, Any]:
    return span(
        trace_id,
        span_id,
        parent_id,
        "retrieval runbook",
        "retrieval",
        start_ms,
        end_ms,
        attrs={"gen_ai.operation.name": "retrieval", "app.retrieval.k": k},
    )


def guardrail_span(
    trace_id: str,
    span_id: str,
    parent_id: Optional[str],
    start_ms: int,
    end_ms: int,
    result: str = "pass",
) -> Dict[str, Any]:
    return span(
        trace_id,
        span_id,
        parent_id,
        "guardrail prompt_injection",
        "guardrail",
        start_ms,
        end_ms,
        attrs={"app.guardrail.name": "prompt_injection", "app.guardrail.result": result},
    )


def finish(
    trace_id: str,
    case_id: str,
    arm: str,
    success: bool,
    spans: List[Dict[str, Any]],
    started_at: str,
    root_input_delta: int = 0,
) -> Dict[str, Any]:
    root = spans[0]
    root["end_ms"] = max(item["end_ms"] for item in spans)
    root["status"] = "ok" if success else "error"
    root_attrs = root["attrs"]
    root_attrs["gen_ai.request.model"] = next(
        (item["attrs"]["gen_ai.request.model"] for item in spans if item["kind"] == "llm"),
        "medic-small",
    )

    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    reasoning = 0
    for item in spans:
        if item["kind"] != "llm":
            continue
        attrs = item["attrs"]
        if "gen_ai.usage.input_tokens" not in attrs:
            continue
        input_tokens += attrs["gen_ai.usage.input_tokens"]
        output_tokens += attrs["gen_ai.usage.output_tokens"]
        cache_read += attrs["gen_ai.usage.cache_read.input_tokens"]
        cache_write += attrs["gen_ai.usage.cache_write.input_tokens"]
        reasoning += attrs["gen_ai.usage.reasoning.output_tokens"]

    if root_input_delta:
        input_tokens += root_input_delta
    root_attrs.update(
        _usage_attrs(input_tokens, output_tokens, cache_read, cache_write, reasoning)
    )

    spans.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["span_id"]))
    return {
        "trace_id": trace_id,
        "run_id": "run-%s" % trace_id,
        "case_id": case_id,
        "arm": arm,
        "success": success,
        "started_at": started_at,
        "metadata": {"agent_version": arm, "source": "fixtures"},
        "spans": spans,
    }


def build_traces() -> List[Dict[str, Any]]:
    traces: List[Dict[str, Any]] = []

    # R01: normal triage, sequential loop, moderate cache.
    t = "R01"
    spans = [
        agent_span(t, "R01-root", None, 0, 0),
        llm_span(t, "R01-l1", "R01-root", 0, 2400, "medic-large", 1, 1200, 180, cache_read=800, ttft_ms=700),
        tool_span(t, "R01-t1", "R01-root", 2500, 3300, "query_logs"),
        llm_span(t, "R01-l2", "R01-root", 3400, 6100, "medic-large", 2, 1900, 240, cache_read=1200, ttft_ms=850),
        tool_span(t, "R01-t2", "R01-root", 6200, 6900, "get_metrics"),
        llm_span(t, "R01-l3", "R01-root", 7000, 10500, "medic-large", 3, 2600, 320, cache_read=1500, ttft_ms=900),
        tool_span(t, "R01-t3", "R01-root", 10600, 11600, "search_code"),
        llm_span(t, "R01-l4", "R01-root", 11700, 14000, "medic-large", 4, 3300, 200, cache_read=2100, ttft_ms=600),
    ]
    traces.append(finish(t, "C01", "v05-hotfix", True, spans, "2026-11-18T09:00:00Z"))

    # R02: small model, one slow tool, no retry.
    t = "R02"
    spans = [
        agent_span(t, "R02-root", None, 0, 0),
        llm_span(t, "R02-l1", "R02-root", 0, 2000, "medic-small", 1, 1400, 150, cache_read=400, ttft_ms=500),
        tool_span(t, "R02-t1", "R02-root", 2000, 2600, "query_logs"),
        tool_span(t, "R02-t2", "R02-root", 2600, 3100, "get_metrics"),
        llm_span(t, "R02-l2", "R02-root", 3100, 5400, "medic-small", 2, 1800, 220, cache_read=600, ttft_ms=650),
        tool_span(t, "R02-t3", "R02-root", 5400, 8600, "search_code"),
        llm_span(t, "R02-l3", "R02-root", 8600, 9500, "medic-small", 3, 2200, 160, cache_read=800, ttft_ms=550),
    ]
    traces.append(finish(t, "C02", "v05-hotfix", True, spans, "2026-11-18T09:10:00Z"))

    # R03: timeout + retry; failed call is still billable.
    t = "R03"
    spans = [
        agent_span(t, "R03-root", None, 0, 0),
        llm_span(t, "R03-l1", "R03-root", 0, 15000, "medic-large", 1, 1500, 0, ttft_ms=None, status="error"),
        llm_span(t, "R03-l2", "R03-root", 15000, 17400, "medic-large", 1, 1500, 160, ttft_ms=800, attempt=2),
        tool_span(t, "R03-t1", "R03-root", 17500, 18800, "query_logs", status="error"),
        llm_span(t, "R03-l3", "R03-root", 18800, 22000, "medic-large", 2, 2000, 180, ttft_ms=700),
    ]
    traces.append(finish(t, "C03", "v05-hotfix", False, spans, "2026-11-18T09:20:00Z"))

    # R04: human approval dominates wall time; active time is much smaller.
    t = "R04"
    spans = [
        agent_span(t, "R04-root", None, 0, 0),
        llm_span(t, "R04-l1", "R04-root", 0, 3000, "medic-large", 1, 1000, 120, cache_read=600, ttft_ms=700),
        tool_span(t, "R04-t1", "R04-root", 3000, 4000, "search_code"),
        approval_span(t, "R04-a1", "R04-root", 4000, 92000, "restart_service"),
        llm_span(t, "R04-l2", "R04-root", 92000, 95000, "medic-large", 2, 1600, 180, cache_read=900, ttft_ms=650),
    ]
    traces.append(finish(t, "C04", "v05-hotfix", True, spans, "2026-11-18T09:30:00Z"))

    # R05: two parallel tools. Summing tool durations overcounts wall time.
    t = "R05"
    spans = [
        agent_span(t, "R05-root", None, 0, 0),
        llm_span(t, "R05-l1", "R05-root", 0, 2500, "medic-large", 1, 1200, 160, cache_read=700, ttft_ms=600),
        tool_span(t, "R05-t1", "R05-root", 2500, 9500, "query_logs"),
        tool_span(t, "R05-t2", "R05-root", 2500, 9500, "get_metrics"),
        llm_span(t, "R05-l2", "R05-root", 9500, 12000, "medic-large", 2, 2600, 220, cache_read=1200, ttft_ms=700),
    ]
    traces.append(finish(t, "C05", "v05-hotfix", True, spans, "2026-11-18T09:40:00Z"))

    # R06: long context. Input tokens grow each turn and are re-billed.
    t = "R06"
    spans = [
        agent_span(t, "R06-root", None, 0, 0),
        llm_span(t, "R06-l1", "R06-root", 0, 2200, "medic-large", 1, 1200, 150, ttft_ms=600),
        tool_span(t, "R06-t1", "R06-root", 2200, 2800, "query_logs"),
        llm_span(t, "R06-l2", "R06-root", 2800, 5000, "medic-large", 2, 1800, 150, ttft_ms=600),
        tool_span(t, "R06-t2", "R06-root", 5000, 5600, "get_metrics"),
        llm_span(t, "R06-l3", "R06-root", 5600, 7800, "medic-large", 3, 2400, 150, ttft_ms=600),
        tool_span(t, "R06-t3", "R06-root", 7800, 8400, "search_code"),
        llm_span(t, "R06-l4", "R06-root", 8400, 10600, "medic-large", 4, 3000, 150, ttft_ms=600),
        tool_span(t, "R06-t4", "R06-root", 10600, 11200, "query_logs"),
        llm_span(t, "R06-l5", "R06-root", 11200, 13400, "medic-large", 5, 3600, 150, ttft_ms=600),
        tool_span(t, "R06-t5", "R06-root", 13400, 14000, "search_code"),
        llm_span(t, "R06-l6", "R06-root", 14000, 16200, "medic-large", 6, 4200, 150, ttft_ms=600),
        llm_span(t, "R06-l7", "R06-root", 16200, 18400, "medic-large", 7, 4800, 150, ttft_ms=600),
        llm_span(t, "R06-l8", "R06-root", 18400, 20600, "medic-large", 8, 5400, 150, ttft_ms=600),
    ]
    traces.append(finish(t, "C06", "v06-candidate", True, spans, "2026-11-19T10:00:00Z"))

    # R07: cache-friendly. Large prefix reused every turn.
    t = "R07"
    spans = [
        agent_span(t, "R07-root", None, 0, 0),
        llm_span(t, "R07-l1", "R07-root", 0, 2000, "medic-small", 1, 4000, 200, cache_read=3800, ttft_ms=300),
        tool_span(t, "R07-t1", "R07-root", 2000, 2400, "query_logs"),
        llm_span(t, "R07-l2", "R07-root", 2400, 4400, "medic-small", 2, 4000, 200, cache_read=3800, ttft_ms=300),
        tool_span(t, "R07-t2", "R07-root", 4400, 4800, "get_metrics"),
        llm_span(t, "R07-l3", "R07-root", 4800, 6800, "medic-small", 3, 4000, 200, cache_read=3800, ttft_ms=300),
        tool_span(t, "R07-t3", "R07-root", 6800, 7200, "search_code"),
        llm_span(t, "R07-l4", "R07-root", 7200, 8000, "medic-small", 4, 4000, 200, cache_read=3800, ttft_ms=300),
    ]
    traces.append(finish(t, "C07", "v06-candidate", True, spans, "2026-11-19T10:10:00Z"))

    # R08: one inference span has no usage; cost is a lower bound, not a guess.
    t = "R08"
    spans = [
        agent_span(t, "R08-root", None, 0, 0),
        llm_span(t, "R08-l1", "R08-root", 0, 2200, "medic-small", 1, 1500, 160, cache_read=500, ttft_ms=500),
        tool_span(t, "R08-t1", "R08-root", 2200, 3000, "query_logs"),
        llm_span(t, "R08-l2", "R08-root", 3000, 5200, "medic-small", 2, None, ttft_ms=500),
        tool_span(t, "R08-t2", "R08-root", 5200, 5800, "get_metrics"),
        llm_span(t, "R08-l3", "R08-root", 5800, 8000, "medic-small", 3, 2400, 180, cache_read=900, ttft_ms=550),
    ]
    traces.append(finish(t, "C08", "v06-candidate", False, spans, "2026-11-19T10:20:00Z"))

    # R09: subagent span aggregates usage; naive sum over every span double counts.
    t = "R09"
    spans = [
        agent_span(t, "R09-root", None, 0, 0),
        llm_span(t, "R09-l1", "R09-root", 0, 2000, "medic-large", 1, 1200, 120, cache_read=800, ttft_ms=600),
        agent_span(t, "R09-sub", "R09-root", 2000, 15000, name="LogSpecialist",
                  **{"gen_ai.request.model": "medic-large"}),
        llm_span(t, "R09-l2", "R09-sub", 2000, 6000, "medic-large", 2, 3000, 260, cache_read=1500, ttft_ms=700),
        tool_span(t, "R09-t1", "R09-sub", 6000, 7000, "query_logs"),
        llm_span(t, "R09-l3", "R09-sub", 7000, 14500, "medic-large", 3, 4500, 300, cache_read=2000, ttft_ms=800),
        llm_span(t, "R09-l4", "R09-sub", 14500, 15000, "medic-large", 4, 2000, 100, ttft_ms=400),
        llm_span(t, "R09-l5", "R09-root", 15000, 18000, "medic-large", 5, 3500, 220, cache_read=1800, ttft_ms=650),
    ]
    traces.append(finish(t, "C01", "v06-candidate", True, spans, "2026-11-19T10:30:00Z", root_input_delta=500))

    # R10: one slow tool among five parallel calls. Tail latency, not average.
    t = "R10"
    spans = [
        agent_span(t, "R10-root", None, 0, 0),
        llm_span(t, "R10-l1", "R10-root", 0, 2000, "medic-small", 1, 1000, 120, cache_read=400, ttft_ms=450),
        tool_span(t, "R10-t1", "R10-root", 2000, 11000, "query_logs"),
        tool_span(t, "R10-t2", "R10-root", 2000, 2500, "get_metrics"),
        tool_span(t, "R10-t3", "R10-root", 2000, 2500, "search_code"),
        tool_span(t, "R10-t4", "R10-root", 2000, 2500, "get_deploy_history"),
        tool_span(t, "R10-t5", "R10-root", 2000, 2500, "consult_runbook"),
        llm_span(t, "R10-l2", "R10-root", 11000, 12000, "medic-small", 2, 3000, 200, cache_read=1200, ttft_ms=500),
    ]
    traces.append(finish(t, "C02", "v06-candidate", True, spans, "2026-11-19T10:40:00Z"))

    return traces


PRICING = {
    "currency": "USD",
    "unit": "per_1m_tokens",
    "note": "示意价格，不是任何供应商报价；用于练习账本结构。",
    "prices": [
        {
            "model": "medic-small",
            "input_per_million": 0.40,
            "output_per_million": 1.60,
            "cache_read_per_million": 0.04,
            "cache_write_per_million": 0.50,
            "effective_from": "2026-10-01",
        },
        {
            "model": "medic-large",
            "input_per_million": 2.50,
            "output_per_million": 10.00,
            "cache_read_per_million": 0.25,
            "cache_write_per_million": 3.125,
            "effective_from": "2026-10-01",
        },
    ],
}

BUDGETS = {
    "active_p95_ms": 15000,
    "max_active_ms": 20000,
    "tool_p95_ms": 8000,
    "approval_p95_ms": 120000,
    "ttft_p95_ms": 1200,
    "cost_per_success_usd": 0.020,
    "cache_hit_rate_min": 0.35,
    "retry_share_max": 0.05,
}


def main() -> None:
    traces = build_traces()
    with (HERE / "traces.jsonl").open("w", encoding="utf-8") as handle:
        for trace in traces:
            handle.write(json.dumps(trace, ensure_ascii=False, sort_keys=True) + "\n")
    with (HERE / "pricing.json").open("w", encoding="utf-8") as handle:
        json.dump(PRICING, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with (HERE / "budgets.json").open("w", encoding="utf-8") as handle:
        json.dump(BUDGETS, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("wrote %d traces, %d spans" % (len(traces), sum(len(t["spans"]) for t in traces)))


if __name__ == "__main__":
    main()
