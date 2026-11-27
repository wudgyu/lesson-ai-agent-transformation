"""Retry policy, full jitter and deadlines.

Three rules drive everything here:

1. Retry only ``RetryableError``.  A 400 or a policy denial will not heal.
2. Back off exponentially and add *full jitter*; clients that failed together
   must not retry in lockstep.
3. Retries spend from a shared budget.  A retry is extra load on a dependency
   that is already unhealthy; it is not a free local decision.
"""

import random

from errors import RetryableError, RetryBudgetExhausted


class Deadline:
    """An absolute deadline, not a per-call timeout.

    Passing the *remaining* budget down the stack prevents the classic bug where
    ten nested calls each get a 10-second timeout and the request takes 100
    seconds.
    """

    def __init__(self, clock, seconds):
        self.clock = clock
        self.seconds = float(seconds)
        self.expires_at = clock.now() + self.seconds

    def remaining(self):
        return max(0.0, self.expires_at - self.clock.now())

    def expired(self):
        return self.remaining() <= 0.0

    def raise_if_expired(self):
        if self.expired():
            from errors import DeadlineExceeded

            raise DeadlineExceeded("run deadline exceeded")


class RetryPolicy:
    """Capped exponential backoff with full jitter.

    For retry number ``n`` (0-based), ``bound = min(max_delay, base * 2**n)``
    and the actual delay is ``uniform(0, bound)``.  Full jitter trades a little
    latency for a much flatter retry wave.
    """

    def __init__(self, max_attempts=3, base_delay=0.1, max_delay=2.0, rng=None):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = int(max_attempts)
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.rng = rng or random.Random()

    def delay_for_retry(self, retry_index):
        """Delay before retry ``retry_index`` (0 means the first retry)."""
        bound = min(self.max_delay, self.base_delay * (2 ** retry_index))
        return self.rng.uniform(0.0, bound)


async def retry_call(fn, policy, clock, budget=None, on_retry=None):
    """Call ``fn`` with retries; return ``(value, attempts)``.

    ``on_retry(attempt_number, delay, error)`` is called before sleeping.
    ``attempt_number`` is 1-based and counts retries, not attempts.
    """
    last_error = None
    for attempt in range(policy.max_attempts):
        try:
            value = await fn()
            if budget is not None:
                budget.on_success()
            return value, attempt + 1
        except RetryableError as exc:
            last_error = exc
            if attempt == policy.max_attempts - 1:
                break
            if budget is not None and not budget.try_consume():
                raise RetryBudgetExhausted(
                    "retry budget exhausted after %d attempt(s)" % (attempt + 1)
                ) from exc
            delay = policy.delay_for_retry(attempt)
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc)
            await clock.sleep(delay)
    raise last_error


def retry_amplification(layers, attempts_per_layer):
    """How many leaf calls one user request creates with naive nested retries.

    Three attempts at each of five layers is 3**5 = 243, not 15.  This is why
    exactly one layer should own retries.
    """
    if layers < 0 or attempts_per_layer < 0:
        raise ValueError("layers and attempts must be >= 0")
    return attempts_per_layer ** layers
