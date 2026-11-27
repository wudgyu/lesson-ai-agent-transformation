#!/usr/bin/env python3
"""Command-line demos for the RepoMedic serving lab.

Examples:
    python3 service_cli.py demo
    python3 service_cli.py load
    python3 service_cli.py retry
    python3 service_cli.py resume
    python3 service_cli.py shutdown
    python3 service_cli.py all

All scenarios use virtual time and a deterministic agent, so the numbers are
stable and the whole run takes well under a second.
"""

import argparse
import asyncio

from clock import ControlledClock, FakeClock
from models import RunRequest
from retry import retry_amplification
from run_service import RunService, ServiceConfig


def _request(tenant, incident, key):
    return RunRequest(
        tenant=tenant,
        service="checkout-api",
        incident=incident,
        idempotency_key=key,
    )


def _event_line(event):
    phase = event.data.get("phase") or ""
    detail = ""
    if event.type == "retry":
        detail = "%s retry=%d delay=%dms" % (
            event.data.get("phase"), event.data.get("retry"), event.data.get("delay_ms"))
    elif event.type == "checkpointed":
        detail = "at phase_index=%s" % event.data.get("phase_index")
    elif event.type == "done":
        detail = event.data.get("status", "")
    elif phase:
        detail = phase
    return "  #%02d %-14s %s" % (event.seq, event.type, detail)


async def cmd_demo():
    clock = FakeClock()
    service = RunService(
        config=ServiceConfig(
            workers=1, max_queue=4, per_tenant_capacity=10,
            per_tenant_refill=0, phase_latency_s=0.01, run_deadline_s=5.0,
            drain_timeout_s=0.05,
        ),
        clock=clock,
    )
    await service.start()
    print("RepoMedic serving demo")
    print("=" * 72)
    admission = await service.submit(_request("acme", "R01", "demo-1"))
    print("POST /v1/runs -> %d %s (%s)" % (
        admission.status, admission.code, admission.run_id))
    print("")
    print("GET /v1/runs/%s/events (SSE)" % admission.run_id)
    async for event in service.subscribe(admission.run_id, 0):
        print(_event_line(event))
    print("")
    run = service.get(admission.run_id)
    print("result: %s (confidence %.2f)" % (
        run.result["root_cause"], run.result["confidence"]))
    print("")
    duplicate = await service.submit(_request("acme", "R01", "demo-1"))
    print("retry the same POST with Idempotency-Key: demo-1")
    print("  -> %d %s, same run: %s" % (
        duplicate.status, duplicate.code, duplicate.run_id == admission.run_id))
    print("")
    print("the stream is a view; GET /v1/runs/%s is the source of truth"
          % admission.run_id)
    await service.drain(0.05)
    return 0


async def cmd_load():
    print("Admission control under load")
    print("=" * 72)

    print("")
    print("A. per-tenant quota: 12 submissions, 3 per tenant, capacity 3")
    clock = FakeClock()
    service = RunService(
        config=ServiceConfig(
            workers=4, max_queue=50, per_tenant_capacity=3,
            per_tenant_refill=0, phase_latency_s=0.001, run_deadline_s=5.0,
            drain_timeout_s=0.05,
        ),
        clock=clock,
    )
    await service.start()
    counts = {200: 0, 202: 0, 409: 0, 422: 0, 429: 0, 503: 0}
    for index in range(12):
        tenant = "tenant-" + "ABC"[index % 3]
        admission = await service.submit(_request(tenant, "R01", "quota-%d" % index))
        counts[admission.status] = counts.get(admission.status, 0) + 1
        await asyncio.sleep(0)
    await service._queue.join()
    print("  accepted 202: %d" % counts[202])
    print("  rejected 429: %d (Retry-After from the unfilled bucket)" % counts[429])
    print("  max_inflight: %d" % service.metrics["max_inflight"])
    print("  completed: %d" % service.metrics["succeeded"])
    await service.drain(0.05)

    print("")
    print("B. saturation: workers=3, queue=4, 12 submissions, unlimited quota")
    clock = ControlledClock()
    clock.pause()
    service = RunService(
        config=ServiceConfig(
            workers=3, max_queue=4, per_tenant_capacity=100,
            per_tenant_refill=0, phase_latency_s=0.001, run_deadline_s=5.0,
            drain_timeout_s=0.05,
        ),
        clock=clock,
    )
    await service.start()
    counts = {}
    for index in range(12):
        admission = await service.submit(_request("tenant-A", "R01", "sat-%d" % index))
        counts[admission.status] = counts.get(admission.status, 0) + 1
        await asyncio.sleep(0)
    print("  accepted 202: %d (3 running + 4 queued)" % counts.get(202, 0))
    print("  rejected 503: %d (bounded queue full; quota token refunded)" % counts.get(503, 0))
    print("  max_inflight: %d" % service.metrics["max_inflight"])
    print("  queue_depth: %d" % service._queue.qsize())
    await service.drain(0.0)
    return 0


