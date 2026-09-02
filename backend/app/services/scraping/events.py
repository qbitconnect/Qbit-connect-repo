"""Job event reporter (brief §13, §39).

Stage-level events are always recorded. Item-level events (ITEM_FOUND /
ITEM_SAVED / ITEM_DUPLICATE / PAGE_FETCHED) are AGGREGATED: a batched row is
emitted every `batch_size` items or `batch_seconds`, with counts in metadata —
trivial events never generate millions of rows (brief §13).

DB writes run on a dedicated session via the provided session_factory so the
event stream survives pipeline session boundaries.
"""

from __future__ import annotations

import uuid

from app.models.scrape import ScrapeJobEvent
from app.services.scraping.progress import ProgressReporter

BATCH_SIZE = 100
BATCH_SECONDS = 10.0
#: events that bypass aggregation
STAGE_EVENTS = {
    "JOB_CREATED",
    "JOB_STARTED",
    "JOB_PAUSED",
    "JOB_RESUMED",
    "JOB_COMPLETED",
    "JOB_FAILED",
    "JOB_CANCELLED",
    "CHECKPOINT_CREATED",
    "RETRY_SCHEDULED",
    "RESUMED_FROM_CHECKPOINT",
    "JOB_RESUMED_FROM_CRASH",
    "LIMIT_REACHED",
    "PROVIDER_STATUS",
}


class EventReporter:
    def __init__(self, session_factory, job_id: uuid.UUID, progress: ProgressReporter, *, page_event_every: int = 50) -> None:
        self._session_factory = session_factory
        self.job_id = job_id
        self._progress = progress
        self._page_event_every = max(page_event_every, 1)
        self._buffer: dict[str, int] = {}
        self._since_flush = 0.0
        self.flushed_batches = 0

    async def emit(self, event_type: str, message: str | None = None, metadata: dict | None = None) -> None:
        """Emit one event. Stage events flush immediately; item events batch."""
        if event_type in STAGE_EVENTS:
            await self._write(event_type, message, metadata or {})
            return
        if event_type == "PAGE_FETCHED":
            # Sample page events: every Nth page is recorded verbatim.
            if self._progress.pages_fetched % self._page_event_every != 0:
                return
            await self._write(event_type, message, metadata or {})
            return
        key = event_type
        self._buffer[key] = self._buffer.get(key, 0) + 1
        self._since_flush += 1
        if self._since_flush >= BATCH_SIZE:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(self._buffer.items()))
        await self._write("ITEMS_BATCH", f"Batch: {summary}", dict(self._buffer))
        self._buffer = {}
        self._since_flush = 0.0
        self.flushed_batches += 1

    async def _write(self, event_type: str, message: str | None, metadata: dict) -> None:
        from datetime import datetime, timezone

        async with self._session_factory() as session:
            session.add(
                ScrapeJobEvent(
                    job_id=self.job_id,
                    event_type=event_type,
                    message=(message or "")[:1000] or None,
                    metadata_json=metadata or {},
                    created_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
