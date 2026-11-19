"""In-process async pub/sub for service events (consumed by the dashboard).

Each subscriber has a bounded queue. Publishing never blocks: when a subscriber's queue is
full, its oldest item is dropped (and counted in ``Subscription.dropped``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Generic, TypeVar

T = TypeVar("T")


class Subscription(Generic[T]):
    def __init__(self, bus: EventBus[T], maxsize: int) -> None:
        self._bus = bus
        self.queue: asyncio.Queue[T] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def _put(self, item: T) -> None:
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(item)

    async def get(self) -> T:
        return await self.queue.get()

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def __aiter__(self) -> AsyncIterator[T]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[T]:
        while True:
            yield await self.queue.get()


class EventBus(Generic[T]):
    def __init__(self, default_maxsize: int = 256) -> None:
        self._subs: list[Subscription[T]] = []
        self._default = default_maxsize

    def subscribe(self, maxsize: int | None = None) -> Subscription[T]:
        sub = Subscription(self, maxsize or self._default)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription[T]) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, item: T) -> None:
        for sub in list(self._subs):
            sub._put(item)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
