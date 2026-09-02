"""Safe merge logic (Phase 4 §9).

Rules:
- prefer the PRIMARY lead's non-empty values; fill its EMPTY fields from the
  merged lead (never blindly overwrite useful data)
- every conflicting (both non-empty, different) value is preserved in
  LeadMergeHistory.conflicts — conflicts are recorded, never silently dropped
- the merged lead is soft-retired: status ARCHIVED + merged_into_id=primary;
  its notes/activities/tags are re-homed on the primary (history preserved)
- evidence (LeadMergeHistory rows) is never hard-deleted
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.lead import (
    DuplicateStatus,
    LeadActivity,
    LeadDuplicateCandidate,
    LeadMergeHistory,
    LeadNote,
)
from app.models.scrape import Lead
from app.services.leads.activity import LeadActivityService, EVENT_MERGED
from app.services.leads.quality import compute_quality_score
from app.services.leads.tags import TagService

logger = get_logger("qbit.leads.merge")

#: scalar lead fields considered during merge (mirror of the model columns)
MERGE_FIELDS = (
    "business_name", "contact_name", "first_name", "last_name", "email", "phone",
    "website", "address", "city", "state", "postal_code", "country", "category",
    "industry", "rating", "review_count",
)

PROVENANCE_FIELDS = ("source_url", "source_id")


class MergeService:
    def __init__(self) -> None:
        self.activities = LeadActivityService()
        self.tags = TagService()

    async def merge(
        self,
        session: AsyncSession,
        *,
        primary_id: uuid.UUID,
        merged_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        candidate_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> Lead:
        if primary_id == merged_id:
            raise ValidationError("Cannot merge a lead into itself")
        primary = await session.get(Lead, primary_id)
        merged = await session.get(Lead, merged_id)
        if primary is None or merged is None:
            raise NotFoundError("Lead not found")
        if primary.merged_into_id is not None:
            raise ValidationError("Primary lead is itself a merged (retired) lead")
        if merged.merged_into_id is not None:
            raise ValidationError("Lead has already been merged")
        now = datetime.now(timezone.utc)

        before_primary = primary.to_public_dict()
        before_merged = merged.to_public_dict()

        # --- field merge: fill primary's empties; record conflicts ----------
        conflicts: dict[str, dict] = {}
        for field in MERGE_FIELDS:
            primary_value = getattr(primary, field)
            merged_value = getattr(merged, field)
            if merged_value in (None, ""):
                continue
            if primary_value in (None, ""):
                setattr(primary, field, merged_value)
            elif str(primary_value) != str(merged_value):
                conflicts[field] = {"primary": primary_value, "merged": merged_value}

        # provenance backfill
        for field in PROVENANCE_FIELDS:
            if getattr(primary, field) in (None, "") and getattr(merged, field) not in (None, ""):
                setattr(primary, field, getattr(merged, field))

        # --- lifecycle / metadata -------------------------------------------
        primary.seen_count = (primary.seen_count or 1) + (merged.seen_count or 1)
        for attr, pick in (
            ("first_seen_at", min),
            ("last_seen_at", max),
            ("scraped_at", min),
        ):
            a, b = getattr(primary, attr), getattr(merged, attr)
            if a is None:
                setattr(primary, attr, b)
            elif b is not None:
                setattr(primary, attr, pick(a, b))
        if primary.source_type in (None, "") and merged.source_type:
            primary.source_type = merged.source_type

        merged_meta = dict(merged.metadata_json or {})
        merge_note = {
            "merged_from": str(merged.id),
            "merged_at": now.isoformat(),
            "merged_fields_conflicts": sorted(conflicts.keys()),
        }
        primary.metadata_json = {**(primary.metadata_json or {}), **merged_meta, **merge_note}
        primary.quality_score = compute_quality_score(primary.to_public_dict())
        primary.updated_at = now

        # --- re-home notes / activities (history preserved) ------------------
        await session.execute(
            update(LeadNote)
            .where(LeadNote.lead_id == merged.id)
            .values(lead_id=primary.id)
        )
        await session.execute(
            update(LeadActivity)
            .where(LeadActivity.lead_id == merged.id)
            .values(lead_id=primary.id, metadata_json={"merged_from": str(merged.id)})
        )

        # --- tags: union ------------------------------------------------------
        merged_tag_names = await self._tag_names_of(session, merged.id)
        for name in merged_tag_names:
            await self.tags.assign(session, primary.id, name, user_id=user_id, commit=False)

        # --- retire the merged lead (soft, reversible evidence kept) ---------
        merged.status = "ARCHIVED"
        merged.archived_at = now
        merged.merged_into_id = primary.id
        merged.updated_at = now

        history = LeadMergeHistory(
            primary_lead_id=primary.id,
            merged_lead_id=merged.id,
            before_data={"primary": before_primary, "merged": before_merged},
            after_data=primary.to_public_dict(),
            conflicts=conflicts,
            user_id=user_id,
        )
        session.add(history)

        await self.activities.log(
            session, primary.id, EVENT_MERGED,
            message=f"Merged lead {str(merged.id)[:8]} ({merged.business_name or 'unnamed'})",
            metadata={"merged_lead_id": str(merged.id), "conflicts": list(conflicts.keys())},
            user_id=user_id,
        )

        # --- duplicate candidates: resolve pairs touching the merged lead ----
        await self._resolve_candidates(session, primary, merged, user_id, candidate_id)

        if commit:
            await session.commit()
            await session.refresh(primary)
        logger.info(
            "Leads merged",
            extra={"extra_fields": {
                "primary": str(primary.id), "merged": str(merged.id),
                "conflicts": list(conflicts.keys()),
            }},
        )
        return primary

    async def _tag_names_of(self, session: AsyncSession, lead_id: uuid.UUID) -> list[str]:
        from app.models.lead import LeadTag, LeadTagAssignment

        return list(
            (await session.scalars(
                select(LeadTag.name)
                .join(LeadTagAssignment, LeadTagAssignment.tag_id == LeadTag.id)
                .where(LeadTagAssignment.lead_id == lead_id)
            )).all()
        )

    async def _resolve_candidates(
        self,
        session: AsyncSession,
        primary: Lead,
        merged: Lead,
        user_id: uuid.UUID | None,
        candidate_id: uuid.UUID | None,
    ) -> None:
        candidates = (
            await session.scalars(
                select(LeadDuplicateCandidate).where(
                    LeadDuplicateCandidate.status == DuplicateStatus.PENDING,
                    (LeadDuplicateCandidate.lead_a_id == merged.id)
                    | (LeadDuplicateCandidate.lead_b_id == merged.id),
                )
            )
        ).all()
        for row in candidates:
            row.status = DuplicateStatus.MERGED.value
            row.resolved_at = datetime.now(timezone.utc)
            row.resolved_by = user_id
            row.resolution_note = f"Merged into {str(primary.id)[:8]}"
        if candidate_id is not None:
            candidate = await session.get(LeadDuplicateCandidate, candidate_id)
            if candidate is not None and candidate.status == DuplicateStatus.PENDING.value:
                candidate.status = DuplicateStatus.MERGED.value
                candidate.resolved_at = datetime.now(timezone.utc)
                candidate.resolved_by = user_id
