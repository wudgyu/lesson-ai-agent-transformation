"""Admission control primitives: token buckets and adaptive throttling.

Two different jobs are often both called "rate limiting":

- a *token bucket* enforces an average rate while allowing a bounded burst;
- a *concurrency limit* bounds how much expensive work is in flight at once.

Agent runs are long and memory-hungry, so the concurrency limit is usually the
more important protection.  The lab uses both.
"""

import math


class TokenBucket:
    """Classic token bucket: ``capacity`` burst, ``refill_per_s`` steady rate."""

    def __init__(self, capacity, refill_per_s, clock):
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        if refill_per_s < 0:
            raise ValueError("refill_per_s must be >= 0")
        self.capacity = float(capacity)
        self.refill_per_s = float(refill_per_s)
        self.clock = clock
        self.tokens = float(capacity)
        self.updated_at = clock.now()

    def _refill(self):
        now = self.clock.now()
        elapsed = max(0.0, now - self.updated_at)
        if elapsed:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_s)
            self.updated_at = now

    def try_take(self, amount=1.0):
        """Return ``(allowed, retry_after_seconds)``.

        ``retry_after`` is an integer because HTTP's ``Retry-After`` is in
        seconds; rounding up avoids telling a client to come back too early.
        """
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True, 0
        missing = amount - self.tokens
        if self.refill_per_s <= 0:
            return False, 60
        return False, max(1, int(math.ceil(missing / self.refill_per_s)))

    def deposit(self, amount):
        self._refill()
        self.tokens = min(self.capacity, self.tokens + max(0.0, amount))

    @property
    def available(self):
        self._refill()
        return self.tokens


class RetryBudget:
    """A token bucket that pays for retries, not for first attempts.

    AWS SDKs use the same idea: retries consume tokens; successes replenish
    them.  When the bucket is empty, failures are returned immediately instead
    of starting a retry storm.
    """

    def __init__(self, capacity=8.0, refill_per_s=0.5, success_deposit=0.25, clock=None):
        self.bucket = TokenBucket(capacity, refill_per_s, clock)
        self.success_deposit = success_deposit
        self.exhausted = 0

    def try_consume(self):
        allowed, _ = self.bucket.try_take(1.0)
        if not allowed:
            self.exhausted += 1
        return allowed

    def on_success(self):
        self.bucket.deposit(self.success_deposit)

    @property
    def available(self):
        return self.bucket.available


def adaptive_reject_probability(requests, accepts, k=2.0):
    """Google SRE client-side throttling formula.

    ``requests`` counts every attempt including locally rejected ones;
    ``accepts`` counts what the backend actually accepted.  With ``k=2`` a
    client keeps sending until it has twice as many attempts as accepts, then
    sheds an increasing fraction locally.

    See https://sre.google/sre-book/handling-overload/.
    """
    if requests <= 0:
        return 0.0
    numerator = requests - k * accepts
    if numerator <= 0:
        return 0.0
    return numerator / (requests + 1.0)
