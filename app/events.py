from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from app.domain import EventRecord


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[EventRecord]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, event: EventRecord) -> None:
        async with self._lock:
            queues = tuple(self._subscribers.get(event.session_id, ()))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(event)

    @asynccontextmanager
    async def channel(self, session_id: str) -> AsyncIterator[asyncio.Queue[EventRecord]]:
        queue: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers[session_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers[session_id].discard(queue)
                if not self._subscribers[session_id]:
                    self._subscribers.pop(session_id, None)

    async def subscribe(self, session_id: str) -> AsyncIterator[EventRecord | None]:
        async with self.channel(session_id) as queue:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield None
