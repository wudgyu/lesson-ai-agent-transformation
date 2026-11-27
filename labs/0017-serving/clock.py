"""Injectable clocks.

Using ``time.monotonic()`` / ``asyncio.sleep()`` directly makes retry, quota and
deadline tests slow and flaky.  Every time-dependent component takes a clock so
the tests can advance virtual time instead.

``ControlledClock`` is the one the shutdown tests use: it can be paused after a
phase so a drain happens at a known checkpoint instead of at a random moment.
"""

import asyncio
import time


class Clock:
    """The production clock."""

    def now(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class FakeClock(Clock):
    """Virtual time; sleeps complete immediately."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += max(0.0, seconds)

    async def sleep(self, seconds):
        self.advance(seconds)
        await asyncio.sleep(0)


class ControlledClock(FakeClock):
    """Virtual time that can be paused between phases.

    ``pause()`` blocks the next ``sleep``; ``resume()`` releases it.  Tests use
    this to make "drain while a run is in flight" deterministic.
    """

    def __init__(self):
        super().__init__()
        self._gate = asyncio.Event()
        self._gate.set()

    def pause(self):
        self._gate.clear()

    def resume(self):
        self._gate.set()

    def is_paused(self):
        return not self._gate.is_set()

    async def sleep(self, seconds):
        await self._gate.wait()
        await super().sleep(seconds)
