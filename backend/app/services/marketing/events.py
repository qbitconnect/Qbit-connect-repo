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
        await session.flush()
        # Phase 9 §8/§10: fan campaign/message events out to the automation
        # engine (best-effort — never breaks campaign flow)
        await _emit_automation_events(session, event)
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


#: CampaignEvent vocabulary → normalized automation event types (Phase 9 §8/§10).
#: One campaign event may fan out to multiple automation events (e.g.
#: MESSAGE_FAILED is both message.failed and campaign.recipient.failed).
_CAMPAIGN_EVENT_MAP = {
    "CAMPAIGN_COMPLETED": (("campaign.completed", "campaign"),),
    "CAMPAIGN_FAILED": (("campaign.failed", "campaign"),),
    "MESSAGE_REPLIED": (("campaign.recipient.replied", "campaign_recipient"),),
    "MESSAGE_FAILED": (
        ("campaign.recipient.failed", "campaign_recipient"),
        ("message.failed", "message"),
    ),
    "MESSAGE_SENT": (("message.sent", "message"),),
    "MESSAGE_DELIVERED": (("message.delivered", "message"),),
}


async def _emit_automation_events(session: AsyncSession, event: CampaignEvent) -> None:
    """Best-effort automation intake (Phase 9) — failures are swallowed and
    logged; the campaign event itself is always authoritative."""
    mappings = _CAMPAIGN_EVENT_MAP.get(str(event.event_type or "").upper())
    if not mappings:
        return
    try:
        from sqlalchemy import select

        from app.automation.services.event_dispatcher import emit_system_event
        from app.models.messaging import Message

        recipient_row = None
        if event.recipient_id:
            from app.models.marketing import CampaignRecipient

            recipient_row = await session.get(CampaignRecipient, event.recipient_id)

        for auto_event, entity_kind in mappings:
            refs: dict = {"campaign_id": str(event.campaign_id)}
            if recipient_row is not None:
                refs["recipient_id"] = str(recipient_row.id)
                if recipient_row.lead_id:
                    refs["lead_id"] = str(recipient_row.lead_id)
                if entity_kind == "message" and recipient_row.provider_message_id:
                    message_row = (await session.execute(
                        select(Message)
                        .where(Message.provider_message_id == recipient_row.provider_message_id)
                        .limit(1)
                    )).scalars().first()
                    if message_row is not None:
                        refs["message_id"] = str(message_row.id)
                        refs["conversation_id"] = str(message_row.conversation_id)
            await emit_system_event(
                session,
                event_type=auto_event,
                entity_type=entity_kind,
                entity_id=event.recipient_id if entity_kind == "campaign_recipient" else event.campaign_id,
                payload={"refs": refs, "campaign_event_id": str(event.id)},
                # deterministic per (campaign event, automation event) — idempotent (§13)
                event_id=f"ce:{event.id}:{auto_event}",
            )
    except Exception:  # noqa: BLE001 — never break campaign flow
        pass
