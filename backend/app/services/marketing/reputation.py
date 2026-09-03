"""Sender reputation monitoring — FOUNDATION ONLY (Phase 7 §43).

Per-email-sending-account delivery metrics computed from ACTUAL campaign
events (never fabricated, never estimated) plus threshold warnings:

    bounce rate    = bounced / sent
    complaint rate = complained / sent
    delivery rate  = delivered / sent

What this is NOT:
- NOT an inbox-placement guarantee (explicitly out of scope, §43)
- NOT a spam-filter analysis, NOT a reputation-manipulation tool — no
  spam-filter bypass techniques exist anywhere in this platform
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.marketing import (
    Campaign,
    CampaignEvent,
    EventType,
    SendingAccount,
)


class SenderReputationService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    async def account_metrics(
        self, session: AsyncSession, account_id: uuid.UUID, *,
        settings: Settings | None = None,
    ) -> dict:
        """Delivery/bounce/complaint metrics for ONE email sending account."""
        cfg = settings or self.settings
        sent = await self._event_count(session, account_id, EventType.MESSAGE_SENT)
        delivered = await self._event_count(session, account_id, EventType.MESSAGE_DELIVERED)
        bounced = await self._event_count(session, account_id, EventType.MESSAGE_BOUNCED)
        complained = await self._event_count(session, account_id, EventType.MESSAGE_COMPLAINED)
        opened = await self._event_count(session, account_id, EventType.MESSAGE_OPENED)
        clicked = await self._event_count(session, account_id, EventType.MESSAGE_CLICKED)
        unsubscribed = await self._event_count(session, account_id, EventType.MESSAGE_UNSUBSCRIBED)

        # delivered-count includes downstream states (read/replied) via status? —
        # no: rates come from EVENTS only (§34: all values from actual events)
        delivery_rate = round(delivered / sent, 4) if sent else 0.0
        bounce_rate = round(bounced / sent, 4) if sent else 0.0
        complaint_rate = round(complained / sent, 4) if sent else 0.0

        warnings: list[str] = []
        if cfg is not None and sent:
            if bounce_rate >= cfg.QBIT_EMAIL_BOUNCE_WARN_RATE:
                warnings.append(
                    f"Bounce rate {bounce_rate:.1%} is at or above the warning threshold "
                    f"({cfg.QBIT_EMAIL_BOUNCE_WARN_RATE:.1%}) — review list quality"
                )
            if complaint_rate >= cfg.QBIT_EMAIL_COMPLAINT_WARN_RATE:
                warnings.append(
                    f"Complaint rate {complaint_rate:.1%} is at or above the warning "
                    f"threshold ({cfg.QBIT_EMAIL_COMPLAINT_WARN_RATE:.1%}) — review targeting "
                    f"and consent"
                )

        return {
            "sending_account_id": str(account_id),
            "sent": sent,
            "delivered": delivered,
            "bounced": bounced,
            "complained": complained,
            "opened": opened,
            "clicked": clicked,
            "unsubscribed": unsubscribed,
            "rates": {
                "delivery_rate": delivery_rate,
                "bounce_rate": bounce_rate,
                "complaint_rate": complaint_rate,
                "open_rate": round(opened / sent, 4) if sent else 0.0,
                "click_rate": round(clicked / sent, 4) if sent else 0.0,
                "unsubscribe_rate": round(unsubscribed / sent, 4) if sent else 0.0,
            },
            "warnings": warnings,
        }

    # ---------------------------------------------------------------- helpers
    async def _event_count(
        self, session: AsyncSession, account_id: uuid.UUID, event_type: str,
    ) -> int:
        """Count events for campaigns that used this sending account."""
        return int(await session.scalar(
            select(func.count())
            .select_from(CampaignEvent)
            .join(Campaign, Campaign.id == CampaignEvent.campaign_id)
            .where(
                Campaign.sending_account_id == account_id,
                CampaignEvent.event_type == event_type,
            )
        ) or 0)