async def cmd_retry():
    print("Retries are a budget, not a reflex")
    print("=" * 72)
    print("")
    print("R02: the logs tool returns 503 on its first two attempts")
    clock = FakeClock()
    service = RunService(
        config=ServiceConfig(
            workers=1, max_queue=4, per_tenant_capacity=100,
            per_tenant_refill=0, phase_latency_s=0.001, run_deadline_s=5.0,
            retry_attempts=3, retry_base_s=0.05, retry_max_s=0.4,
            retry_budget_capacity=8.0, retry_budget_refill=0.0,
            drain_timeout_s=0.05,
        ),
        clock=clock,
    )
    await service.start()
    admission = await service.submit(_request("acme", "R02", "retry-1"))
    await service._queue.join()
    run = service.get(admission.run_id)
    for event in run.events:
        if event.type in ("retry", "phase_finished", "done"):
            print(_event_line(event))
    print("")
    print("  attempts: %d · retries: %d · retry tokens left: %.2f" % (
        run.attempts,
        len([e for e in run.events if e.type == "retry"]),
        service.snapshot_metrics()["retry_tokens"],
    ))
    await service.drain(0.05)

    print("")
    print("Same run with a zero-capacity retry budget")
    clock = FakeClock()
    service = RunService(
        config=ServiceConfig(
            workers=1, max_queue=4, per_tenant_capacity=100,
            per_tenant_refill=0, phase_latency_s=0.001, run_deadline_s=5.0,
            retry_attempts=3, retry_base_s=0.05, retry_max_s=0.4,
            retry_budget_capacity=0.0, retry_budget_refill=0.0,
            drain_timeout_s=0.05,
        ),
        clock=clock,
    )
    await service.start()
    admission = await service.submit(_request("acme", "R02", "retry-2"))
    await service._queue.join()
    run = service.get(admission.run_id)
    print("  status: %s · code: %s" % (run.status, run.error["code"]))
    await service.drain(0.05)

    print("")
    print("Layered retries multiply (3 attempts per layer):")
    for layers in range(1, 6):
        print("  layers=%d -> %d downstream calls" % (
            layers, retry_amplification(layers, 3)))
    return 0


