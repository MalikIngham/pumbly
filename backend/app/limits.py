import asyncio
import time
from collections import deque
from typing import Awaitable, Callable

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class QuotaExhausted(Exception):
    '''The daily Tumblr quota for this consumer key is used up.'''


class SlidingWindow:
    '''Tracks events in the last `period` seconds, allowing at most `limit`.'''

    def __init__(self, limit: int, period: float, clock: Clock = time.monotonic):
        self.limit = limit
        self.period = period
        self.clock = clock
        self.events: deque[float] = deque()

    def retry_after(self) -> float:
        '''Seconds until a slot is free, 0 if one is free now.'''
        now = self.clock()
        while self.events and self.events[0] <= now - self.period:
            self.events.popleft()
        if len(self.events) < self.limit:
            return 0
        return self.events[0] + self.period - now

    def record(self) -> None:
        self.events.append(self.clock())


class ApiQuota:
    '''Shared budget for every call made with our Tumblr consumer key.'''

    def __init__(
        self,
        per_hour: int,
        per_day: int,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ):
        self.hourly = SlidingWindow(per_hour, 3600, clock)
        self.daily = SlidingWindow(per_day, 86400, clock)
        self.sleep = sleep

    async def acquire(self) -> None:
        '''Waits out the hourly limit. Gives up on the daily one, since waiting
        could take most of a day.'''
        while True:
            if self.daily.retry_after() > 0:
                raise QuotaExhausted
            wait = self.hourly.retry_after()
            if wait <= 0:
                self.hourly.record()
                self.daily.record()
                return
            await self.sleep(wait)


class ClientLimiter:
    '''Per client sliding window, used to stop one visitor draining the quota.'''

    def __init__(self, limit: int, period: float, clock: Clock = time.monotonic):
        self.limit = limit
        self.period = period
        self.clock = clock
        self.windows: dict[str, SlidingWindow] = {}

    def hit(self, client: str) -> float:
        '''Records a hit. Returns 0 if allowed, otherwise seconds to wait.'''
        now = self.clock()
        self.windows = {
            key: window
            for key, window in self.windows.items()
            if window.events and window.events[-1] > now - self.period
        }
        window = self.windows.setdefault(
            client, SlidingWindow(self.limit, self.period, self.clock)
        )
        wait = window.retry_after()
        if wait > 0:
            return wait
        window.record()
        return 0
