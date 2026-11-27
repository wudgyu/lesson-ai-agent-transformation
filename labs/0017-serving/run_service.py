"""The run service: admission, execution, event log and shutdown.

This file is the core of the lesson.  The HTTP layer in ``api.py`` is a thin
adapter; every interesting decision is here:

    submit -> idempotency -> quota -> bounded queue -> worker
           -> deadline / retry / cancel / checkpoint -> event log + result

Two invariants drive the design:

1. **The result store is the source of truth; the stream is a view.**  A client
   can lose the SSE connection at any byte and still get the answer from
   ``GET /v1/runs/{id}``.
2. **Cancellation is cooperative at phase boundaries.**  A task can only be
   cancelled at an ``await``, and an effect must never be half-applied.  The
   checkpoint is written after the effect, and an idempotency ledger makes the
   replay harmless.
"""

import asyncio
import random
from dataclasses import dataclass
from typing import Optional

from agent import RepoMedicAgent
from clock import Clock
from errors import (
    DeadlineExceeded,
    NonRetryableError,
    RetryBudgetExhausted,
    RunCancelled,
    RunNotFound,
)
from limiter import RetryBudget, TokenBucket
from models import Admission, Event, IdempotencyRecord, Run, Subscription
from retry import Deadline, RetryPolicy
from store import Store, fingerprint

OVERFLOW = object()


@dataclass
class ServiceConfig:
    # Workers == maximum concurrent runs in this process.
    workers: int = 4
    # Bounded queue.  When full, shed immediately with 503 instead of growing
    # memory without limit.
    max_queue: int = 8
    # Per-tenant token bucket: burst and steady-state runs/second.
    per_tenant_capacity: int = 4
    per_tenant_refill: float = 0.5
    idempotency_ttl_s: float = 24 * 3600
    # Ring buffer of events kept in memory.  A real service would use a durable
    # log or a stream store; the bound is what makes replay gaps visible.
    event_retention: int = 64
    subscriber_queue: int = 16
    run_deadline_s: float = 10.0
    phase_latency_s: float = 0.05
    retry_attempts: int = 3
    retry_base_s: float = 0.05
    retry_max_s: float = 0.4
    # Lab-only seed: makes full-jitter output reproducible.  Production should
    # use the system RNG so retries from different processes do not align.
    retry_seed: int = 144
    retry_budget_capacity: float = 8.0
    retry_budget_refill: float = 0.5
    drain_timeout_s: float = 3.0
    # SSE keep-alive.  The HTTP adapter exposes this; tests set None to avoid
    # waiting on real time.
    heartbeat_s: Optional[float] = 15.0

    def __post_init__(self):
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if self.max_queue < 1:
            raise ValueError("max_queue must be >= 1")
        if self.event_retention < 1:
            raise ValueError("event_retention must be >= 1")


