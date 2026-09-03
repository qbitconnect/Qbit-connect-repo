"""Marketing send queue (Phase 7 §21).

Mirrors the Phase 3 queue contract with a marketing namespace:
- Redis: LIST qbit:queue:marketing + ZSET …:scheduled (delayed retries)
- In-process asyncio fallback when REDIS_URL is unset (tests/dev)
DB-first durability: a CampaignRecipient row (QUEUED) is committed before the
enqueue, so broker loss never loses a send; the recovery sweep re-enqueues.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

QUEUE_NAME = "qbit:queue:marketing"
SCHEDULED_NAME = "qbit:queue:marketing:scheduled"


class MarketingQueue(Protocol):
    name: str

    async def enqueue(self, recipient_id: str, *, delay_seconds: float = 0) -> None: ...
    async def dequeue(self, timeout_seconds: float) -> str | None: ...
    async def pending_count(self) -> int: ...


class RedisMarketingQueue:
    name = "redis"

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def enqueue(self, recipient_id: str, *, delay_seconds: float = 0) -> None:
        if delay_seconds > 0:
            await self._redis.zadd(SCHEDULED_NAME, {recipient_id: time.time() + delay_seconds})
        else:
            await self._redis.rpush(QUEUE_NAME, recipient_id)

    async def dequeue(self, timeout_seconds: float) -> str | None:
        due = await self._redis.zrangebyscore(
            SCHEDULED_NAME, "-inf", time.time(), start=0, num=20
        )
        for recipient_id in due:
            removed = await self._redis.zrem(SCHEDULED_NAME, recipient_id)
            if removed:
                await self._redis.rpush(QUEUE_NAME, recipient_id)
        item = await self._redis.blpop(QUEUE_NAME, timeout=int(max(timeout_seconds, 0)))
        if not item:
            return None
        return item[1] if isinstance(item, tuple) else item

    async def pending_count(self) -> int:
        return int(await self._redis.llen(QUEUE_NAME))


class InProcessMarketingQueue:
    name = "inprocess"

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._scheduled: list[tuple[float, str]] = []
        self._wake = asyncio.Event()

    async def enqueue(self, recipient_id: str, *, delay_seconds: float = 0) -> None:
        if delay_seconds > 0:
            self._scheduled.append((time.monotonic() + delay_seconds, recipient_id))
        else:
            await self._queue.put(recipient_id)
            self._wake.set()

    async def dequeue(self, timeout_seconds: float) -> str | None:
        # Promote due scheduled retries first.
        now = time.monotonic()
        due = [item for item in self._scheduled if item[0] <= now]
        for item in due:
            self._scheduled.remove(item)
            await self._queue.put(item[1])
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=max(timeout_seconds, 0))
        except asyncio.TimeoutError:
            return None

    async def pending_count(self) -> int:
        return self._queue.qsize()


def build_marketing_queue(settings, redis_manager) -> MarketingQueue:
    """Redis when configured, in-process otherwise (Phase 2 rule §27)."""
    if settings.REDIS_URL and redis_manager is not None:
        client = redis_manager.get_client()
        if client is not None:
            return RedisMarketingQueue(client)
    return InProcessMarketingQueue()
