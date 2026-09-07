"""Marketing analytics (spec §9, §10) — campaigns, recipients and the
immutable CampaignEvent stream.

Metric definitions (spec §28):
- sent/delivered/read/replied/failed/unsubscribed come from CampaignEvent rows
  in the period (the immutable stream), cross-checked against recipient status
  counts where both exist (max of the two is NEVER used to inflate numbers;
  events are authoritative, recipient status is the operational present)
- delivery rate  delivered / sent (events)
- failure rate   failed / (sent + failed) attempts
- reply rate     replied / delivered
- zero denominators return None (rendered as "—"), never estimates
- open/click metrics exist ONLY where tracking events exist (email, opt-in)
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.math import safe_rate
from app.analytics.core.query_builder import base_campaigns_query, base_recipients_query, in_period
from app.analytics.core.time import Period, day_bucket
from app.models.marketing import (
    Campaign,
    CampaignEvent,
    CampaignQueueItem,
    CampaignRecipient,
    CampaignStatus,
    EventType,
)

TERMINAL_OK = (CampaignStatus.COMPLETED, CampaignStatus.ARCHIVED)


async def marketing_kpis(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    campaigns = base_campaigns_query(filters)
    status_rows = (await session.execute(
        campaigns.with_only_columns(Campaign.status, func.count(Campaign.id))
        .group_by(Campaign.status)
    )).all()
    by_status = {status: int(n) for status, n in status_rows}

    recipients = int(await session.scalar(
        base_recipients_query(filters).with_only_columns(func.count(CampaignRecipient.id))
    ) or 0)

    events = await _event_counts(session, filters)
    queue_rows = (await session.execute(
        select(CampaignQueueItem.status, func.count(CampaignQueueItem.id))
        .group_by(CampaignQueueItem.status)
    )).all()
    queue = {status: int(n) for status, n in queue_rows}

    sent = events.get(EventType.MESSAGE_SENT, 0)
    delivered = events.get(EventType.MESSAGE_DELIVERED, 0)
    failed = events.get(EventType.MESSAGE_FAILED, 0)
    replied = events.get(EventType.MESSAGE_REPLIED, 0)
    unsubscribed = events.get(EventType.MESSAGE_UNSUBSCRIBED, 0)

    return {
        "campaigns_total": sum(by_status.values()),
        "campaigns_completed": by_status.get(CampaignStatus.COMPLETED, 0),
        "campaigns_running": by_status.get(CampaignStatus.RUNNING, 0)
        + by_status.get(CampaignStatus.QUEUED, 0)
        + by_status.get(CampaignStatus.SCHEDULED, 0),
        "campaigns_paused": by_status.get(CampaignStatus.PAUSED, 0),
        "campaigns_failed": by_status.get(CampaignStatus.FAILED, 0),
        "campaigns_cancelled": by_status.get(CampaignStatus.CANCELLED, 0),
        "by_status": by_status,
        "recipients": recipients,
        "messages_queued": events.get(EventType.MESSAGE_QUEUED, 0) + queue.get("WAITING", 0)
        + queue.get("RETRY", 0),
        "messages_sent": sent,
        "messages_delivered": delivered,
        "messages_failed": failed,
        "replies": replied,
        "unsubscribed": unsubscribed,
        "rates": {
            "delivery_rate": safe_rate(delivered, sent),
            "failure_rate": safe_rate(failed, sent + failed),
            "reply_rate": safe_rate(replied, delivered),
            "unsubscribe_rate": safe_rate(unsubscribed, sent),
        },
    }


async def _event_counts(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    """Counts over the immutable CampaignEvent stream, scoped by campaign
    attributes (channel/account/campaign) — the event table itself has no
    channel column, so the join supplies the filter dimensions."""
    query = select(
        CampaignEvent.event_type,
        func.count(CampaignEvent.id),
    ).join(Campaign, Campaign.id == CampaignEvent.campaign_id)
    if filters.period is not None:
        query = query.where(in_period(CampaignEvent.created_at, filters.period))
    query = query.where(
        Campaign.channel.in_(filters.channel) if filters.channel else true(),
        Campaign.sending_account_id.in_(filters.sending_account_id)
        if filters.sending_account_id else true(),
        Campaign.id.in_(filters.campaign_id) if filters.campaign_id else true(),
    )
    rows = (await session.execute(
        query.group_by(CampaignEvent.event_type)
    )).all()
    return {event_type: int(n) for event_type, n in rows}


async def campaigns_timeseries(
    session: AsyncSession, filters: AnalyticsFilters, tz, dialect: str,
) -> list[dict]:
    """Campaigns created + message events per day."""
    period: Period = filters.period
    bucket = day_bucket(Campaign.created_at, str(tz), period.start, period.end, dialect=dialect)
    created_rows = (await session.execute(
        base_campaigns_query(filters).with_only_columns(
            bucket.label("day"), func.count(Campaign.id).label("created"),
        ).group_by(bucket).order_by(bucket)
    )).all()

    event_bucket = day_bucket(CampaignEvent.created_at, str(tz), period.start, period.end,
                              dialect=dialect)
    event_query = select(
        event_bucket.label("day"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_SENT, 1), else_=0)).label("sent"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_DELIVERED, 1), else_=0)).label("delivered"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_FAILED, 1), else_=0)).label("failed"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_REPLIED, 1), else_=0)).label("replied"),
    ).join(Campaign, Campaign.id == CampaignEvent.campaign_id).where(
        in_period(CampaignEvent.created_at, period)
    )
    event_query = event_query.where(
        Campaign.channel.in_(filters.channel) if filters.channel else true(),
        Campaign.sending_account_id.in_(filters.sending_account_id)
        if filters.sending_account_id else true(),
    )
    event_rows = (await session.execute(event_query.group_by(event_bucket).order_by(event_bucket))).all()

    series: dict[str, dict] = {}
    for row in created_rows:
        series[str(row.day)] = {
            "day": str(row.day), "campaigns_created": int(row.created),
            "sent": 0, "delivered": 0, "failed": 0, "replied": 0,
        }
    for row in event_rows:
        day = str(row.day)
        series.setdefault(day, {
            "day": day, "campaigns_created": 0, "sent": 0, "delivered": 0,
            "failed": 0, "replied": 0,
        })
        series[day].update({
            "sent": int(row.sent or 0), "delivered": int(row.delivered or 0),
            "failed": int(row.failed or 0), "replied": int(row.replied or 0),
        })
    return [series[key] for key in sorted(series)]


async def channel_comparison(session: AsyncSession, filters: AnalyticsFilters) -> list[dict]:
    """Sent/delivered/replied/failed grouped by channel from events."""
    query = select(
        Campaign.channel.label("channel"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_SENT, 1), else_=0)).label("sent"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_DELIVERED, 1), else_=0)).label("delivered"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_FAILED, 1), else_=0)).label("failed"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_REPLIED, 1), else_=0)).label("replied"),
        func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_UNSUBSCRIBED, 1), else_=0)).label("unsubscribed"),
    ).select_from(CampaignEvent).join(Campaign, Campaign.id == CampaignEvent.campaign_id).where(
        in_period(CampaignEvent.created_at, filters.period)
    )
    query = query.where(
        Campaign.channel.in_(filters.channel) if filters.channel else true(),
        Campaign.sending_account_id.in_(filters.sending_account_id)
        if filters.sending_account_id else true(),
    )
    rows = (await session.execute(query.group_by(Campaign.channel))).all()
    out = []
    for row in rows:
        sent = int(row.sent or 0)
        delivered = int(row.delivered or 0)
        out.append({
            "value": row.channel,
            "sent": sent,
            "delivered": delivered,
            "failed": int(row.failed or 0),
            "replied": int(row.replied or 0),
            "unsubscribed": int(row.unsubscribed or 0),
            "delivery_rate": safe_rate(delivered, sent),
            "reply_rate": safe_rate(int(row.replied or 0), delivered),
        })
    return out


async def campaign_list_performance(
    session: AsyncSession, filters: AnalyticsFilters, limit: int = 20,
) -> list[dict]:
    """Top-level per-campaign rows for tables (spec §10 header fields)."""
    from app.models.marketing import CampaignRecipient, RecipientStatus

    SENT_STATUSES = (
        RecipientStatus.SENT, RecipientStatus.DELIVERED, RecipientStatus.READ,
        RecipientStatus.REPLIED, RecipientStatus.FAILED,
    )
    DELIVERED_STATUSES = (RecipientStatus.DELIVERED, RecipientStatus.READ, RecipientStatus.REPLIED)

    rows = (await session.execute(
        base_campaigns_query(filters).with_only_columns(
            Campaign.id.label("id"),
            Campaign.name.label("name"),
            Campaign.channel.label("channel"),
            Campaign.status.label("status"),
            Campaign.started_at.label("started_at"),
            Campaign.completed_at.label("completed_at"),
            func.count(CampaignRecipient.id).label("recipients"),
            func.sum(case((CampaignRecipient.status.in_(SENT_STATUSES), 1), else_=0)).label("sent"),
            func.sum(case((CampaignRecipient.status.in_(DELIVERED_STATUSES), 1), else_=0)).label("delivered"),
            func.sum(case((CampaignRecipient.status == RecipientStatus.FAILED, 1), else_=0)).label("failed"),
            func.sum(case((CampaignRecipient.status == RecipientStatus.REPLIED, 1), else_=0)).label("replied"),
        )
        .outerjoin(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .group_by(Campaign.id)
        .order_by(Campaign.created_at.desc())
        .limit(limit)
    )).all()
    return [
        {
            "campaign_id": str(row.id),
            "name": row.name,
            "channel": row.channel,
            "status": row.status,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "recipients": int(row.recipients or 0),
            "sent": int(row.sent or 0),
            "delivered": int(row.delivered or 0),
            "failed": int(row.failed or 0),
            "replied": int(row.replied or 0),
            "delivery_rate": safe_rate(int(row.delivered or 0), int(row.sent or 0)),
            "reply_rate": safe_rate(int(row.replied or 0), int(row.delivered or 0)),
        }
        for row in rows
    ]


async def campaign_timeline(
    session: AsyncSession, campaign_id, tz, dialect: str,
) -> list[dict]:
    """Event progression for ONE campaign (spec §10) from the immutable stream."""
    from datetime import datetime, timezone as dt_timezone

    start = await session.scalar(
        select(func.min(CampaignEvent.created_at)).where(CampaignEvent.campaign_id == campaign_id)
    )
    end = await session.scalar(
        select(func.max(CampaignEvent.created_at)).where(CampaignEvent.campaign_id == campaign_id)
    )
    now = datetime.now(dt_timezone.utc)
    period_start = start or now - timedelta(days=1)
    period_end = end or now
    bucket = day_bucket(CampaignEvent.created_at, str(tz), period_start, period_end,
                        dialect=dialect)
    rows = (await session.execute(
        select(
            bucket.label("day"),
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_QUEUED, 1), else_=0)).label("queued"),
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_SENT, 1), else_=0)).label("sent"),
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_DELIVERED, 1), else_=0)).label("delivered"),
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_READ, 1), else_=0)).label("read"),
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_REPLIED, 1), else_=0)).label("replied"),
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_FAILED, 1), else_=0)).label("failed"),
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_BOUNCED, 1), else_=0)).label("bounced"),
            func.sum(case((CampaignEvent.event_type == EventType.MESSAGE_UNSUBSCRIBED, 1), else_=0)).label("unsubscribed"),
        ).where(CampaignEvent.campaign_id == campaign_id)
        .group_by(bucket).order_by(bucket)
    )).all()
    return [
        {
            "day": str(row.day),
            "queued": int(row.queued or 0),
            "sent": int(row.sent or 0),
            "delivered": int(row.delivered or 0),
            "read": int(row.read or 0),
            "replied": int(row.replied or 0),
            "failed": int(row.failed or 0),
            "bounced": int(row.bounced or 0),
            "unsubscribed": int(row.unsubscribed or 0),
        }
        for row in rows
    ]
