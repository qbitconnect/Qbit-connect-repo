"""Checkpoint manager (brief §18).

Checkpoints are PERSISTED in Postgres (scrape_job_checkpoints) — Redis never
holds the only copy. Saving is throttled (min interval / forced on pause,
shutdown and completion) so cheap jobs don't spam the table.

Resume contract: the runner loads the latest checkpoint before run() and the
actor reads `ctx.checkpoint.data` to continue from its cursor. Checkpoint
payloads are small JSON dicts (cursors, offsets, page ids) — never bulk data.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scrape import ScrapeJobCheckpoint


class CheckpointManager:
    def __init__(self, session_factory, job_id: uuid.UUID, *, min_interval: float = 5.0, keep: int = 5) -> None:
        self._session_factory = session_factory
        self.job_id = job_id
        self._min_interval = min_interval
        self._keep = keep
        self.data: dict = {}
        self.records_processed = 0
        self._last_save = 0.0
        self._dirty = False
        self.saved_count = 0

    async def load(self) -> dict:
        """Load the latest checkpoint for this job (empty dict if none)."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ScrapeJobCheckpoint)
                .where(ScrapeJobCheckpoint.job_id == self.job_id)
                .order_by(ScrapeJobCheckpoint.created_at.desc())
                .limit(1)
            )
            if row is not None:
                self.data = dict(row.cursor or {})
                self.records_processed = int(row.records_processed or 0)
            return dict(self.data)

    def update(self, cursor: dict, records_processed: int | None = None) -> None:
        """Merge a cursor fragment into the working checkpoint (in memory)."""
        self.data.update(cursor)
        if records_processed is not None:
            self.records_processed = records_processed
        self._dirty = True

    async def save(self, *, force: bool = False) -> bool:
        """Persist the working checkpoint (throttled). Returns True if written."""
        if not self._dirty and not force:
            return False
        now = time.monotonic()
        if not force and now - self._last_save < self._min_interval:
            return False
        async with self._session_factory() as session:
            session.add(
                ScrapeJobCheckpoint(
                    job_id=self.job_id,
                    cursor=dict(self.data),
                    records_processed=self.records_processed,
                )
            )
            await self._prune(session)
            await session.commit()
        self._last_save = now
        self._dirty = False
        self.saved_count += 1
        return True

    async def _prune(self, session: AsyncSession) -> None:
        """Keep only the newest `keep` checkpoints per job (bounded growth)."""
        rows = (
            await session.scalars(
                select(ScrapeJobCheckpoint.id)
                .where(ScrapeJobCheckpoint.job_id == self.job_id)
                .order_by(ScrapeJobCheckpoint.created_at.desc(), ScrapeJobCheckpoint.id.desc())
            )
        ).all()
        stale = rows[self._keep:]
        if stale:
            await session.execute(
                delete(ScrapeJobCheckpoint).where(ScrapeJobCheckpoint.id.in_(stale))
            )

    async def clear(self) -> None:
        """Remove all checkpoints (completion)."""
        async with self._session_factory() as session:
            await session.execute(
                delete(ScrapeJobCheckpoint).where(ScrapeJobCheckpoint.job_id == self.job_id)
            )
            await session.commit()
        self.data = {}
        self.records_processed = 0
