"""Lead tagging (Phase 4 §4).

LeadTag + LeadTagAssignment are the relational source of truth; the legacy
`leads.tags` JSON column is a denormalized display MIRROR refreshed after every
mutation. Duplicate assignments are impossible (unique constraint + explicit
existence checks). Tag names are case-preserving but case-insensitively unique.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.lead import LeadTag, LeadTagAssignment
from app.models.scrape import Lead

logger = get_logger("qbit.leads.tags")

MAX_NAME = 100


def _clean_name(name: str) -> str:
    cleaned = " ".join(str(name or "").split())[:MAX_NAME]
    if not cleaned:
        raise ValidationError("Tag name must not be empty")
    return cleaned


class TagService:
    # ------------------------------------------------------------- tag CRUD
    async def create(
        self, session: AsyncSession, name: str, *, color: str | None = None,
        is_system: bool = False, created_by: uuid.UUID | None = None,
        commit: bool = True,
    ) -> LeadTag:
        clean = _clean_name(name)
        existing = await session.scalar(
            select(LeadTag).where(func.lower(LeadTag.name) == clean.lower())
        )
        if existing is not None:
            raise ConflictError(f"Tag already exists: {clean}")
        tag = LeadTag(name=clean, color=color, is_system=is_system, created_by=created_by)
        session.add(tag)
        if commit:
            await session.commit()
            await session.refresh(tag)
        else:
            # callers immediately use tag.id for assignments — flush now
            await session.flush()
        return tag

    async def ensure(self, session: AsyncSession, name: str, *, commit: bool = False) -> LeadTag:
        """Get-or-create by name (used by ingestion/import auto tags)."""
        clean = _clean_name(name)
        tag = await session.scalar(
            select(LeadTag).where(func.lower(LeadTag.name) == clean.lower())
        )
        if tag is None:
            tag = await self.create(session, clean, is_system=True, commit=False)
        return tag

    async def rename(self, session: AsyncSession, tag_id: uuid.UUID, new_name: str) -> LeadTag:
        tag = await session.get(LeadTag, tag_id)
        if tag is None:
            raise NotFoundError("Tag not found")
        clean = _clean_name(new_name)
        clash = await session.scalar(
            select(LeadTag).where(
                func.lower(LeadTag.name) == clean.lower(), LeadTag.id != tag_id
            )
        )
        if clash is not None:
            raise ConflictError(f"Tag already exists: {clean}")
        old = tag.name
        tag.name = clean
        tag.updated_at = tag.updated_at  # touched by DB on flush
        await session.commit()
        # refresh the mirror on every lead carrying this tag
        lead_ids = (
            await session.scalars(
                select(LeadTagAssignment.lead_id).where(LeadTagAssignment.tag_id == tag_id)
            )
        ).all()
        for lead_id in lead_ids:
            await self._refresh_mirror(session, lead_id)
        await session.commit()
        logger.info("Tag renamed", extra={"extra_fields": {"tag_id": str(tag_id), "from": old, "to": clean}})
        return tag

    async def delete(self, session: AsyncSession, tag_id: uuid.UUID) -> None:
        tag = await session.get(LeadTag, tag_id)
        if tag is None:
            raise NotFoundError("Tag not found")
        lead_ids = (
            await session.scalars(
                select(LeadTagAssignment.lead_id).where(LeadTagAssignment.tag_id == tag_id)
            )
        ).all()
        await session.execute(
            LeadTagAssignment.__table__.delete().where(LeadTagAssignment.tag_id == tag_id)
        )
        await session.delete(tag)
        for lead_id in lead_ids:
            await self._refresh_mirror(session, lead_id)
        await session.commit()
        logger.info("Tag deleted", extra={"extra_fields": {"tag_id": str(tag_id), "name": tag.name}})

    async def list(self, session: AsyncSession, *, with_counts: bool = True) -> list[dict]:
        tags = (await session.scalars(select(LeadTag).order_by(LeadTag.name))).all()
        if not with_counts:
            return [t.to_public_dict() for t in tags]
        counts = dict(
            (await session.execute(
                select(LeadTagAssignment.tag_id, func.count())
                .group_by(LeadTagAssignment.tag_id)
            )).all()
        )
        out = []
        for tag in tags:
            d = tag.to_public_dict()
            d["lead_count"] = int(counts.get(tag.id, 0))
            out.append(d)
        return out

    # ---------------------------------------------------------- assignments
    async def assign(
        self, session: AsyncSession, lead_id: uuid.UUID, tag: str | LeadTag, *,
        user_id: uuid.UUID | None = None, commit: bool = True,
    ) -> tuple[LeadTag, bool]:
        """Assign a tag to one lead. Returns (tag, created). Idempotent."""
        lead = await session.get(Lead, lead_id)
        if lead is None:
            raise NotFoundError("Lead not found")
        if isinstance(tag, str):
            tag = await self.ensure(session, tag)
        exists = await session.scalar(
            select(LeadTagAssignment.id).where(
                LeadTagAssignment.lead_id == lead_id, LeadTagAssignment.tag_id == tag.id
            )
        )
        if exists is not None:
            return tag, False
        session.add(LeadTagAssignment(lead_id=lead_id, tag_id=tag.id, created_by=user_id))
        await self._refresh_mirror(session, lead_id)
        if commit:
            await session.commit()
        return tag, True

    async def unassign(
        self, session: AsyncSession, lead_id: uuid.UUID, tag_id: uuid.UUID, *,
        commit: bool = True,
    ) -> bool:
        result = await session.execute(
            LeadTagAssignment.__table__.delete().where(
                LeadTagAssignment.lead_id == lead_id, LeadTagAssignment.tag_id == tag_id
            )
        )
        removed = (result.rowcount or 0) > 0
        if removed:
            await self._refresh_mirror(session, lead_id)
        if commit:
            await session.commit()
        return removed

    async def bulk_assign(
        self, session: AsyncSession, lead_ids: list[uuid.UUID], tag_names: list[str], *,
        user_id: uuid.UUID | None = None,
    ) -> int:
        """Efficient bulk tag: one existence query, one bulk insert, mirrors per lead."""
        if not lead_ids or not tag_names:
            return 0
        tags = [await self.ensure(session, name) for name in tag_names]
        existing = set(
            (await session.execute(
                select(LeadTagAssignment.lead_id, LeadTagAssignment.tag_id).where(
                    LeadTagAssignment.lead_id.in_(lead_ids),
                    LeadTagAssignment.tag_id.in_([t.id for t in tags]),
                )
            )).all()
        )
        rows = [
            LeadTagAssignment(lead_id=lead_id, tag_id=tag.id, created_by=user_id)
            for lead_id in lead_ids
            for tag in tags
            if (lead_id, tag.id) not in existing
        ]
        if rows:
            session.add_all(rows)
        for lead_id in lead_ids:
            await self._refresh_mirror(session, lead_id)
        await session.commit()
        return len(rows)

    async def bulk_unassign(
        self, session: AsyncSession, lead_ids: list[uuid.UUID], tag_ids: list[uuid.UUID]
    ) -> int:
        if not lead_ids or not tag_ids:
            return 0
        result = await session.execute(
            LeadTagAssignment.__table__.delete().where(
                LeadTagAssignment.lead_id.in_(lead_ids), LeadTagAssignment.tag_id.in_(tag_ids)
            )
        )
        for lead_id in lead_ids:
            await self._refresh_mirror(session, lead_id)
        await session.commit()
        return result.rowcount or 0

    async def sync_names(
        self, session: AsyncSession, lead_id: uuid.UUID, names: list[str], *,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Make assignments match `names` exactly (used by ingestion/import)."""
        current = {
            row[0]: row[1]
            for row in (await session.execute(
                select(LeadTagAssignment.tag_id, LeadTag.name)
                .join(LeadTag, LeadTag.id == LeadTagAssignment.tag_id)
                .where(LeadTagAssignment.lead_id == lead_id)
            )).all()
        }
        desired = {}
        for name in names:
            if not str(name).strip():
                continue
            tag = await self.ensure(session, str(name))
            desired[tag.id] = tag.name
        for tag_id in set(desired) - set(current):
            session.add(LeadTagAssignment(lead_id=lead_id, tag_id=tag_id, created_by=user_id))
        for tag_id in set(current) - set(desired):
            await session.execute(
                LeadTagAssignment.__table__.delete().where(
                    LeadTagAssignment.lead_id == lead_id, LeadTagAssignment.tag_id == tag_id
                )
            )
        await self._refresh_mirror(session, lead_id)

    async def _refresh_mirror(self, session: AsyncSession, lead_id: uuid.UUID) -> None:
        """Rewrite the denormalized `tags` JSON column from assignments.

        The sessions run with expire_on_commit=False, so any already-loaded
        Lead instance must have its `tags` attribute expired — otherwise API
        responses would show a stale mirror (identity-map hit)."""
        # autoflush is disabled app-wide: flush so PENDING assignment inserts
        # from THIS transaction are visible to the SELECT below
        await session.flush()
        names = sorted(
            (await session.scalars(
                select(LeadTag.name)
                .join(LeadTagAssignment, LeadTagAssignment.tag_id == LeadTag.id)
                .where(LeadTagAssignment.lead_id == lead_id)
            )).all()
        )
        await session.execute(
            Lead.__table__.update().where(Lead.id == lead_id).values(tags=names)
        )
        cached = await session.get(Lead, lead_id)  # identity-map hit when loaded
        if cached is not None:
            # set directly (no expire → no lazy-load IO in async context);
            # identical value, harmless on the next flush
            cached.tags = names
