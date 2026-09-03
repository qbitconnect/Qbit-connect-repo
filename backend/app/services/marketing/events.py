"""Campaign event stream (Phase 5 §8, §37).

Append-only, immutable event records. Events are the single source of truth
for analytics (§24) — nothing is computed from mutable state alone.

The event normalizer converts provider webhook payloads into campaign events
without letting provider-specific payload shapes leak into CampaignService
(§37). Secrets never enter event payloads — the same redaction used by the
audit log is applied to metadata.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import redact
from app.models.marketing import CampaignEvent, EventType
from app.services.marketing.providers import MarketingProviderRegistry

#: provider event names → canonical event types (§37 normalizer)
PROVIDER_EVENT_MAP = {
    "sent": EventType.MESSAGE_SENT,
    "delivered": EventType.MESSAGE_DELIVERED,
    "read": EventType.MESSAGE_READ,
    "reply": EventType.MESSAGE_REPLIED,
    "replied": EventType.MESSAGE_REPLIED,
    "failed": EventType.MESSAGE_FAILED,
    "bounce": EventType.MESSAGE_FAILED,
    "bounced": EventType.MESSAGE_FAILED,
    "unsubscribe": "UNSUBSCRIBE",
}


class EventService:
    async def record(
        self, session: AsyncSession, *,
        campaign_id: uuid.UUID,
        recipient_id: uuid.UUID | None = None,
        event_type: str,
        provider: str | None = None,
        provider_event_id: str | None = None,
        metadata: dict | None = None,
        commit: bool = True,
    ) -> CampaignEvent:
        event = CampaignEvent(
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            event_type=event_type,
            provider=provider,
            provider_event_id=(provider_event_id or None),
            payload_metadata=redact(metadata or {}),
        )
        session.add(event)
        if commit:
            await session.commit()
        return event

    async def list_events(
        self, session: AsyncSession, *, campaign_id: uuid.UUID,
        recipient_id: uuid.UUID | None = None,
        event_type: str | None = None,
        page: int = 1, page_size: int = 100,
    ) -> tuple[list[CampaignEvent], int]:
        query = select(CampaignEvent).where(CampaignEvent.campaign_id == campaign_id)
        count_query = select(func.count()).select_from(CampaignEvent).where(
            CampaignEvent.campaign_id == campaign_id
        )
        if recipient_id is not None:
            query = query.where(CampaignEvent.recipient_id == recipient_id)
            count_query = count_query.where(CampaignEvent.recipient_id == recipient_id)
        if event_type:
            query = query.where(CampaignEvent.event_type == event_type.upper())
            count_query = count_query.where(CampaignEvent.event_type == event_type.upper())
        total = await session.scalar(count_query)
        rows = await session.execute(
            query.order_by(CampaignEvent.created_at.desc(), CampaignEvent.id.desc())
            .offset(max(0, page - 1) * page_size).limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def normalize_provider_event(
        self, registry: MarketingProviderRegistry, payload: dict,
    ) -> dict:
        """Provider Event → Event Normalizer → Campaign Event (§37).

        Returns {event_type, provider_message_id, metadata}. Unknown or
        malformed payloads raise ValidationError — never guess silently.
        """
        from app.core.errors import ValidationError

        if not isinstance(payload, dict):
            raise ValidationError("Event payload must be an object")
        provider_id = str(payload.get("provider") or "").strip()
        provider = registry.get(provider_id)
        if provider is None:
            raise ValidationError(f"Unknown provider: {provider_id!r}")
        raw_type = str(payload.get("event") or payload.get("event_type") or "").strip().lower()
        if not raw_type:
            raise ValidationError("Event payload is missing the event type")
        mapped = PROVIDER_EVENT_MAP.get(raw_type)
        if mapped is None:
            raise ValidationError(f"Unsupported provider event type: {raw_type!r}")
        normalized = await provider.handle_event({
            "event_type": mapped,
            "provider_message_id": payload.get("provider_message_id"),
            "metadata": payload.get("metadata") or {},
        })
        return normalized
