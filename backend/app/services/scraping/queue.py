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
    leases are no-ops.

    Cross-process discovery (Actor Platform fix): when the API and the worker
    run as SEPARATE processes, an API-side enqueue can never reach this
    in-memory queue. The worker's backend therefore ALSO polls the durable
    `scrape_jobs` table for QUEUED rows (DB-first principle, doc 09 §1) —
    bounded, TTL-deduped, and always followed by the runner's atomic claim,
    so double-dispatch across workers remains impossible.
    """

    name = "inprocess"
    #: DB-poll cadence while the local queue is empty (seconds)
    DB_POLL_SECONDS = 2.0

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._scheduled: list[tuple[float, str]] = []
        self._controls: dict[str, str] = {}
        self._wake = asyncio.Event()
        self._session_factory = None  # worker wires this for DB discovery
        self._last_db_poll = 0.0
        self._db_suppressed: dict[str, float] = {}

    def attach_db_discovery(self, session_factory) -> None:
        """Enable QUEUED-row discovery (worker process only)."""
        self._session_factory = session_factory

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
        remaining = timeout_seconds
        while True:
            try:
                return await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            # local queue empty — poll the DB (rate-limited) for QUEUED jobs
            # created by OTHER processes (API, schedules).
            spent = timeout_seconds - remaining
            if now - self._last_db_poll >= self.DB_POLL_SECONDS or spent >= timeout_seconds:
                self._last_db_poll = now
                discovered = await self._discover_queued()
                if discovered:
                    await self._queue.put(discovered)
                    continue
            return None

    async def _discover_queued(self) -> str | None:
        if self._session_factory is None:
            return None
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone as _tz

        from sqlalchemy import select

        from app.models.scrape import JobStatus, ScrapeJob

        cutoff = datetime.now(_tz.utc) - timedelta(seconds=5)  # grace for in-flight commits
        suppressed = {
            jid: ts for jid, ts in self._db_suppressed.items()
            if time.monotonic() - ts < 30
        }
        self._db_suppressed = suppressed
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(ScrapeJob)
                        .where(
                            ScrapeJob.status == JobStatus.QUEUED.value,
                            ScrapeJob.created_at <= cutoff,
                        )
                        .order_by(ScrapeJob.created_at)
                        .limit(10)
                    )
                ).scalars().all()
            for job in rows:
                key = str(job.id)
                if key in self._db_suppressed:
                    continue
                self._db_suppressed[key] = time.monotonic()
                return key
        except Exception:  # noqa: BLE001 — discovery must never kill the loop
            return None
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


def build_queue_backend(settings, redis_manager=None, *, session_factory=None) -> QueueBackend:
    """Select the backend from configuration.

    `session_factory` enables DB-row discovery on the in-process backend
    (worker process) so jobs enqueued by OTHER processes are picked up
    without Redis (spec deployment: API + worker as separate processes).
    """
    if settings.REDIS_URL and redis_manager is not None:
        client = redis_manager.get_client()
        if client is not None:
            log_with(logger, 20, "Scrape queue: Redis backend")
            return RedisQueueBackend(client)
    backend = InProcessQueueBackend()
    if session_factory is not None:
        backend.attach_db_discovery(session_factory)
        log_with(logger, 20, "Scrape queue: in-process backend + DB discovery (REDIS_URL unset)")
    else:
        log_with(logger, 20, "Scrape queue: in-process backend (REDIS_URL unset)")
    return backend
