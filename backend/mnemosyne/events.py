"""In-process pub/sub used to fan out backend events to WebSocket clients."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

Event = dict[str, Any]


class EventBus:
    """Each subscriber gets its own bounded queue. Slow consumers drop events
    rather than stalling the publisher."""

    def __init__(self, maxsize: int = 1000):
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: Event) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping event for slow subscriber: %s", event.get("type"))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
