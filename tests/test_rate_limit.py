import asyncio
import json
from types import SimpleNamespace
import pytest

from pae.rate_limit import RequestPacer


class Clock:
    now = 0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


def test_pacing_includes_initial_cooldown_and_spaces_all_requests():
    clock = Clock()
    pacer = RequestPacer(4, clock=clock.time, sleep=clock.sleep)

    async def calls():
        starts = []
        for _ in range(9):
            await pacer.wait()
            starts.append(clock.now)
        return starts

    starts = asyncio.run(calls())
    assert starts[0] >= 60
    assert all(b - a > 15 for a, b in zip(starts, starts[1:]))
    assert all(sum(start <= other < start + 60 for other in starts) <= 4
               for start in starts)


def test_concurrent_waiters_do_not_burst_together():
    import time
    pacer = RequestPacer(4, initial_delay=0, period_seconds=0.08)

    async def calls():
        async def one():
            await pacer.wait()
            return time.monotonic()
        return sorted(await asyncio.gather(*(one() for _ in range(6))))

    starts = asyncio.run(calls())
    assert all(b - a >= 0.019 for a, b in zip(starts, starts[1:]))
