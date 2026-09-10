"""Key-Value storage + persistent Request Queue (Actor Platform spec §13-§14).

KVStore  — actor state, checkpoints metadata, JSON documents; scope+key
           addressed, DB-backed (survives restarts, spec §14/§15).
RequestQueue — persistent per-run URL queue with priority/depth/retries and
           URL-level dedup (url_norm). Actors use it to hand discovered URLs
           to the platform instead of hoarding them in RAM.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.actor_platform import (
    ActorKvEntry,
    ActorRequestQueueItem,
    QueueItemStatus,
)


def normalize_queue_url(url: str) -> str:
    """Dedup key: strip fragment, lowercase host, keep path+query."""
    parts = urlsplit(url.strip())
    netloc = (parts.hostname or "").lower()
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


class KVStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str, scope: str = "global") -> dict | None:
        row = (
            await self.session.execute(
                select(ActorKvEntry).where(
                    ActorKvEntry.scope == scope, ActorKvEntry.key == key
                )
            )
        ).scalars().first()
        return row.to_public_dict() if row else None

    async def get_value(self, key: str, scope: str = "global", default=None):
        row = await self.get(key, scope)
        return row["value"] if row else default

    async def set(
        self,
        key: str,
        value: dict,
        *,
        scope: str = "global",
        content_type: str = "json",
        updated_by: uuid.UUID | None = None,
    ) -> dict:
        row = (
            await self.session.execute(
                select(ActorKvEntry).where(
                    ActorKvEntry.scope == scope, ActorKvEntry.key == key
                )
            )
        ).scalars().first()
        now = datetime.now(timezone.utc)
        if row is None:
            row = ActorKvEntry(
                scope=scope, key=key, value=value,
                content_type=content_type, updated_by=updated_by,
                created_at=now, updated_at=now,
            )
            self.session.add(row)
        else:
            row.value = value
            row.content_type = content_type
            row.updated_by = updated_by
            row.updated_at = now
        await self.session.flush()
        return row.to_public_dict()

    async def delete(self, key: str, scope: str = "global") -> bool:
        res = await self.session.execute(
            delete(ActorKvEntry).where(ActorKvEntry.scope == scope, ActorKvEntry.key == key)
        )
        return bool(res.rowcount)

    async def list(self, *, scope: str | None = None, limit: int = 100, offset: int = 0):
        stmt = select(ActorKvEntry)
        count = select(func.count(ActorKvEntry.id))
        if scope:
            stmt = stmt.where(ActorKvEntry.scope == scope)
            count = count.where(ActorKvEntry.scope == scope)
        total = (await self.session.execute(count)).scalar_one()
        rows = await self.session.execute(
            stmt.order_by(ActorKvEntry.scope, ActorKvEntry.key).limit(limit).offset(offset)
        )
        return [r.to_public_dict() for r in rows.scalars()], int(total)


class RequestQueue:
    """Persistent request queue (spec §13). One queue per run (queue_name =
    the job id by convention) plus an optional actor-scoped durable queue."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def push(
        self,
        queue_name: str,
        url: str,
        *,
        method: str = "GET",
        priority: int = 100,
        depth: int = 0,
        parent_url: str | None = None,
        job_id: uuid.UUID | None = None,
        max_retries: int = 3,
    ) -> ActorRequestQueueItem | None:
        """Add a URL; returns None when it already exists (dedup, spec §13)."""
        url_norm = normalize_queue_url(url)
        existing = (
            await self.session.execute(
                select(ActorRequestQueueItem.id).where(
                    ActorRequestQueueItem.queue_name == queue_name,
                    ActorRequestQueueItem.url_norm == url_norm,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
        item = ActorRequestQueueItem(
            queue_name=queue_name,
            job_id=job_id,
            url=url,
            url_norm=url_norm,
            method=method,
            priority=priority,
            depth=depth,
            parent_url=parent_url,
            max_retries=max_retries,
            discovered_at=datetime.now(timezone.utc),
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def claim(self, queue_name: str, *, limit: int = 10) -> list[ActorRequestQueueItem]:
        """Claim the next PENDING items (priority ASC, FIFO) → PROCESSING."""
        rows = (
            await self.session.execute(
                select(ActorRequestQueueItem)
                .where(
                    ActorRequestQueueItem.queue_name == queue_name,
                    ActorRequestQueueItem.status == QueueItemStatus.PENDING.value,
                )
                .order_by(ActorRequestQueueItem.priority, ActorRequestQueueItem.discovered_at)
                .limit(limit)
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        for item in rows:
            item.status = QueueItemStatus.PROCESSING.value
        if rows:
            await self.session.flush()
        return list(rows)

    async def mark_done(self, item_id: uuid.UUID) -> bool:
        return await self._transition(item_id, QueueItemStatus.DONE)

    async def mark_failed(self, item_id: uuid.UUID, error: str | None = None) -> str:
        """FAILED; re-queues for retry when under max_retries (spec §16).
        Returns the resulting status: PENDING (retry) or FAILED (final)."""
        row = await self.session.get(ActorRequestQueueItem, item_id)
        if row is None:
            return "GONE"
        row.retries += 1
        row.error = (error or "")[:2000]
        if row.retries <= row.max_retries:
            row.status = QueueItemStatus.PENDING.value
            await self.session.flush()
            return QueueItemStatus.PENDING.value
        row.status = QueueItemStatus.FAILED.value
        row.processed_at = datetime.now(timezone.utc)
        await self.session.flush()
        return QueueItemStatus.FAILED.value

    async def _transition(self, item_id: uuid.UUID, status: QueueItemStatus) -> bool:
        res = await self.session.execute(
            update(ActorRequestQueueItem)
            .where(ActorRequestQueueItem.id == item_id)
            .values(
                status=status.value,
                processed_at=datetime.now(timezone.utc),
            )
        )
        return res.rowcount == 1

    async def stats(self, queue_name: str) -> dict:
        rows = await self.session.execute(
            select(ActorRequestQueueItem.status, func.count(ActorRequestQueueItem.id))
            .where(ActorRequestQueueItem.queue_name == queue_name)
            .group_by(ActorRequestQueueItem.status)
        )
        by_status = {status: int(n) for status, n in rows.all()}
        return {
            "queue_name": queue_name,
            "total": sum(by_status.values()),
            "pending": by_status.get(QueueItemStatus.PENDING.value, 0),
            "processing": by_status.get(QueueItemStatus.PROCESSING.value, 0),
            "done": by_status.get(QueueItemStatus.DONE.value, 0),
            "failed": by_status.get(QueueItemStatus.FAILED.value, 0),
        }

    async def list_items(
        self, queue_name: str, *, status: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> tuple[list[ActorRequestQueueItem], int]:
        stmt = select(ActorRequestQueueItem).where(
            ActorRequestQueueItem.queue_name == queue_name
        )
        count = select(func.count(ActorRequestQueueItem.id)).where(
            ActorRequestQueueItem.queue_name == queue_name
        )
        if status:
            stmt = stmt.where(ActorRequestQueueItem.status == status)
            count = count.where(ActorRequestQueueItem.status == status)
        total = (await self.session.execute(count)).scalar_one()
        rows = await self.session.execute(
            stmt.order_by(ActorRequestQueueItem.discovered_at.desc()).limit(limit).offset(offset)
        )
        return list(rows.scalars()), int(total)

    async def list_queues(self) -> list[dict]:
        rows = await self.session.execute(
            select(
                ActorRequestQueueItem.queue_name,
                func.count(ActorRequestQueueItem.id),
            )
            .group_by(ActorRequestQueueItem.queue_name)
            .order_by(ActorRequestQueueItem.queue_name)
        )
        return [
            {"queue_name": name, "total": int(total)}
            for name, total in rows.all()
        ]