async def cmd_resume():
    print("SSE: Last-Event-ID, replay and the ring buffer")
    print("=" * 72)
    clock = FakeClock()
    service = RunService(
        config=ServiceConfig(
            workers=1, max_queue=4, per_tenant_capacity=100,
            per_tenant_refill=0, phase_latency_s=0.001, run_deadline_s=5.0,
            event_retention=64, drain_timeout_s=0.05,
        ),
        clock=clock,
    )
    await service.start()
    admission = await service.submit(_request("acme", "R01", "resume-1"))
    await service._queue.join()
    run = service.get(admission.run_id)
    print("run %s has %d events" % (run.run_id, run.last_seq))
    print("")
    print("client A reads four events, then disconnects:")
    first = []
    async for event in service.subscribe(run.run_id, 0):
        first.append(event)
        if len(first) == 4:
            break
    for event in first:
        print(_event_line(event))
    last_id = first[-1].seq
    print("  disconnected after Last-Event-ID=%d" % last_id)
    print("")
    print("client B reconnects with Last-Event-ID=%d:" % last_id)
    second = []
    async for event in service.subscribe(run.run_id, last_id):
        second.append(event)
    for event in second[:3]:
        print(_event_line(event))
    if len(second) > 3:
        print("  ... %d more events ..." % (len(second) - 3))
        print(_event_line(second[-1]))
    overlap = set(e.seq for e in first) & set(e.seq for e in second)
    print("  overlap: %d events · final: %s" % (len(overlap), second[-1].type))
    await service.drain(0.05)

    print("")
    print("Now shrink the ring buffer to 4 and ask from id=1:")
    clock = FakeClock()
    service = RunService(
        config=ServiceConfig(
            workers=1, max_queue=4, per_tenant_capacity=100,
            per_tenant_refill=0, phase_latency_s=0.001, run_deadline_s=5.0,
            event_retention=4, drain_timeout_s=0.05,
        ),
        clock=clock,
    )
    await service.start()
    admission = await service.submit(_request("acme", "R01", "resume-2"))
    await service._queue.join()
    events = []
    async for event in service.subscribe(admission.run_id, 1):
        events.append(event)
    for event in events[:3]:
        print(_event_line(event))
    print("  ...")
    print(_event_line(events[-1]))
    print("  the snapshot at id=%d compresses the lost prefix; the result is"
          % events[0].seq)
    print("  still in the store, not in the stream")
    await service.drain(0.05)
    return 0


async def cmd_shutdown():
    print("SIGTERM is not a power cut")
    print("=" * 72)
    clock = ControlledClock()
    clock.pause()
    config = ServiceConfig(
        workers=2, max_queue=8, per_tenant_capacity=100,
        per_tenant_refill=0, phase_latency_s=0.01, run_deadline_s=10.0,
        drain_timeout_s=0.05,
    )
    service = RunService(config=config, clock=clock)
    await service.start()
    for index in range(6):
        await service.submit(_request("acme", "R01", "shutdown-%d" % index))
    for _ in range(20):
        await asyncio.sleep(0)
    running = [r for r in service.store.runs.values() if r.status == "running"]
    queued = [r for r in service.store.runs.values() if r.status == "queued"]
    print("before shutdown: %d running, %d queued" % (len(running), len(queued)))
    service.begin_shutdown()
    print("readiness: %s" % service.ready())
    report = await service.drain(0.0)
    print("drain: timed_out=%s · inflight=%d · checkpointed=%d · queued_for_restart=%d"
          % (report["timed_out"], report["inflight_at_start"],
             report["checkpointed"], report["queued_for_restart"]))
    print("")

    print("new process, same store:")
    service2 = RunService(store=service.store, config=config, clock=clock)
    clock.resume()
    await service2.start()
    await service2._queue.join()
    statuses = {}
    for run in service2.store.runs.values():
        statuses[run.status] = statuses.get(run.status, 0) + 1
    print("  statuses: %s" % statuses)
    print("  resumed: %d · effects: %d (each published exactly once)" % (
        service2.metrics["resumed"], service2.store.effects.count()))
    print("  attempts are cumulative across restarts: %s" % [
        r.attempts for r in service2.store.runs.values()])
    await service2.drain(0.05)
    return 0


async def cmd_all():
    for command in (cmd_demo, cmd_load, cmd_retry, cmd_resume, cmd_shutdown):
        await command()
        print("")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="RepoMedic serving lab")
    sub = parser.add_subparsers(dest="command")
    for name, func, help_text in (
        ("demo", cmd_demo, "one run: accept, stream, result, idempotent retry"),
        ("load", cmd_load, "quota and saturation scenarios"),
        ("retry", cmd_retry, "retry budget and amplification"),
        ("resume", cmd_resume, "SSE replay, Last-Event-ID and snapshot"),
        ("shutdown", cmd_shutdown, "drain, checkpoint and restart"),
        ("all", cmd_all, "run every demo"),
    ):
        parser_command = sub.add_parser(name, help=help_text)
        parser_command.set_defaults(func=func)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return asyncio.run(args.func())


if __name__ == "__main__":
    raise SystemExit(main())
