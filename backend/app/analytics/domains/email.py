"""Email analytics (spec §12) — provider events only, never fabricated.

Sources:
- `campaign_recipients` for EMAIL-channel campaigns (sent/delivered/bounced/
  complained/opened/clicked/replied timestamp facts written by real provider
  webhooks or the send pipeline)
- `campaign_events` MESSAGE_BOUNCED metadata for the hard/soft split

Open/click numbers exist ONLY where tracking events were recorded (opt-in per
campaign, §31 of the Phase 7 spec). When a campaign never produced tracking
events, open/click rates are None → displayed "not available", never guessed.
"""

from __future__ import annotations

from sqlalchemy import case, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.math import safe_rate
from app.analytics.core.query_builder import base_campaigns_query, in_period
from app.models.marketing import Campaign, CampaignEvent, CampaignRecipient, EventType

EMAIL = "EMAIL"


def _email_campaigns_query(filters: AnalyticsFilters):
    return base_campaigns_query(filters).where(Campaign.channel == EMAIL)


async def email_kpis(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    recipients = _email_campaigns_query(filters).outerjoin(
        CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id
    )
    rows = (await session.execute(
        recipients.with_only_columns(
            func.count(CampaignRecipient.id).label("recipients"),
            func.count(CampaignRecipient.sent_at).label("sent"),
            func.count(CampaignRecipient.delivered_at).label("delivered"),
            func.count(CampaignRecipient.bounced_at).label("bounced"),
            func.count(CampaignRecipient.complained_at).label("complained"),
            func.count(CampaignRecipient.opened_at).label("opened"),
            func.count(CampaignRecipient.clicked_at).label("clicked"),
            func.count(CampaignRecipient.replied_at).label("replied"),
        )
    )).first()

    events = (await session.execute(
        select(
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_UNSUBSCRIBED, 1), else_=0))
            .label("unsubscribed"),
        )
        .join(Campaign, Campaign.id == CampaignEvent.campaign_id)
        .where(Campaign.channel == EMAIL)
        .where(in_period(CampaignEvent.created_at, filters.period) if filters.period else true())
    )).first()

    sent = int(rows.sent or 0)
    delivered = int(rows.delivered or 0)
    bounced = int(rows.bounced or 0)
    complained = int(rows.complained or 0)
    opened = int(rows.opened or 0)
    clicked = int(rows.clicked or 0)
    replied = int(rows.replied or 0)

    hard_bounces, soft_bounces = await _bounce_split(session, filters)

    return {
        "channel": EMAIL,
        "recipients": int(rows.recipients or 0),
        "emails_sent": sent,
        "delivered": delivered,
        "bounced": bounced,
        "hard_bounces": hard_bounces,
        "soft_bounces": soft_bounces,
        "complained": complained,
        "failed": max(sent - delivered - bounced, 0),
        "replies": replied,
        # opens/clicks: reported only when actual tracking events exist (§12)
        "opens": opened,
        "clicks": clicked,
        "unsubscribed": int(events.unsubscribed or 0),
        "tracking_events_available": bool(opened or clicked),
        "rates": {
            "delivery_rate": safe_rate(delivered, sent),
            "bounce_rate": safe_rate(bounced, sent),
            "complaint_rate": safe_rate(complained, sent),
            "reply_rate": safe_rate(replied, delivered),
            "open_rate": safe_rate(opened, delivered) if opened else None,
            "click_rate": safe_rate(clicked, delivered) if clicked else None,
            "unsubscribe_rate": safe_rate(int(events.unsubscribed or 0), sent),
        },
    }


async def _bounce_split(session: AsyncSession, filters: AnalyticsFilters) -> tuple[int, int]:
    """Hard/soft split from the immutable bounce-event metadata (same
    definition as the Phase 7 campaign service — never recomputed differently)."""
    query = select(CampaignEvent.payload_metadata).join(
        Campaign, Campaign.id == CampaignEvent.campaign_id
    ).where(
        Campaign.channel == EMAIL,
        CampaignEvent.event_type == EventType.MESSAGE_BOUNCED,
    )
    if filters.period is not None:
        query = query.where(in_period(CampaignEvent.created_at, filters.period))
    metas = (await session.execute(query)).scalars().all()
    hard = soft = 0
    for meta in metas:
        btype = str((meta or {}).get("bounce_type") or "").upper()
        if btype == "HARD_BOUNCE":
            hard += 1
        elif btype == "SOFT_BOUNCE":
            soft += 1
    return hard, soft


async def per_sender_account(session: AsyncSession, filters: AnalyticsFilters) -> list[dict]:
    """Per sender-account volume/reputation rows (spec §12 'per sender account')."""
    from app.models.marketing import SendingAccount

    rows = (await session.execute(
        base_campaigns_query(filters).where(Campaign.channel == EMAIL)
        .outerjoin(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .with_only_columns(
            Campaign.sending_account_id.label("account_id"),
            func.count(CampaignRecipient.id).label("recipients"),
            func.count(CampaignRecipient.sent_at).label("sent"),
            func.count(CampaignRecipient.delivered_at).label("delivered"),
            func.count(CampaignRecipient.bounced_at).label("bounced"),
            func.count(CampaignRecipient.complained_at).label("complained"),
            func.count(CampaignRecipient.replied_at).label("replied"),
        ).group_by(Campaign.sending_account_id)
    )).all()

    accounts = (await session.execute(
        select(
            SendingAccount.id.label("id"),
            SendingAccount.display_identifier.label("identifier"),
            SendingAccount.provider.label("provider"),
            SendingAccount.health_status.label("health"),
        ).where(SendingAccount.channel == EMAIL)
    )).all()
    labels = {a.id: a for a in accounts}

    out = []
    for row in rows:
        account_id = row.account_id
        sent = int(row.sent or 0)
        delivered = int(row.delivered or 0)
        bounced = int(row.bounced or 0)
        complained = int(row.complained or 0)
        record = labels.get(account_id)
        out.append({
            "account_id": str(account_id) if account_id else "unlinked",
            "identifier": (record.identifier if record else None) or "Unknown sender",
            "provider": record.provider if record else None,
            "health_status": record.health if record else None,
            "recipients": int(row.recipients or 0),
            "sent": sent,
            "delivery_rate": safe_rate(delivered, sent),
            "bounce_rate": safe_rate(bounced, sent),
            "complaint_rate": safe_rate(complained, sent),
            "reply_rate": safe_rate(int(row.replied or 0), delivered),
        })
    return out
