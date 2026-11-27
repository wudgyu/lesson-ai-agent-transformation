"""Offline tests for the serving core.

No network, no API key, no real sleeps.  Run with:

    python3 -m unittest test_service -v

The HTTP adapter has its own optional tests in ``test_api.py``.
"""

import asyncio
import unittest

from clock import ControlledClock, FakeClock
from errors import RunNotFound
from limiter import RetryBudget, TokenBucket, adaptive_reject_probability
from models import RunRequest
from retry import RetryPolicy, retry_amplification
from run_service import RunService, ServiceConfig
from store import EffectLedger, Store, fingerprint


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def make_service(self, clock=None, **overrides):
        options = {
            "workers": 1,
            "max_queue": 4,
            "per_tenant_capacity": 100,
            "per_tenant_refill": 0.0,
            "idempotency_ttl_s": 24 * 3600,
            "event_retention": 64,
            "subscriber_queue": 16,
            "run_deadline_s": 5.0,
            "phase_latency_s": 0.0,
            "retry_attempts": 3,
            "retry_base_s": 0.0,
            "retry_max_s": 0.0,
            "retry_budget_capacity": 8.0,
            "retry_budget_refill": 0.0,
            "drain_timeout_s": 0.05,
            "heartbeat_s": None,
        }
        options.update(overrides)
        service = RunService(config=ServiceConfig(**options), clock=clock or FakeClock())
        await service.start()
        self.addAsyncCleanup(service.drain, 0.01)
        return service

    async def submit(self, service, key="k1", incident="R01", tenant="t1", priority="normal"):
        return await service.submit(
            RunRequest(
                tenant=tenant,
                service="checkout-api",
                incident=incident,
                idempotency_key=key,
                priority=priority,
            )
        )

    async def wait_idle(self, service, timeout=2.0):
        await asyncio.wait_for(service._queue.join(), timeout=timeout)

    async def wait_for_status(self, run, status, ticks=200):
        for _ in range(ticks):
            if run.status == status:
                return
            await asyncio.sleep(0)
        self.fail("run %s did not reach %s (was %s)" % (run.run_id, status, run.status))


