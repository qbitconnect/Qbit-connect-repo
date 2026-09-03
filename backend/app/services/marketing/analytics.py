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