class RunService:
    def __init__(self, store=None, config=None, clock=None):
        self.store = store or Store()
        self.config = config or ServiceConfig()
        self.clock = clock or Clock()
        self.metrics = {
            "submitted": 0,
            "accepted": 0,
            "duplicate": 0,
            "in_progress": 0,
            "rate_limited": 0,
            "queue_full": 0,
            "draining_rejects": 0,
            "started": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "checkpointed": 0,
            "resumed": 0,
            "events_emitted": 0,
            "slow_consumers": 0,
            "max_inflight": 0,
        }
        self._queue = None
        self._workers = []
        self._running = {}  # run_id -> asyncio.Task
        self._subscribers = {}  # run_id -> {sub_id: Subscription}
        self._next_sub = 1
        self._buckets = {}
        self._inflight = 0
        self._started = False
        self._draining = False
        self._retry_budget = RetryBudget(
            capacity=self.config.retry_budget_capacity,
            refill_per_s=self.config.retry_budget_refill,
            clock=self.clock,
        )

    # ------------------------------------------------------------------ lifecycle

    async def start(self):
        if self._started:
            return
        self._queue = asyncio.Queue(maxsize=self.config.max_queue)
        self._started = True
        self._draining = False
        for index in range(self.config.workers):
            self._workers.append(asyncio.create_task(self._worker(index)))

        # Resume anything that was queued or checkpointed by a previous
        # process.  In production this scan would be a database query.
        for run in list(self.store.runs.values()):
            if run.status != "queued":
                continue
            if run.cancel_requested:
                # A crash can happen after the cancel flag is set but before
                # the terminal events are written.  Finish the cancellation.
                self._finish_cancelled(run)
                continue
            if self._queue.full():
                self.metrics["queue_full"] += 1
                continue
            run.resumable = False
            self._queue.put_nowait(run.run_id)
            self.metrics["resumed"] += 1
            self._emit(
                run,
                "resumed",
                {"phase_index": run.checkpoint.get("phase_index", 0)},
            )

    def begin_shutdown(self):
        """Stop accepting new work; readiness must fail immediately."""
        self._draining = True

    async def drain(self, timeout=None):
        """Stop accepting, let in-flight work finish, then checkpoint the rest."""
        self.begin_shutdown()
        timeout = self.config.drain_timeout_s if timeout is None else timeout
        inflight_at_start = self._inflight
        succeeded_before = self.metrics["succeeded"]
        checkpointed_before = self.metrics["checkpointed"]

        timed_out = False
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True

        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

        # A worker may have been cancelled between "status = running" and the
        # checkpoint write.  The last completed phase is still valid, so put the
        # run back in the queue for the next process.
        for run in self.store.runs.values():
            if run.status == "running":
                run.status = "queued"
                run.resumable = True
                self._emit(
                    run,
                    "checkpointed",
                    {
                        "reason": "shutdown",
                        "phase_index": run.checkpoint.get("phase_index", 0),
                    },
                )
                self.metrics["checkpointed"] += 1

        self._started = False
        return {
            "timed_out": timed_out,
            "inflight_at_start": inflight_at_start,
            "completed": self.metrics["succeeded"] - succeeded_before,
            "checkpointed": self.metrics["checkpointed"] - checkpointed_before,
            "queued_for_restart": sum(
                1 for r in self.store.runs.values() if r.status == "queued"
            ),
            "metrics": self.snapshot_metrics(),
        }

    def ready(self):
        return self._started and not self._draining

    def snapshot_metrics(self):
        out = dict(self.metrics)
        out["inflight"] = self._inflight
        out["queue_depth"] = self._queue.qsize() if self._queue is not None else 0
        out["retry_tokens"] = round(self._retry_budget.available, 2)
        out["effects"] = self.store.effects.count()
        return out

    # ------------------------------------------------------------------ admission

    async def submit(self, request):
        """Create or deduplicate a run.

        The order is deliberate: idempotency before quota (a retry must not burn
        a token), quota before queue (a tenant cannot fill the global queue),
        queue before creating state (rejected work leaves no orphan record).
        """
        self.metrics["submitted"] += 1
        if not request.idempotency_key:
            return Admission(False, 400, "missing_idempotency_key", "Idempotency-Key is required")
        if not self._started or self._draining:
            self.metrics["draining_rejects"] += 1
            return Admission(False, 503, "draining", "server is shutting down")

        now = self.clock.now()
        self.store.expire_idempotency(now)
        record = self.store.get_idempotency(request.tenant, request.idempotency_key)
        fp = fingerprint(request)
        if record is not None:
            if record.fingerprint != fp:
                return Admission(
                    False,
                    422,
                    "idempotency_conflict",
                    "same Idempotency-Key with a different payload",
                    run_id=record.run_id,
                )
            existing = self.store.get_run(record.run_id)
            if existing is not None:
                if existing.is_terminal():
                    self.metrics["duplicate"] += 1
                    return Admission(
                        False,
                        200,
                        "duplicate",
                        "run already completed",
                        run_id=existing.run_id,
                    )
                self.metrics["in_progress"] += 1
                return Admission(
                    False,
                    409,
                    "in_progress",
                    "run with this key is still in progress; attach to it",
                    run_id=existing.run_id,
                )

        bucket = self._buckets.get(request.tenant)
        if bucket is None:
            bucket = TokenBucket(
                self.config.per_tenant_capacity,
                self.config.per_tenant_refill,
                self.clock,
            )
            self._buckets[request.tenant] = bucket
        allowed, retry_after = bucket.try_take(1.0)
        if not allowed:
            self.metrics["rate_limited"] += 1
            return Admission(
                False,
                429,
                "tenant_rate_limited",
                "tenant quota exhausted",
                retry_after=retry_after,
            )

        if self._queue.full():
            # Rejected work should not consume quota: the client did nothing
            # wrong, the shared resource is busy.
            bucket.deposit(1.0)
            self.metrics["queue_full"] += 1
            return Admission(False, 503, "queue_full", "service is at capacity")

        run_id = self.store.next_run_id()
        run = Run(
            run_id=run_id,
            request=request,
            created_at=now,
            checkpoint={"phase_index": 0, "findings": []},
            resumable=True,
        )
        self.store.add_run(run)
        self.store.put_idempotency(
            request.tenant,
            request.idempotency_key,
            IdempotencyRecord(
                fingerprint=fp,
                run_id=run_id,
                created_at=now,
                expires_at=now + self.config.idempotency_ttl_s,
            ),
        )
        self._emit(run, "queued", {"queue_depth": self._queue.qsize() + 1})
        self._queue.put_nowait(run_id)
        self.metrics["accepted"] += 1
        return Admission(True, 202, "accepted", "run accepted", run_id=run_id)

    def get(self, run_id):
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFound("unknown run %s" % run_id, {"run_id": run_id})
        return run

    def cancel(self, run_id):
        run = self.get(run_id)
        if run.is_terminal():
            return run
        run.cancel_requested = True
        task = self._running.get(run_id)
        if task is not None:
            task.cancel()
        else:
            # Still queued: finalize now; the worker will skip it when dequeued.
            self._finish_cancelled(run)
        return run

    async def cancel_and_wait(self, run_id):
        """Cancel and wait for the worker to finish its cleanup.

        The HTTP caller gets a truthful final status instead of "running" with
        a cancel flag that it has to poll.  Cancellation itself is cooperative;
        this only waits for the already-running cleanup path.
        """
        run = self.cancel(run_id)
        task = self._running.get(run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if not run.is_terminal():
            self._finish_cancelled(run)
        return run

    # ------------------------------------------------------------------ workers

    async def _worker(self, index):
        while True:
            run_id = await self._queue.get()
            try:
                run = self.store.get_run(run_id)
                if run is None or run.is_terminal():
                    continue
                if run.cancel_requested:
                    self._finish_cancelled(run)
                    continue
                if self._draining:
                    run.status = "queued"
                    run.resumable = True
                    continue
                await self._process(run)
            except asyncio.CancelledError:
                raise
            finally:
                self._queue.task_done()

    async def _process(self, run):
        run.status = "running"
        run.started_at = self.clock.now()
        run.resumable = False
        self._inflight += 1
        self.metrics["started"] += 1
        self.metrics["max_inflight"] = max(self.metrics["max_inflight"], self._inflight)
        self._emit(
            run,
            "started",
            {
                "phase_index": run.checkpoint.get("phase_index", 0),
                "resumed": run.checkpoint.get("phase_index", 0) > 0,
            },
        )
        child = asyncio.create_task(self._execute(run))
        self._running[run.run_id] = child
        try:
            await child
        except asyncio.CancelledError:
            # The child can be cancelled before its first scheduling turn, in
            # which case its own ``except CancelledError`` never runs.  The
            # service still has to finalize the run.
            if run.cancel_requested and not self._draining:
                self._finish_cancelled(run)
                return
            self._pause_or_cancel(run)
            raise
        finally:
            self._running.pop(run.run_id, None)
            self._inflight -= 1

    async def _execute(self, run):
        deadline = Deadline(self.clock, self.config.run_deadline_s)
        agent = RepoMedicAgent(
            clock=self.clock,
            ledger=self.store.effects,
            phase_latency=self.config.phase_latency_s,
            retry_policy=RetryPolicy(
                max_attempts=self.config.retry_attempts,
                base_delay=self.config.retry_base_s,
                max_delay=self.config.retry_max_s,
                rng=random.Random(self.config.retry_seed),
            ),
            retry_budget=self._retry_budget,
        )

        def emit(event_type, data):
            self._emit(run, event_type, data)

        def should_stop():
            return run.cancel_requested or self._draining

        try:
            result = await agent.run(run, emit, should_stop, deadline)
            run.result = result
            run.status = "succeeded"
            run.finished_at = self.clock.now()
            run.resumable = False
            self.metrics["succeeded"] += 1
            self._emit(run, "done", {"status": run.status, "result": result})
        except DeadlineExceeded:
            run.status = "failed"
            run.finished_at = self.clock.now()
            run.resumable = False
            run.error = {
                "code": "deadline_exceeded",
                "message": "run deadline exceeded",
                "phase_index": run.checkpoint.get("phase_index", 0),
            }
            run.result = {
                "partial": True,
                "evidence": list(run.checkpoint.get("findings", [])),
            }
            self.metrics["failed"] += 1
            self._emit(run, "error", run.error)
            self._emit(run, "done", {"status": run.status, "error": run.error})
        except RetryBudgetExhausted as exc:
            run.status = "failed"
            run.finished_at = self.clock.now()
            run.resumable = False
            run.error = {
                "code": "retry_budget_exhausted",
                "message": str(exc),
                "phase_index": run.checkpoint.get("phase_index", 0),
            }
            run.result = {
                "partial": True,
                "evidence": list(run.checkpoint.get("findings", [])),
            }
            self.metrics["failed"] += 1
            self._emit(run, "error", run.error)
            self._emit(run, "done", {"status": run.status, "error": run.error})
        except NonRetryableError as exc:
            run.status = "failed"
            run.finished_at = self.clock.now()
            run.resumable = False
            run.error = {
                "code": "non_retryable",
                "message": str(exc),
                "phase_index": run.checkpoint.get("phase_index", 0),
            }
            self.metrics["failed"] += 1
            self._emit(run, "error", run.error)
            self._emit(run, "done", {"status": run.status, "error": run.error})
        except RunCancelled:
            self._pause_or_cancel(run)
        except asyncio.CancelledError:
            if run.cancel_requested:
                # A user cancel stops this run, not the worker loop.
                self._finish_cancelled(run)
                return
            self._pause_or_cancel(run)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            run.status = "failed"
            run.finished_at = self.clock.now()
            run.resumable = False
            run.error = {"code": "internal_error", "message": str(exc)}
            self.metrics["failed"] += 1
            self._emit(run, "error", run.error)
            self._emit(run, "done", {"status": run.status, "error": run.error})

    def _pause_or_cancel(self, run):
        if run.cancel_requested:
            self._finish_cancelled(run)
            return
        if run.status == "queued" and run.resumable:
            # Both _execute and _process can observe the same cancellation; the
            # checkpoint (and its metric) must be written once.
            return
        # Draining: keep the completed phases, put the run back for next boot.
        run.status = "queued"
        run.resumable = True
        run.finished_at = None
        self.metrics["checkpointed"] += 1
        self._emit(
            run,
            "checkpointed",
            {
                "reason": "shutdown",
                "phase_index": run.checkpoint.get("phase_index", 0),
            },
        )

    def _finish_cancelled(self, run):
        if run.is_terminal():
            return
        run.status = "cancelled"
        run.finished_at = self.clock.now()
        run.resumable = False
        run.error = {"code": "cancelled", "message": "cancelled by caller"}
        self.metrics["cancelled"] += 1
        self._emit(run, "cancelled", {"phase_index": run.checkpoint.get("phase_index", 0)})
        self._emit(run, "done", {"status": run.status, "error": run.error})

    # ------------------------------------------------------------------ events

    def _emit(self, run, event_type, data):
        run.last_seq += 1
        event = Event(run.last_seq, event_type, data, self.clock.now())
        run.events.append(event)
        if len(run.events) > self.config.event_retention:
            del run.events[: len(run.events) - self.config.event_retention]
        self.metrics["events_emitted"] += 1

        subscribers = self._subscribers.get(run.run_id, {})
        for sub_id, sub in list(subscribers.items()):
            if sub.closed:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # SSE has no flow control.  A slow consumer must be disconnected
                # and allowed to resume from Last-Event-ID, not buffered forever.
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    sub.queue.put_nowait(OVERFLOW)
                except asyncio.QueueFull:
                    pass
                sub.closed = True
                subscribers.pop(sub_id, None)
                self.metrics["slow_consumers"] += 1

    async def subscribe(self, run_id, last_event_id=None, heartbeat_s=None):
        """Async generator: replay then live events, ending at ``done``.

        Registration happens before the replay snapshot is copied.  An event
        emitted between registration and replay is therefore delivered twice;
        the ``seq <= last_yielded`` check removes the duplicate.
        """
        run = self.get(run_id)
        sub_id = self._next_sub
        self._next_sub += 1
        sub = Subscription(sub_id=sub_id, queue=asyncio.Queue(maxsize=self.config.subscriber_queue))
        self._subscribers.setdefault(run_id, {})[sub_id] = sub

        first_seq = run.events[0].seq if run.events else run.last_seq + 1
        # A brand-new subscriber with no Last-Event-ID also has a gap if the
        # beginning of the log has already been evicted.
        gap = first_seq > 1 and (
            last_event_id is None or last_event_id < first_seq - 1
        )
        replay = list(run.events)
        if last_event_id is not None:
            replay = [event for event in replay if event.seq > last_event_id]
        last_yielded = last_event_id if last_event_id is not None else 0
        # The terminal check must reflect the moment of subscription.  The run
        # can finish while the client is still consuming the replay; in that
        # case the live queue already holds the newer events (including done),
        # so returning here would silently drop them.
        terminal_at_subscribe = run.is_terminal()

        try:
            if gap:
                # The requested id has fallen out of the ring buffer.  A
                # snapshot compresses the missing prefix; it must carry an id
                # just before the first retained event so the client can keep
                # resuming monotonically.
                snapshot = Event(
                    first_seq - 1,
                    "snapshot",
                    {
                        "run_id": run.run_id,
                        "status": run.status,
                        "last_event_id": run.last_seq,
                        "result": run.result,
                        "error": run.error,
                        "phase_index": run.checkpoint.get("phase_index", 0),
                    },
                    self.clock.now(),
                )
                last_yielded = snapshot.seq
                yield snapshot

            for event in replay:
                if event.seq <= last_yielded:
                    continue
                last_yielded = event.seq
                yield event

            if terminal_at_subscribe:
                return

            while True:
                if heartbeat_s:
                    try:
                        item = await asyncio.wait_for(
                            sub.queue.get(), timeout=heartbeat_s
                        )
                    except asyncio.TimeoutError:
                        # A ping is a comment, not an event: it must not advance
                        # Last-Event-ID or a client could skip real events.
                        yield Event(run.last_seq, "ping", {}, self.clock.now())
                        continue
                else:
                    item = await sub.queue.get()

                if item is OVERFLOW:
                    yield Event(
                        last_yielded,
                        "overflow",
                        {
                            "reason": "slow_consumer",
                            "last_event_id": last_yielded,
                        },
                        self.clock.now(),
                    )
                    return
                if item.seq <= last_yielded:
                    continue
                last_yielded = item.seq
                yield item
                if item.type == "done":
                    return
        finally:
            subscribers = self._subscribers.get(run_id)
            if subscribers is not None:
                subscribers.pop(sub_id, None)
                if not subscribers:
                    self._subscribers.pop(run_id, None)
