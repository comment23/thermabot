"""Server-sent events (D§11): `state`, `snapshot`, `plan`, `alert`. In-process fan-out."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

KEEPALIVE_S = 15.0
QUEUE_MAX = 100


def frame(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


class Broker:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(QUEUE_MAX)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: object) -> None:
        msg = frame(event, data)
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # slow client: drop it; it falls back to polling
                self._subs.discard(q)

    @property
    def subscribers(self) -> int:
        return len(self._subs)


async def stream(
    broker: Broker,
    first: str,
    disconnected: Callable[[], Awaitable[bool]],
    keepalive_s: float = KEEPALIVE_S,
) -> AsyncIterator[str]:
    """Initial frame, then broker messages; `: ka` comment lines keep proxies open."""
    q = broker.subscribe()
    try:
        yield first
        while not await disconnected():
            try:
                yield await asyncio.wait_for(q.get(), keepalive_s)
            except TimeoutError:
                yield ": ka\n\n"
    finally:
        broker.unsubscribe(q)
