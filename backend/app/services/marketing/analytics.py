"""Campaign analytics (Phase 5 §24).

All values are computed from actual campaign/recipient/queue/event rows —
nothing is fabricated, nothing is estimated. Rates are None-safe: a campaign
with zero recipients simply reports zeros.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import (
    Campaign,
    CampaignEvent,
    CampaignQueueItem,
    CampaignRecipient,
    CampaignStatus,
    EventType,
    RecipientStatus,
)


class AnalyticsService:
    async def campaign_analytics(self, session: AsyncSession, campaign_id: uuid.UUID) -> dict:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            return {}

        recipients = int(await session.scalar(
            select(func.count()).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id)
        ) or 0)

        status_rows = (await session.execute(
            select(CampaignRecipient.status, func.count())
            .where(CampaignRecipient.campaign_id == campaign_id)
            .group_by(CampaignRecipient.status)
        )).all()
        by_status = {status: count for status, count in status_rows}

        eligible = by_status.get(RecipientStatus.ELIGIBLE, 0) + by_status.get(RecipientStatus.QUEUED, 0) \
            + by_status.get(RecipientStatus.SENDING, 0) + by_status.get(RecipientStatus.SENT, 0) \
            + by_status.get(RecipientStatus.DELIVERED, 0) + by_status.get(RecipientStatus.READ, 0) \
            + by_status.get(RecipientStatus.REPLIED, 0) + by_status.get(RecipientStatus.FAILED, 0)
        skipped = by_status.get(RecipientStatus.INELIGIBLE, 0) + by_status.get(RecipientStatus.SKIPPED, 0)
        cancelled = by_status.get(RecipientStatus.CANCELLED, 0)

        sent = by_status.get(RecipientStatus.SENT, 0) + by_status.get(RecipientStatus.DELIVERED, 0) \
            + by_status.get(RecipientStatus.READ, 0) + by_status.get(RecipientStatus.REPLIED, 0)
        delivered = by_status.get(RecipientStatus.DELIVERED, 0) + by_status.get(RecipientStatus.READ, 0) \
            + by_status.get(RecipientStatus.REPLIED, 0)
        read = by_status.get(RecipientStatus.READ, 0) + by_status.get(RecipientStatus.REPLIED, 0)
        replied = by_status.get(RecipientStatus.REPLIED, 0)
        failed = by_status.get(RecipientStatus.FAILED, 0)

        queue_counts = await self._queue_counts(session, campaign_id)

        return {
            "campaign_id": str(campaign_id),
            "status": campaign.status,
            "recipients": {
                "total": recipients,
                "eligible": eligible,
                "skipped": skipped,
                "suppressed": by_status.get(RecipientStatus.INELIGIBLE, 0),
                "cancelled": cancelled,
                "by_status": by_status,
            },
            "messages": {
                "queued": queue_counts.get("WAITING", 0) + queue_counts.get("RETRY", 0)
                + queue_counts.get("PROCESSING", 0) + queue_counts.get("COMPLETED", 0),
                "sent": sent,
                "delivered": delivered,
                "read": read,
                "replied": replied,
                "failed": failed,
                "cancelled": queue_counts.get("CANCELLED", 0),
            },
            "rates": {
                # rates computed only over actually-sent messages (never fabricated)
                "delivery_rate": round(delivered / sent, 4) if sent else 0.0,
                "read_rate": round(read / sent, 4) if sent else 0.0,
                "reply_rate": round(replied / sent, 4) if sent else 0.0,
                "failure_rate": round(failed / (sent + failed), 4) if (sent + failed) else 0.0,
            },
        }

    async def email_campaign_analytics(
        self, session: AsyncSession, campaign_id: uuid.UUID,
    ) -> dict:
        """EMAIL channel analytics (Phase 7 §34).

        Every value comes from ACTUAL recorded events / recipient rows —
        nothing is fabricated, nothing is estimated. Open/click numbers are
        directional only (email clients block or prefetch tracking pixels,
        §31) and are reported without any accuracy claim.
        """
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            return {}
        base = await self.campaign_analytics(session, campaign_id)
        if not base:
            return base
        event_rows = (await session.execute(
            select(CampaignEvent.event_type, func.count())
            .where(CampaignEvent.campaign_id == campaign_id)
            .group_by(CampaignEvent.event_type)
        )).all()
        events = {event_type: int(count) for event_type, count in event_rows}

        ts_rows = (await session.execute(
            select(
                func.count(CampaignRecipient.bounced_at),
                func.count(CampaignRecipient.complained_at),
                func.count(CampaignRecipient.opened_at),
                func.count(CampaignRecipient.clicked_at),
                func.count(CampaignRecipient.replied_at),
            ).where(CampaignRecipient.campaign_id == campaign_id)
        )).first() or (0, 0, 0, 0, 0)
        bounced = max(events.get(EventType.MESSAGE_BOUNCED, 0), int(ts_rows[0] or 0))
        complained = max(events.get(EventType.MESSAGE_COMPLAINED, 0), int(ts_rows[1] or 0))
        opened = max(events.get(EventType.MESSAGE_OPENED, 0), int(ts_rows[2] or 0))
        clicked = max(events.get(EventType.MESSAGE_CLICKED, 0), int(ts_rows[3] or 0))
        replied = max(events.get(EventType.MESSAGE_REPLIED, 0), int(ts_rows[4] or 0))
        unsubscribed = events.get(EventType.MESSAGE_UNSUBSCRIBED, 0)

        sent = base["messages"]["sent"]
        delivered = base["messages"]["delivered"]
        recipients_total = base["recipients"]["total"] or 0

        # §34: email rates come from ACTUAL events — the worker appends a
        # MESSAGE_SENT per accepted send; provider webhooks append the rest
        event_sent = events.get(EventType.MESSAGE_SENT, 0)
        event_delivered = events.get(EventType.MESSAGE_DELIVERED, 0)

        base["email"] = {
            "channel": campaign.channel,
            "events": {
                "sent": event_sent,
                "delivered": event_delivered,
                "bounced": bounced,
                "complained": complained,
                "opened": opened,
                "clicked": clicked,
                "replied": replied,
                "unsubscribed": unsubscribed,
                "hard_bounces": 0,   # refined below from event metadata
                "soft_bounces": 0,
            },
            "rates": {
                "delivery_rate": round(event_delivered / event_sent, 4) if event_sent else 0.0,
                "bounce_rate": round(bounced / event_sent, 4) if event_sent else 0.0,
                "complaint_rate": round(complained / event_sent, 4) if event_sent else 0.0,
                "open_rate": round(opened / event_sent, 4) if event_sent else 0.0,
                "click_rate": round(clicked / event_sent, 4) if event_sent else 0.0,
                "reply_rate": round(replied / event_sent, 4) if event_sent else 0.0,
                "unsubscribe_rate": round(unsubscribed / event_sent, 4) if event_sent else 0.0,
            },
            "audience": {
                "recipients": recipients_total,
                "eligible": base["recipients"]["eligible"],
                "skipped": base["recipients"]["skipped"],
                "queued": base["messages"]["queued"],
            },
            "tracking_note": (
                "Open/click tracking is approximate: email clients may block "
                "or prefetch tracking pixels. Values are directional."
            ),
        }

        # hard/soft bounce split from the immutable event metadata
        hard = soft = 0
        bounce_rows = (await session.execute(
            select(CampaignEvent.payload_metadata)
            .where(
                CampaignEvent.campaign_id == campaign_id,
                CampaignEvent.event_type == EventType.MESSAGE_BOUNCED,
            )
        )).scalars().all()
        for meta in bounce_rows:
            btype = str((meta or {}).get("bounce_type") or "").upper()
            if btype == "HARD_BOUNCE":
                hard += 1
            elif btype == "SOFT_BOUNCE":
                soft += 1
        base["email"]["events"]["hard_bounces"] = hard
        base["email"]["events"]["soft_bounces"] = soft
        return base

    async def _queue_counts(self, session: AsyncSession, campaign_id: uuid.UUID) -> dict:
        rows = (await session.execute(
            select(CampaignQueueItem.status, func.count())
            .where(CampaignQueueItem.campaign_id == campaign_id)
            .group_by(CampaignQueueItem.status)
        )).all()
        return {status: count for status, count in rows}

    async def dashboard_totals(self, session: AsyncSession) -> dict:
        """Campaign dashboard cards (§25) — real aggregates only."""
        total = int(await session.scalar(select(func.count()).select_from(Campaign)) or 0)
        by_status_rows = (await session.execute(
            select(Campaign.status, func.count()).group_by(Campaign.status)
        )).all()
        by_status = {status: count for status, count in by_status_rows}
        return {
            "total_campaigns": total,
            "drafts": by_status.get(CampaignStatus.DRAFT, 0),
            "scheduled": by_status.get(CampaignStatus.SCHEDULED, 0),
            "queued": by_status.get(CampaignStatus.QUEUED, 0),
            "running": by_status.get(CampaignStatus.RUNNING, 0),
            "paused": by_status.get(CampaignStatus.PAUSED, 0),
            "completed": by_status.get(CampaignStatus.COMPLETED, 0),
            "cancelled": by_status.get(CampaignStatus.CANCELLED, 0),
            "failed": by_status.get(CampaignStatus.FAILED, 0),
            "archived": by_status.get(CampaignStatus.ARCHIVED, 0),
        }

    async def event_counts(self, session: AsyncSession, campaign_id: uuid.UUID) -> dict:
        rows = (await session.execute(
            select(CampaignEvent.event_type, func.count())
            .where(CampaignEvent.campaign_id == campaign_id)
            .group_by(CampaignEvent.event_type)
        )).all()
        return {event_type: count for event_type, count in rows}
