"""LeadService — the ONLY writer of lead rows (brief §22).

Actors never touch lead tables directly; the result pipeline calls this
service with normalized items. Responsibilities: create, update-on-match,
source tracking, metadata merge.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scrape import Lead


def _merge_metadata(existing: dict, incoming: dict) -> dict:
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key in ("possible_duplicate_of",):
            continue
        if value in (None, "", {}, []):
            continue
        if key not in merged:
            merged[key] = value
        elif merged[key] != value and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_metadata(merged[key], value)
        elif merged[key] != value:
            history = merged.setdefault(f"{key}_history", [])
            if not isinstance(history, list):
                history = [history]
            if merged[key] not in history:
                history.append(merged[key])
            merged[key] = value
    return merged


def _merge_social(existing: dict, incoming: dict) -> dict:
    merged = dict(existing or {})
    for platform, url in (incoming or {}).items():
        if url and not merged.get(platform):
            merged[platform] = url
    return merged


class LeadService:
    async def create_or_update(
        self,
        session: AsyncSession,
        item: dict,
        *,
        actor_id: str,
        actor_version: str,
        job_id: uuid.UUID,
        match=None,  # MatchResult | None
        merge: bool = False,
        created_by: uuid.UUID | None = None,
        commit: bool = True,
    ) -> tuple[Lead, bool]:
        """Insert a new lead or update the matched one.

        Returns (lead, created). `item` is a normalized lead dict (with
        *_norm keys produced by the normalizer). With `commit=False` the
        caller owns the transaction — the pipeline commits once per BATCH
        instead of once per record (brief §44).
        """
        now = datetime.now(timezone.utc)
        base = dict(
            business_name=item.get("business_name"),
            contact_name=item.get("contact_name"),
            email=item.get("email"),
            phone=item.get("phone"),
            website=item.get("website"),
            address=item.get("address"),
            city=item.get("city"),
            state=item.get("state"),
            country=item.get("country"),
            category=item.get("category"),
            rating=item.get("rating"),
            review_count=item.get("review_count"),
        )

        if merge and match is not None and match.lead_id is not None:
            lead = await session.get(Lead, match.lead_id)
            if lead is not None:
                for key, value in base.items():
                    if value is not None and (getattr(lead, key) is None or getattr(lead, key) == ""):
                        setattr(lead, key, value)
                lead.social_links = _merge_social(lead.social_links or {}, item.get("social_links") or {})
                lead.metadata_json = _merge_metadata(
                    lead.metadata_json or {},
                    {
                        **(item.get("metadata") or {}),
                        "last_job_id": str(job_id),
                        "last_actor_id": actor_id,
                        "last_match_confidence": match.confidence.value if match.confidence else None,
                        "matched_on": match.matched_on,
                    },
                )
                lead.seen_count = (lead.seen_count or 1) + 1
                lead.last_seen_at = now
                if item.get("scraped_at"):
                    lead.scraped_at = item["scraped_at"]
                if commit:
                    await session.commit()
                    await session.refresh(lead)
                return lead, False

        metadata = dict(item.get("metadata") or {})
        if (
            match is not None
            and not merge
            and match.lead_id is not None
            and match.confidence is not None
            and match.confidence.value != "NONE"
        ):
            # information-preserving duplicate flag (brief §21)
            metadata["possible_duplicate_of"] = str(match.lead_id)
            metadata["possible_duplicate_confidence"] = (
                match.confidence.value if match.confidence else None
            )

        lead = Lead(
            **base,
            social_links=item.get("social_links") or {},
            metadata_json=metadata,
            tags=item.get("tags") or [],
            email_norm=item.get("email_norm"),
            phone_norm=item.get("phone_norm"),
            website_norm=item.get("website_norm"),
            name_key=item.get("name_key"),
            source=item.get("source") or actor_id,
            source_url=item.get("source_url"),
            source_actor_id=actor_id,
            source_actor_version=actor_version,
            source_job_id=job_id,
            scraped_at=item.get("scraped_at") or now,
            first_seen_at=now,
            last_seen_at=now,
            created_by=created_by,
        )
        session.add(lead)
        if commit:
            await session.commit()
            await session.refresh(lead)
        return lead, True

    async def count_for_job(self, session: AsyncSession, job_id: uuid.UUID) -> int:
        return int(
            await session.scalar(
                select(func.count()).select_from(Lead).where(Lead.source_job_id == job_id)
            )
            or 0
        )

    async def list_for_job(
        self, session: AsyncSession, job_id: uuid.UUID, *, page: int = 1, page_size: int = 50
    ) -> tuple[list[Lead], int]:
        query = select(Lead).where(Lead.source_job_id == job_id)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(Lead.created_at.asc()).offset((page - 1) * page_size).limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def update_lead(self, session: AsyncSession, lead_id: uuid.UUID, changes: dict) -> Lead | None:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            return None
        for key, value in changes.items():
            if hasattr(lead, key) and key not in ("id", "created_at"):
                setattr(lead, key, value)
        lead.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(lead)
        return lead

    async def archive(self, session: AsyncSession, lead_id: uuid.UUID) -> Lead | None:
        return await self.update_lead(session, lead_id, {"status": "archived"})
