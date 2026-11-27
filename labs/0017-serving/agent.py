"""A deterministic RepoMedic agent for the serving lab.

There is no model call here on purpose.  The serving problems we want to study
(deadlines, retries, cancellation, checkpoints, duplicate side effects) are
properties of the *execution harness*, not of the model.  A deterministic agent
lets us reproduce each failure exactly.

Scenarios are keyed by incident id:

- ``R01`` normal run
- ``R02`` logs tool fails twice with a retryable error
- ``R03`` hypothesize step hangs past the deadline
- ``R04`` publish step fails permanently (auth)
- ``R05`` normal run
- ``R06`` code search fails once, then succeeds
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set

from errors import FatalStepError, RetryableError, RunCancelled
from retry import RetryPolicy, retry_call

PHASES = (
    "intake",
    "retrieve_logs",
    "retrieve_code",
    "hypothesize",
    "publish_report",
)


@dataclass
class Scenario:
    flaky: Dict[str, int] = field(default_factory=dict)  # phase -> failures before success
    hang: Set[str] = field(default_factory=set)
    fatal: Set[str] = field(default_factory=set)
    hang_seconds: float = 10.0


SCENARIOS: Dict[str, Scenario] = {
    "R01": Scenario(),
    "R02": Scenario(flaky={"retrieve_logs": 2}),
    "R03": Scenario(hang={"hypothesize"}),
    "R04": Scenario(fatal={"publish_report"}),
    "R05": Scenario(),
    "R06": Scenario(flaky={"retrieve_code": 1}),
}

PHASE_FINDINGS = {
    "intake": "alert: p99 latency 2.4s, error rate 8% on checkout-api",
    "retrieve_logs": "log: NullPointerException at OrderMapper.java:142 (x312)",
    "retrieve_code": "code: order.discount may be null when coupon is absent",
    "hypothesize": "hypothesis: missing null guard in OrderMapper.toDto",
    "publish_report": "report: open fix branch, attach 3 evidence links",
}


class RepoMedicAgent:
    def __init__(self, clock, ledger, phase_latency=0.05, retry_policy=None, retry_budget=None):
        self.clock = clock
        self.ledger = ledger
        self.phase_latency = phase_latency
        self.retry_policy = retry_policy or RetryPolicy(
            max_attempts=3, base_delay=0.05, max_delay=0.4
        )
        self.retry_budget = retry_budget
        self._failures = {}  # (run_id, phase) -> remaining injected failures

    def _scenario(self, incident):
        key = (incident or "")[:3].upper()
        return SCENARIOS.get(key, Scenario())

    def _should_fail(self, run_id, phase, scenario):
        key = (run_id, phase)
        if key not in self._failures:
            self._failures[key] = scenario.flaky.get(phase, 0)
        if self._failures[key] > 0:
            self._failures[key] -= 1
            return True
        return False

    async def _run_phase(self, run, phase, scenario, emit):
        run_id = run.run_id

        async def step():
            if phase in scenario.fatal:
                raise FatalStepError("%s: permission denied by upstream" % phase)
            if self._should_fail(run_id, phase, scenario):
                raise RetryableError("%s: upstream returned 503" % phase)
            # An effect is marked before the checkpoint is written.  If the
            # process dies in between, the replay skips the effect but still
            # advances the checkpoint: at-least-once execution, one effect.
            if phase == "publish_report" and not self.ledger.contains(run_id, phase):
                self.ledger.mark(run_id, phase)
                emit("effect", {"phase": phase, "effect": "comment_posted"})
            return PHASE_FINDINGS[phase]

        def on_retry(attempt, delay, exc):
            emit(
                "retry",
                {
                    "phase": phase,
                    "retry": attempt,
                    "delay_ms": int(delay * 1000),
                    "error": str(exc),
                },
            )

        value, attempts = await retry_call(
            step,
            policy=self.retry_policy,
            clock=self.clock,
            budget=self.retry_budget,
            on_retry=on_retry,
        )
        run.attempts += attempts
        return value, attempts

    async def run(self, run, emit, should_stop, deadline):
        """Execute (or resume) one run.

        ``emit(type, data)`` is synchronous; the event bus assigns sequence
        numbers.  ``should_stop()`` is checked between phases so a cancel is
        honoured at a step boundary, not in the middle of an effect.
        """
        scenario = self._scenario(run.request.incident)
        checkpoint = run.checkpoint
        start_index = int(checkpoint.get("phase_index", 0))
        findings: List[str] = checkpoint.setdefault("findings", [])

        if start_index >= len(PHASES):
            return self._result(run, findings)

        for index in range(start_index, len(PHASES)):
            phase = PHASES[index]
            if should_stop():
                raise RunCancelled("stop requested before %s" % phase)
            deadline.raise_if_expired()

            # Every phase starts with a yield point: this is what makes the
            # async task cancellable (a task can only be cancelled at an await).
            await self.clock.sleep(self.phase_latency)

            if phase in scenario.hang:
                # Simulate a tool that ignores cancellation until its own
                # timeout.  The run deadline is the backstop.
                await self.clock.sleep(scenario.hang_seconds)
                deadline.raise_if_expired()

            if should_stop():
                raise RunCancelled("stop requested during %s" % phase)

            emit("phase_started", {"phase": phase, "index": index})

            if self.ledger.contains(run.run_id, phase):
                emit("effect_skipped", {"phase": phase, "reason": "already applied"})
                attempts = 0
            else:
                finding, attempts = await self._run_phase(run, phase, scenario, emit)
                findings.append(finding)

            checkpoint["phase_index"] = index + 1
            checkpoint["last_completed_phase"] = phase
            emit(
                "phase_finished",
                {
                    "phase": phase,
                    "index": index,
                    "attempts": attempts,
                    "phase_index": checkpoint["phase_index"],
                },
            )

        return self._result(run, findings)

    def _result(self, run, findings):
        incident = (run.request.incident or "").upper()
        if incident.startswith("R03"):
            root_cause = "worker pool exhausted by a slow downstream call"
        elif incident.startswith("R02"):
            root_cause = "log pipeline backpressure masked the NPE"
        else:
            root_cause = "missing null guard in OrderMapper.toDto"
        return {
            "run_id": run.run_id,
            "service": run.request.service,
            "root_cause": root_cause,
            "confidence": 0.86,
            "evidence": list(findings),
            "actions": ["open fix branch", "post report"],
        }
