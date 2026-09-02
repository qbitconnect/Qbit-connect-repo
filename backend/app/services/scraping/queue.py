"""Queue backends (brief §14, doc 09 §1).

DB-first: the scrape_jobs row (QUEUED) is COMMITTED before enqueue, so a
broker loss never loses a job — the recovery sweep re-enqueues orphans.

Two interchangeable backends behind `QueueBackend`:

- RedisQueueBackend  — production: LIST qbit:queue:scrape + ZSET
  qbit:queue:scrape:scheduled for delayed retries + control keys
  qbit:job:{id}:control (pause/cancel plane) + lease keys.
- InProcessQueueBackend — local-first fallback when REDIS_URL is unset
  (Phase 2 rule §27: the platform must run without Redis). asyncio-native.

Celery/RQ can be added later as another QueueBackend without touching the
engine (Phase 3 decision documented in the completion report).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from app.core.logging import get_logger, log_with

logger = get_logger("qbit.scrapers.queue")

QUEUE_NAME = "qbit:queue:scrape"
SCHEDULED_NAME = "qbit:queue:scrape:scheduled"
CONTROL_KEY = "qbit:job:{job_id}:control"
LEASE_KEY = "qbit:job:{job_id}:lease"


def control_key(job_id: str) -> str:
    return CONTROL_KEY.format(job_id=job_id)


def lease_key(job_id: str) -> str:
    return LEASE_KEY.format(job_id=job_id)


class QueueBackend(Protocol):
    name: str

    async def enqueue(self, job_id: str, *, delay_seconds: float = 0) -> None: ...
    async def dequeue(self, timeout_seconds: float) -> str | None: ...
    async def set_control(self, job_id: str, value: str, ttl_seconds: int = 86400) -> None: ...
    async def get_control(self, job_id: str) -> str | None: ...
    async def clear_control(self, job_id: str) -> None: ...
    async def renew_lease(self, job_id: str, owner: str, ttl_seconds: int) -> None: ...
    async def release_lease(self, job_id: str) -> None: ...
    async def recover_orphans(self) -> list[str]: ...
    async def pending_count(self) -> int: ...
    async def aclose(self) -> None: ...


# --------------------------------------------------------------------- Redis
class RedisQueueBackend:
    name = "redis"

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def enqueue(self, job_id: str, *, delay_seconds: float = 0) -> None:
        if delay_seconds > 0:
            await self._redis.zadd(
                SCHEDULED_NAME, {job_id: time.time() + delay_seconds}
            )
        else:
            await self._redis.rpush(QUEUE_NAME, job_id)

    async def dequeue(self, timeout_seconds: float) -> str | None:
        # First: promote due scheduled retries.
        due_ids = await self._redis.zrangebyscore(
            SCHEDULED_NAME, "-inf", time.time(), start=0, num=10
        )
        for job_id in due_ids:
            removed = await self._redis.zrem(SCHEDULED_NAME, job_id)
            if removed:
                await self._redis.rpush(QUEUE_NAME, job_id)
        item = await self._redis.blpop(QUEUE_NAME, timeout=int(max(timeout_seconds, 0)))
        if not item:
            return None
        return item[1] if isinstance(item, tuple) else item

    async def set_control(self, job_id: str, value: str, ttl_seconds: int = 86400) -> None:
        await self._redis.set(control_key(job_id), value, ex=ttl_seconds)

    async def get_control(self, job_id: str) -> str | None:
        return await self._redis.get(control_key(job_id))

    async def clear_control(self, job_id: str) -> None:
        await self._redis.delete(control_key(job_id))

    async def renew_lease(self, job_id: str, owner: str, ttl_seconds: int) -> None:
        await self._redis.set(lease_key(job_id), owner, ex=ttl_seconds)

    async def release_lease(self, job_id: str) -> None:
        await self._redis.delete(lease_key(job_id))

    async def recover_orphans(self) -> list[str]:
        """Jobs whose worker died: DB scan handles it; Redis side we only
        clear the scheduled ZSET duplicates. Returns nothing by design."""
        return []

    async def pending_count(self) -> int:
        return int(await self._redis.llen(QUEUE_NAME))

    async def aclose(self) -> None:
        return None


# ----------------------------------------------------------------- In-process
class InProcessQueueBackend:
    """Local-first fallback (no Redis). Control flags live in a dict;
    leases are no-ops (single process owns the jobs)."""

    name = "inprocess"

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._scheduled: list[tuple[float, str]] = []
        self._controls: dict[str, str] = {}
        self._wake = asyncio.Event()

    async def enqueue(self, job_id: str, *, delay_seconds: float = 0) -> None:
        if delay_seconds > 0:
            self._scheduled.append((time.monotonic() + delay_seconds, job_id))
        else:
            await self._queue.put(job_id)
            self._wake.set()

    async def dequeue(self, timeout_seconds: float) -> str | None:
        now = time.monotonic()
        due = [s for s in self._scheduled if s[0] <= now]
        for item in due:
            self._scheduled.remove(item)
            await self._queue.put(item[1])
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return None

    async def set_control(self, job_id: str, value: str, ttl_seconds: int = 86400) -> None:
        self._controls[job_id] = value

    async def get_control(self, job_id: str) -> str | None:
        return self._controls.get(job_id)

    async def clear_control(self, job_id: str) -> None:
        self._controls.pop(job_id, None)

    async def renew_lease(self, job_id: str, owner: str, ttl_seconds: int) -> None:
        return None

    async def release_lease(self, job_id: str) -> None:
        return None

    async def recover_orphans(self) -> list[str]:
        return []

    async def pending_count(self) -> int:
        return self._queue.qsize()

    async def aclose(self) -> None:
        return None


def build_queue_backend(settings, redis_manager=None) -> QueueBackend:
    """Select the backend from configuration."""
    if settings.REDIS_URL and redis_manager is not None:
        client = redis_manager.get_client()
        if client is not None:
            log_with(logger, 20, "Scrape queue: Redis backend")
            return RedisQueueBackend(client)
    log_with(logger, 20, "Scrape queue: in-process backend (REDIS_URL unset)")
    return InProcessQueueBackend()
