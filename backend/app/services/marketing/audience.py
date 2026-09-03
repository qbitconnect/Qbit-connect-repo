"""Audience engine (Phase 5 §11, §12).

Campaign → AudienceDefinition → Lead Query → Recipients.

Supported audience definitions (stored as campaign.audience_definition JSON):

    {"type": "saved_view", "saved_view_id": "..."}
    {"type": "filters", "filters": {...Phase-4 filter grammar...}}
    {"type": "tags", "tags": ["vip", "hot"], "match": "any"|"all"}
    {"type": "selected", "lead_ids": ["...", "..."]}
    ... all types accept optional "statuses": ["NEW", ...] narrowing

Guarantees:
- resolution streams lead ids in bounded batches — a 10k/100k audience never
  loads every lead into RAM (§44)
- the SNAPSHOT at launch is immutable: recipients are materialized into
  campaign_recipients once; later changes to saved views/leads never mutate
  a launched campaign (§12)
- PRIVATE saved views are only usable by their owner
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

from sqlalchemy import ColumnElement, false, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.models.lead import LeadTag, LeadTagAssignment, SavedView
from app.models.marketing import Campaign, CampaignRecipient, RecipientStatus
from app.models.scrape import Lead
from app.services.leads import filters as filter_engine
from app.services.marketing.channels import get_channel

AUDIENCE_TYPES = ("saved_view", "filters", "tags", "selected")


class AudienceService:
    # ------------------------------------------------------------ validation
    def validate_definition(self, definition: dict | None) -> dict:
        if not isinstance(definition, dict) or not definition:
            raise ValidationError("Audience definition is required")
        a_type = (definition.get("type") or "").strip().lower()
        if a_type not in AUDIENCE_TYPES:
            raise ValidationError(f"audience.type must be one of: {', '.join(AUDIENCE_TYPES)}")
        unknown = set(definition) - {"type", "saved_view_id", "filters", "tags", "match", "lead_ids", "statuses"}
        if unknown:
            raise ValidationError(f"Unknown audience keys: {sorted(unknown)}")

        statuses = definition.get("statuses")
        if statuses is not None:
            if not isinstance(statuses, list) or not all(isinstance(s, str) for s in statuses):
                raise ValidationError("audience.statuses must be a list of strings")
            if len(statuses) > 50:
                raise ValidationError("Too many statuses (max 50)")

        if a_type == "saved_view":
            raw_id = definition.get("saved_view_id")
            try:
                uuid.UUID(str(raw_id))
            except (ValueError, TypeError) as exc:
                raise ValidationError("audience.saved_view_id must be a UUID") from exc
        elif a_type == "filters":
            filters = definition.get("filters")
            if not filters:
                raise ValidationError("audience.filters is required for type 'filters'")
            filter_engine.build_filter_condition(filters)  # validates eagerly
        elif a_type == "tags":
            tags = definition.get("tags")
            if not tags or not isinstance(tags, list):
                raise ValidationError("audience.tags must be a non-empty list")
            if len(tags) > 100:
                raise ValidationError("Too many tags (max 100)")
            match = (definition.get("match") or "any").lower()
            if match not in ("any", "all"):
                raise ValidationError("audience.match must be 'any' or 'all'")
        elif a_type == "selected":
            lead_ids = definition.get("lead_ids")
            if not lead_ids or not isinstance(lead_ids, list):
                raise ValidationError("audience.lead_ids must be a non-empty list")
            if len(lead_ids) > 50_000:
                raise ValidationError("Too many selected leads (max 50000)")
            for raw in lead_ids:
                try:
                    uuid.UUID(str(raw))
                except (ValueError, TypeError) as exc:
                    raise ValidationError(f"Invalid lead id: {raw!r}") from exc
        return {**definition, "type": a_type}

    # ------------------------------------------------------------- condition
    async def build_condition(
        self, session: AsyncSession, definition: dict, *,
        user_id: uuid.UUID | None = None,
    ) -> ColumnElement:
        """Fully resolved, validated WHERE condition over `leads`."""
        definition = self.validate_definition(definition)

        condition = Lead.merged_into_id.is_(None)
        statuses = definition.get("statuses")
        if statuses:
            condition = condition & Lead.status.in_([s.upper() for s in statuses])

        a_type = definition["type"]
        if a_type == "filters":
            condition = condition & filter_engine.build_filter_condition(definition["filters"])
        elif a_type == "saved_view":
            view = await session.get(SavedView, uuid.UUID(str(definition.get("saved_view_id"))))
            if view is None:
                raise NotFoundError("Saved view not found")
            if view.visibility == "PRIVATE" and user_id is not None and view.owner_id != user_id:
                raise PermissionDeniedError("This saved view is private")
            filters = view.filters or {}
            if filters:
                condition = condition & filter_engine.build_filter_condition(filters)
        elif a_type == "tags":
            tag_names = [str(t)[:100] for t in definition.get("tags", [])]
            tag_ids = (await session.execute(
                select(LeadTag.id).where(LeadTag.name.in_(tag_names))
            )).scalars().all()
            if not tag_ids:
                return condition & false()  # impossible tag set → empty audience
            match = (definition.get("match") or "any").lower()
            assigned = select(LeadTagAssignment.lead_id).where(
                LeadTagAssignment.tag_id.in_(tag_ids)
            )
            if match == "all":
                assigned = (
                    select(LeadTagAssignment.lead_id)
                    .where(LeadTagAssignment.tag_id.in_(tag_ids))
                    .group_by(LeadTagAssignment.lead_id)
                    .having(func.count(func.distinct(LeadTagAssignment.tag_id)) == len(tag_ids))
                )
            condition = condition & Lead.id.in_(assigned)
        elif a_type == "selected":
            ids = [uuid.UUID(str(raw)) for raw in definition.get("lead_ids", [])]
            condition = condition & Lead.id.in_(ids)
        return condition

    # -------------------------------------------------------------- queries
    async def count(
        self, session: AsyncSession, definition: dict, *,
        user_id: uuid.UUID | None = None,
    ) -> int:
        condition = await self.build_condition(session, definition, user_id=user_id)
        return int(await session.scalar(
            select(func.count()).select_from(Lead).where(condition)
        ) or 0)

    async def iter_lead_ids(
        self, session: AsyncSession, definition: dict, *,
        user_id: uuid.UUID | None = None, batch_size: int = 1000,
        max_audience: int | None = None,
    ) -> AsyncIterator[list[uuid.UUID]]:
        """Stream matching lead ids in bounded keyset batches (§44)."""
        condition = await self.build_condition(session, definition, user_id=user_id)
        last_id: uuid.UUID | None = None
        emitted = 0
        while True:
            stmt = select(Lead.id).where(condition)
            if last_id is not None:
                stmt = stmt.where(Lead.id > last_id)
            stmt = stmt.order_by(Lead.id).limit(batch_size)
            rows = await session.execute(stmt)
            ids = [row[0] for row in rows.all()]
            if not ids:
                return
            last_id = ids[-1]
            if max_audience is not None and emitted + len(ids) > max_audience:
                raise ValidationError(
                    f"Audience exceeds the {max_audience} lead safety cap"
                )
            emitted += len(ids)
            yield ids

    async def preview(
        self, session: AsyncSession, definition: dict, *,
        user_id: uuid.UUID | None = None, limit: int = 10,
    ) -> tuple[list[Lead], int]:
        """Sample leads + total for the wizard audience step."""
        condition = await self.build_condition(session, definition, user_id=user_id)
        total = int(await session.scalar(
            select(func.count()).select_from(Lead).where(condition)
        ) or 0)
        rows = await session.execute(
            select(Lead).where(condition)
            .order_by(Lead.created_at.desc()).limit(min(max(1, limit), 50))
        )
        return list(rows.scalars().all()), total

    # -------------------------------------------------------------- snapshot
    async def snapshot(
        self, session: AsyncSession, campaign: Campaign, *,
        user_id: uuid.UUID | None = None, batch_size: int = 1000,
        max_audience: int | None = None,
    ) -> dict:
        """Materialize the audience into campaign_recipients (§12).

        Batched bulk inserts; refuses to double-snapshot (idempotency guard
        against accidental double-launch).
        """
        existing = await session.scalar(
            select(func.count()).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign.id)
        )
        if existing:
            return {"total": int(existing), "created": 0, "already_snapshotted": True}

        spec = get_channel(campaign.channel)
        if spec is None:
            raise ValidationError(f"Unknown campaign channel: {campaign.channel}")
        address_attr = "email" if spec.address_kind == "email" else "phone"

        created = 0
        async for id_batch in self.iter_lead_ids(
            session, campaign.audience_definition or {},
            user_id=user_id, batch_size=batch_size, max_audience=max_audience,
        ):
            rows_q = await session.execute(
                select(Lead.id, getattr(Lead, address_attr),
                       func.coalesce(Lead.business_name, Lead.contact_name, Lead.first_name))
                .where(Lead.id.in_(id_batch))
            )
            seen: set[uuid.UUID] = set()
            payload = []
            for lead_id, address, name in rows_q.all():
                if lead_id in seen:
                    continue
                seen.add(lead_id)
                payload.append({
                    "campaign_id": campaign.id,
                    "lead_id": lead_id,
                    "recipient_address": (address or "")[:320],
                    "recipient_name": (name or "")[:300],
                    "status": RecipientStatus.PENDING,
                })
            if payload:
                await session.execute(insert(CampaignRecipient), payload)
                created += len(payload)
        await session.commit()
        return {"total": created, "created": created}
