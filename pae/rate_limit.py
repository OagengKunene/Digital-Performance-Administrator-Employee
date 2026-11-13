"""Conservative per-model pacing for this application's single API process."""
import asyncio
import threading
import time


class RequestPacer:
    def __init__(self, requests_per_minute, *, initial_delay=60,
                 period_seconds=60, clock=time.monotonic, sleep=asyncio.sleep):
        self.interval = period_seconds / requests_per_minute * 1.05
        self.clock, self.sleep = clock, sleep
        self.next_start = clock() + initial_delay
        self.lock = threading.Lock()

    async def wait(self):
        started = self.clock()
        while True:
            with self.lock:
                now = self.clock()
                delay = self.next_start - now
                if delay <= 0:
                    self.next_start = now + self.interval
                    return max(0, now - started)
            # Recheck actual start time after waking; delayed/concurrent callers
            # cannot all consume old reservations and burst together.
            await self.sleep(delay)


_pacers = {}
_lock = threading.Lock()


def model_pacer(model, rpm, *, initial_delay=60):
    with _lock:
        key = (model, rpm)
        if key not in _pacers:
            _pacers[key] = RequestPacer(rpm, initial_delay=initial_delay)
        return _pacers[key]


class ModelCallBudgetExceeded(Exception):
    pass