class AdmissionTests(ServiceTestCase):
    async def test_accept_returns_202_and_run_id(self):
        service = await self.make_service()
        admission = await self.submit(service)
        self.assertTrue(admission.ok)
        self.assertEqual(admission.status, 202)
        self.assertEqual(admission.run_id, "run-0001")
        self.assertEqual(service.get(admission.run_id).status, "queued")

    async def test_missing_idempotency_key_is_400(self):
        service = await self.make_service()
        admission = await self.submit(service, key="")
        self.assertEqual(admission.status, 400)
        self.assertEqual(admission.code, "missing_idempotency_key")
        self.assertEqual(len(service.store.runs), 0)

    async def test_duplicate_completed_request_returns_existing_run(self):
        service = await self.make_service()
        first = await self.submit(service)
        await self.wait_idle(service)
        again = await self.submit(service)
        self.assertEqual(again.status, 200)
        self.assertEqual(again.run_id, first.run_id)
        self.assertEqual(service.metrics["duplicate"], 1)
        self.assertEqual(len(service.store.runs), 1)

    async def test_same_key_different_payload_is_422(self):
        service = await self.make_service()
        first = await self.submit(service)
        await self.wait_idle(service)
        conflict = await self.submit(service, incident="R02")
        self.assertEqual(conflict.status, 422)
        self.assertEqual(conflict.code, "idempotency_conflict")
        self.assertEqual(conflict.run_id, first.run_id)
        self.assertEqual(len(service.store.runs), 1)

    async def test_inflight_duplicate_is_409(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(clock=clock)
        first = await self.submit(service)
        await self.wait_for_status(service.get(first.run_id), "running")
        again = await self.submit(service)
        self.assertEqual(again.status, 409)
        self.assertEqual(again.code, "in_progress")
        self.assertEqual(again.run_id, first.run_id)
        clock.resume()

    async def test_idempotency_is_scoped_by_tenant(self):
        service = await self.make_service()
        first = await self.submit(service, tenant="tenant-a")
        second = await self.submit(service, tenant="tenant-b")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertNotEqual(first.run_id, second.run_id)
        await self.wait_idle(service)

    async def test_idempotency_expires(self):
        clock = FakeClock()
        service = await self.make_service(clock=clock, idempotency_ttl_s=10)
        first = await self.submit(service)
        await self.wait_idle(service)
        clock.advance(11)
        second = await self.submit(service)
        self.assertTrue(second.ok)
        self.assertNotEqual(first.run_id, second.run_id)

    async def test_rate_limit_burst_and_retry_after(self):
        service = await self.make_service(per_tenant_capacity=2, per_tenant_refill=0.5)
        self.assertTrue((await self.submit(service, key="a")).ok)
        self.assertTrue((await self.submit(service, key="b")).ok)
        rejected = await self.submit(service, key="c")
        self.assertEqual(rejected.status, 429)
        self.assertEqual(rejected.retry_after, 2)
        self.assertEqual(service.metrics["rate_limited"], 1)
        await self.wait_idle(service)

    async def test_rate_limit_refills(self):
        clock = FakeClock()
        service = await self.make_service(
            clock=clock, per_tenant_capacity=1, per_tenant_refill=1.0
        )
        self.assertTrue((await self.submit(service, key="a")).ok)
        self.assertEqual((await self.submit(service, key="b")).status, 429)
        clock.advance(1)
        self.assertTrue((await self.submit(service, key="c")).ok)
        await self.wait_idle(service)

    async def test_queue_full_is_503_and_refunds_the_quota_token(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(
            clock=clock, workers=1, max_queue=1, per_tenant_capacity=3
        )
        first = await self.submit(service, key="a")
        await self.wait_for_status(service.get(first.run_id), "running")
        second = await self.submit(service, key="b")
        self.assertTrue(second.ok)  # fills the queue
        third = await self.submit(service, key="c")
        self.assertEqual(third.status, 503)
        self.assertEqual(third.code, "queue_full")
        bucket = service._buckets["t1"]
        self.assertAlmostEqual(bucket.available, 1.0, places=6)
        clock.resume()

    async def test_concurrency_limit(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(clock=clock, workers=2, max_queue=4)
        runs = []
        for key in ("a", "b", "c", "d"):
            admission = await self.submit(service, key=key)
            self.assertTrue(admission.ok)
            runs.append(service.get(admission.run_id))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(service.metrics["max_inflight"], 2)
        self.assertEqual(sum(1 for r in runs if r.status == "running"), 2)
        clock.resume()
        await self.wait_idle(service)

    async def test_submit_before_start_is_503(self):
        service = RunService(config=ServiceConfig())
        admission = await self.submit(service)
        self.assertEqual(admission.status, 503)
        self.assertEqual(admission.code, "draining")

    async def test_double_start_does_not_duplicate_workers(self):
        service = await self.make_service(workers=2)
        count = len(service._workers)
        await service.start()
        self.assertEqual(len(service._workers), count)


class ExecutionTests(ServiceTestCase):
    async def test_run_completes_and_result_is_persisted(self):
        service = await self.make_service()
        admission = await self.submit(service)
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.result["root_cause"], "missing null guard in OrderMapper.toDto")
        self.assertEqual(run.events[-1].type, "done")
        self.assertEqual(run.events[-1].data["status"], "succeeded")

    async def test_result_exists_before_done_event_is_delivered(self):
        service = await self.make_service()
        admission = await self.submit(service)
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        done_index = [e.type for e in run.events].index("done")
        self.assertEqual(done_index, len(run.events) - 1)
        self.assertIsNotNone(run.result)

    async def test_event_sequence_is_monotonic_and_complete(self):
        service = await self.make_service()
        admission = await self.submit(service)
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual([e.seq for e in run.events], list(range(1, run.last_seq + 1)))

    async def test_retryable_failure_is_retried(self):
        service = await self.make_service()
        admission = await self.submit(service, incident="R02")
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.attempts, 7)  # 5 phases + 2 injected retries
        retries = [e for e in run.events if e.type == "retry"]
        self.assertEqual(len(retries), 2)
        self.assertEqual(retries[0].data["phase"], "retrieve_logs")

    async def test_non_retryable_failure_is_not_retried(self):
        service = await self.make_service()
        admission = await self.submit(service, incident="R04")
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error["code"], "non_retryable")
        self.assertEqual(run.attempts, 4)  # stopped at publish; no retry
        self.assertFalse(any(e.type == "retry" for e in run.events))

    async def test_deadline_exceeded_marks_partial_failure(self):
        clock = FakeClock()
        service = await self.make_service(clock=clock, run_deadline_s=1.0)
        admission = await self.submit(service, incident="R03")
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error["code"], "deadline_exceeded")
        self.assertTrue(run.result["partial"])
        self.assertEqual(run.checkpoint["phase_index"], 3)
        self.assertEqual(run.events[-1].type, "done")

    async def test_retry_budget_exhaustion_fails_fast(self):
        service = await self.make_service(
            retry_budget_capacity=1.0, retry_budget_refill=0.0
        )
        admission = await self.submit(service, incident="R02")
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error["code"], "retry_budget_exhausted")
        self.assertEqual(service.metrics["failed"], 1)

    async def test_cancel_queued_run(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(clock=clock)
        first = await self.submit(service, key="a")
        second = await self.submit(service, key="b")
        queued = service.get(second.run_id)
        self.assertEqual(queued.status, "queued")
        service.cancel(queued.run_id)
        self.assertEqual(queued.status, "cancelled")
        self.assertEqual(queued.events[-1].type, "done")
        clock.resume()
        await self.wait_idle(service)
        self.assertEqual(service.get(first.run_id).status, "succeeded")
        self.assertEqual(service.get(second.run_id).status, "cancelled")

    async def test_cancel_running_run(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(clock=clock)
        admission = await self.submit(service)
        run = service.get(admission.run_id)
        await self.wait_for_status(run, "running")
        service.cancel(run.run_id)
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.error["code"], "cancelled")
        self.assertEqual(run.events[-1].type, "done")
        clock.resume()
        await self.wait_idle(service)

    async def test_cancel_terminal_run_is_noop(self):
        service = await self.make_service()
        admission = await self.submit(service)
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        before = len(run.events)
        service.cancel(run.run_id)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(len(run.events), before)

    async def test_priority_is_preserved(self):
        service = await self.make_service()
        admission = await self.submit(service, priority="critical")
        self.assertEqual(service.get(admission.run_id).request.priority, "critical")
        await self.wait_idle(service)

    async def test_metrics_after_one_run(self):
        service = await self.make_service()
        await self.submit(service)
        await self.wait_idle(service)
        metrics = service.snapshot_metrics()
        self.assertEqual(metrics["submitted"], 1)
        self.assertEqual(metrics["accepted"], 1)
        self.assertEqual(metrics["started"], 1)
        self.assertEqual(metrics["succeeded"], 1)
        self.assertEqual(metrics["effects"], 1)
        self.assertEqual(metrics["inflight"], 0)


class EventLogTests(ServiceTestCase):
    async def test_replay_from_zero_yields_all_events(self):
        service = await self.make_service()
        admission = await self.submit(service)
        await self.wait_idle(service)
        events = []
        async for event in service.subscribe(admission.run_id, 0):
            events.append(event)
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(events[-1].type, "done")
        self.assertEqual([e.seq for e in events], sorted(set(e.seq for e in events)))

    async def test_resume_from_last_event_id(self):
        service = await self.make_service()
        admission = await self.submit(service)
        await self.wait_idle(service)
        events = []
        async for event in service.subscribe(admission.run_id, 5):
            events.append(event)
        self.assertTrue(events)
        self.assertTrue(all(e.seq > 5 for e in events))
        self.assertEqual(events[-1].type, "done")

    async def test_gap_yields_snapshot_then_retained_events(self):
        service = await self.make_service(event_retention=3)
        admission = await self.submit(service)
        await self.wait_idle(service)
        events = []
        async for event in service.subscribe(admission.run_id, 1):
            events.append(event)
        self.assertEqual(events[0].type, "snapshot")
        self.assertEqual(events[0].seq, 11)
        self.assertEqual(events[0].data["status"], "succeeded")
        self.assertEqual(events[0].data["result"]["root_cause"],
                         "missing null guard in OrderMapper.toDto")
        self.assertEqual([e.seq for e in events[1:]], [12, 13, 14])

    async def test_brand_new_subscriber_gets_snapshot_when_prefix_evicted(self):
        service = await self.make_service(event_retention=3)
        admission = await self.submit(service)
        await self.wait_idle(service)
        events = []
        async for event in service.subscribe(admission.run_id, None):
            events.append(event)
        self.assertEqual(events[0].type, "snapshot")
        self.assertEqual(events[0].seq, 11)
        self.assertEqual(events[0].data["result"]["root_cause"],
                         "missing null guard in OrderMapper.toDto")
        self.assertEqual([e.seq for e in events[1:]], [12, 13, 14])

    async def test_terminal_stream_closes(self):
        service = await self.make_service()
        admission = await self.submit(service)
        await self.wait_idle(service)
        count = 0
        async for _ in service.subscribe(admission.run_id, None):
            count += 1
        self.assertGreater(count, 0)

    async def test_event_retention_keeps_the_latest(self):
        service = await self.make_service(event_retention=3)
        admission = await self.submit(service)
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual(len(run.events), 3)
        self.assertEqual([e.seq for e in run.events], [12, 13, 14])

    async def test_result_survives_event_eviction(self):
        service = await self.make_service(event_retention=2)
        admission = await self.submit(service)
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual(len(run.events), 2)
        self.assertIsNotNone(run.result)

    async def test_slow_consumer_gets_overflow_and_can_resume(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(
            clock=clock, subscriber_queue=2, event_retention=64
        )
        admission = await self.submit(service)
        stream = service.subscribe(admission.run_id, 0)
        first = await stream.__anext__()
        self.assertEqual(first.seq, 1)
        clock.resume()
        await self.wait_idle(service)
        events = []
        try:
            while True:
                events.append(await asyncio.wait_for(stream.__anext__(), timeout=1.0))
        except StopAsyncIteration:
            pass
        self.assertEqual(events[-1].type, "overflow")
        self.assertEqual(service.metrics["slow_consumers"], 1)
        # The overflow event carries the last delivered id, not the run's last
        # id, so the reconnect resumes exactly where this consumer stopped.
        resume_from = events[-1].data["last_event_id"]
        replay = []
        async for event in service.subscribe(admission.run_id, resume_from):
            replay.append(event)
        self.assertEqual(replay[-1].type, "done")

    async def test_heartbeat_is_a_ping_event(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(clock=clock)
        admission = await self.submit(service)
        stream = service.subscribe(admission.run_id, 0, heartbeat_s=0.01)
        first = await stream.__anext__()
        self.assertEqual(first.type, "queued")
        # The worker also emitted "started" for the paused run; consume it,
        # then the idle stream should produce a ping comment.
        seen = [first.type]
        for _ in range(3):
            event = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
            seen.append(event.type)
            if event.type == "ping":
                break
        self.assertIn("ping", seen)
        await stream.aclose()
        clock.resume()

    async def test_unknown_run_raises(self):
        service = await self.make_service()
        with self.assertRaises(RunNotFound):
            service.get("run-9999")
        with self.assertRaises(RunNotFound):
            async for _ in service.subscribe("run-9999", 0):
                pass


class ShutdownTests(ServiceTestCase):
    async def test_drain_fails_readiness_and_rejects_new_runs(self):
        service = await self.make_service()
        self.assertTrue(service.ready())
        service.begin_shutdown()
        self.assertFalse(service.ready())
        admission = await self.submit(service, key="after-drain")
        self.assertEqual(admission.status, 503)
        self.assertEqual(admission.code, "draining")

    async def test_drain_checkpoints_and_restart_resumes(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(clock=clock, workers=1, max_queue=4)
        admission = await self.submit(service)
        run = service.get(admission.run_id)
        await self.wait_for_status(run, "running")

        # Let two phases complete, then stop at the next await point.
        clock.resume()
        for _ in range(200):
            await asyncio.sleep(0)
            if run.checkpoint.get("phase_index", 0) >= 2:
                clock.pause()
                break
        self.assertGreaterEqual(run.checkpoint["phase_index"], 2)

        report = await service.drain(0.0)
        self.assertTrue(report["timed_out"])
        self.assertEqual(run.status, "queued")
        self.assertTrue(run.resumable)
        self.assertEqual(report["checkpointed"], 1)
        self.assertEqual(run.events[-1].type, "checkpointed")
        self.assertEqual(
            len([e for e in run.events if e.type == "checkpointed"]), 1
        )

        # New process, same store.
        clock.resume()
        service3 = RunService(
            store=service.store,
            config=ServiceConfig(
                workers=1, max_queue=4, per_tenant_capacity=100,
                per_tenant_refill=0.0, phase_latency_s=0.0,
                run_deadline_s=5.0, drain_timeout_s=0.05,
            ),
            clock=clock,
        )
        await service3.start()
        self.addAsyncCleanup(service3.drain, 0.01)
        await self.wait_idle(service3)
        resumed = service3.get(admission.run_id)
        self.assertEqual(resumed.status, "succeeded")
        self.assertEqual(service3.store.effects.count(), 1)
        started = [e for e in resumed.events if e.type == "started"]
        self.assertTrue(started[-1].data["resumed"])
        self.assertEqual(service3.metrics["resumed"], 1)

    async def test_queued_runs_survive_drain(self):
        clock = ControlledClock()
        clock.pause()
        service = await self.make_service(clock=clock, workers=1, max_queue=8)
        ids = []
        for key in ("a", "b", "c"):
            admission = await self.submit(service, key=key)
            ids.append(admission.run_id)
        report = await service.drain(0.0)
        self.assertEqual(report["queued_for_restart"], 3)
        for run_id in ids:
            self.assertEqual(service.get(run_id).status, "queued")

        clock.resume()
        service2 = RunService(
            store=service.store,
            config=ServiceConfig(
                workers=1, max_queue=8, per_tenant_capacity=100,
                per_tenant_refill=0.0, phase_latency_s=0.0,
                run_deadline_s=5.0, drain_timeout_s=0.05,
            ),
            clock=clock,
        )
        await service2.start()
        self.addAsyncCleanup(service2.drain, 0.01)
        await self.wait_idle(service2)
        for run_id in ids:
            self.assertEqual(service2.get(run_id).status, "succeeded")

    async def test_effect_is_not_replayed_after_crash_between_effect_and_checkpoint(self):
        service = await self.make_service()
        admission = await self.submit(service)
        await self.wait_idle(service)
        run = service.get(admission.run_id)
        self.assertEqual(service.store.effects.count(), 1)

        # Simulate a crash after the comment was posted but before the
        # checkpoint advanced from "hypothesize" to "publish_report".
        run.status = "queued"
        run.resumable = True
        run.result = None
        run.finished_at = None
        run.checkpoint["phase_index"] = 4

        clock = FakeClock()
        service2 = RunService(
            store=service.store,
            config=ServiceConfig(
                workers=1, max_queue=4, per_tenant_capacity=100,
                per_tenant_refill=0.0, phase_latency_s=0.0,
                run_deadline_s=5.0, drain_timeout_s=0.05,
            ),
            clock=clock,
        )
        await service2.start()
        self.addAsyncCleanup(service2.drain, 0.01)
        await self.wait_idle(service2)
        resumed = service2.get(admission.run_id)
        self.assertEqual(resumed.status, "succeeded")
        self.assertEqual(service2.store.effects.count(), 1)
        skipped = [e for e in resumed.events if e.type == "effect_skipped"]
        self.assertTrue(skipped)
        self.assertEqual(skipped[-1].data["phase"], "publish_report")

    async def test_drain_report_counts_completed_work(self):
        service = await self.make_service(workers=1)
        await self.submit(service, key="a")
        await self.wait_idle(service)
        report = await service.drain(0.05)
        self.assertFalse(report["timed_out"])
        self.assertEqual(report["queued_for_restart"], 0)

    async def test_drain_twice_is_safe(self):
        service = await self.make_service()
        await self.submit(service)
        await self.wait_idle(service)
        first = await service.drain(0.05)
        second = await service.drain(0.01)
        self.assertFalse(first["timed_out"])
        self.assertFalse(second["timed_out"])


class PrimitiveTests(unittest.TestCase):
    def test_token_bucket_burst_and_refill(self):
        clock = FakeClock()
        bucket = TokenBucket(capacity=2, refill_per_s=1, clock=clock)
        self.assertTrue(bucket.try_take()[0])
        self.assertTrue(bucket.try_take()[0])
        allowed, retry_after = bucket.try_take()
        self.assertFalse(allowed)
        self.assertEqual(retry_after, 1)
        clock.advance(1)
        self.assertTrue(bucket.try_take()[0])

    def test_token_bucket_zero_capacity(self):
        bucket = TokenBucket(capacity=0, refill_per_s=10, clock=FakeClock())
        allowed, retry_after = bucket.try_take()
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)

    def test_retry_budget_consumes_and_replenishes(self):
        budget = RetryBudget(capacity=1, refill_per_s=0, success_deposit=1, clock=FakeClock())
        self.assertTrue(budget.try_consume())
        self.assertFalse(budget.try_consume())
        self.assertEqual(budget.exhausted, 1)
        budget.on_success()
        self.assertTrue(budget.try_consume())

    def test_full_jitter_stays_within_bounds(self):
        policy = RetryPolicy(max_attempts=5, base_delay=0.1, max_delay=1.0)
        delays = [policy.delay_for_retry(i) for i in range(10)]
        self.assertTrue(all(0 <= d <= 1.0 for d in delays))
        self.assertGreater(len(set(delays)), 1)

    def test_retry_amplification_is_exponential(self):
        self.assertEqual(retry_amplification(5, 3), 243)
        self.assertEqual(retry_amplification(1, 3), 3)

    def test_adaptive_throttle_formula(self):
        self.assertAlmostEqual(adaptive_reject_probability(100, 40, 2), 20 / 101)
        self.assertEqual(adaptive_reject_probability(100, 60, 2), 0.0)
        self.assertEqual(adaptive_reject_probability(0, 0, 2), 0.0)

    def test_fingerprint_is_stable_and_payload_sensitive(self):
        a = RunRequest("t", "s", "R01", "k")
        b = RunRequest("other", "s", "R01", "k")
        c = RunRequest("t", "s", "R02", "k")
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertNotEqual(fingerprint(a), fingerprint(c))

    def test_effect_ledger(self):
        ledger = EffectLedger()
        self.assertFalse(ledger.contains("r1", "publish_report"))
        ledger.mark("r1", "publish_report")
        self.assertTrue(ledger.contains("r1", "publish_report"))
        self.assertEqual(ledger.count(), 1)

    def test_store_expiry(self):
        store = Store()
        from models import IdempotencyRecord

        store.put_idempotency("t", "k", IdempotencyRecord("f", "r", 0, 10))
        self.assertEqual(store.expire_idempotency(9), 0)
        self.assertEqual(store.expire_idempotency(10), 1)
        self.assertIsNone(store.get_idempotency("t", "k"))

    def test_config_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            ServiceConfig(workers=0)
        with self.assertRaises(ValueError):
            ServiceConfig(max_queue=0)
        with self.assertRaises(ValueError):
            ServiceConfig(event_retention=0)


if __name__ == "__main__":
    unittest.main()
