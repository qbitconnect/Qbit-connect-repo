"""Lead activity trail (Phase 4 §6).

One service writes every per-lead event. `commit=False` by default so callers
batch activities into their own transactions (pipeline batches, bulk ops).
Event types are a fixed vocabulary used by the UI timeline.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.lead import LeadActivity

logger = get_logger("qbit.leads.activity")

# vocabulary
EVENT_CREATED = "lead_created"
EVENT_IMPORTED = "lead_imported"
EVENT_SCRAPED = "lead_scraped"
EVENT_UPDATED = "lead_updated"
EVENT_STATUS_CHANGED = "status_changed"
EVENT_TAG_ADDED = "tag_added"
EVENT_TAG_REMOVED = "tag_removed"
EVENT_NOTE_ADDED = "note_added"
EVENT_EXPORTED = "export_performed"
EVENT_MERGED = "duplicate_merged"
EVENT_ARCHIVED = "lead_archived"
EVENT_RESTORED = "lead_restored"


class LeadActivityService:
    async def log(
        self,
        session: AsyncSession,
        lead_id: uuid.UUID,
        event_type: str,
        *,
        message: str | None = None,
        metadata: dict | None = None,
        user_id: uuid.UUID | None = None,
        commit: bool = False,
    ) -> LeadActivity:
        row = LeadActivity(
            lead_id=lead_id,
            user_id=user_id,
            event_type=event_type,
            message=message[:500] if message else None,
            metadata_json=metadata or {},
        )
        session.add(row)
        if commit:
            await session.commit()
        return row

    async def log_many(
        self,
        session: AsyncSession,
        entries: list[dict],
        *,
        commit: bool = False,
    ) -> None:
        """Batched insert for bulk operations / pipeline batches."""
        for entry in entries:
            session.add(
                LeadActivity(
                    lead_id=entry["lead_id"],
                    user_id=entry.get("user_id"),
                    event_type=entry["event_type"],
                    message=(entry.get("message") or "")[:500] or None,
                    metadata_json=entry.get("metadata") or {},
                )
            )
        if commit:
            await session.commit()

    async def list_for_lead(
        self, session: AsyncSession, lead_id: uuid.UUID, *, page: int = 1, page_size: int = 50
    ) -> tuple[list[LeadActivity], int]:
        query = select(LeadActivity).where(LeadActivity.lead_id == lead_id)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(LeadActivity.created_at.desc(), LeadActivity.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)
