"""Inbox / conversation analytics (spec §13) — conversations, statuses,
assignment and response times.

Response-time definitions (spec §13, honest-timestamp rule):
- first response time: min(outbound message created_at) − min(inbound
  created_at) per conversation, only over conversations that have BOTH —
  conversations missing either timestamp are excluded, never imputed
- resolution time: closed_at − created_at over conversations with closed_at
- averages are means over the qualifying sets; the qualifying count is
  returned so empty sets are visible ("no data available")
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.math import safe_rate
from app.analytics.core.query_builder import base_conversations_query, in_period
from app.analytics.core.time import Period, day_bucket
from app.models.messaging import Conversation, ConversationEvent, Message

OPEN_STATUSES = ("OPEN", "PENDING", "WAITING")


async def inbox_kpis(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    base = base_conversations_query(filters)
    rows = (await session.execute(
        base.with_only_columns(Conversation.status, func.count(Conversation.id))
        .group_by(Conversation.status)
    )).all()
    by_status = {status: int(n) for status, n in rows}

    assigned = int(await session.scalar(
        base.with_only_columns(func.count(Conversation.id))
        .where(Conversation.assigned_user_id.is_not(None))
    ) or 0)
    unassigned = int(await session.scalar(
        base.with_only_columns(func.count(Conversation.id))
        .where(Conversation.assigned_user_id.is_(None))
    ) or 0)
    unread = int(await session.scalar(
        base.with_only_columns(func.count(Conversation.id))
        .where(Conversation.unread_count > 0)
    ) or 0)
    reopened = 0
    if filters.period is not None:
        reopened = int(await session.scalar(
            select(func.count(ConversationEvent.id))
            .where(
                ConversationEvent.event_type == "CONVERSATION_REOPENED",
                in_period(ConversationEvent.created_at, filters.period),
            )
        ) or 0)

    total = sum(by_status.values())
    return {
        "conversations_total": total,
        "new": by_status.get("NEW", 0),
        "open": by_status.get("OPEN", 0),
        "pending": by_status.get("PENDING", 0),
        "waiting": by_status.get("WAITING", 0),
        "resolved": by_status.get("RESOLVED", 0),
        "closed": by_status.get("CLOSED", 0),
        "by_status": by_status,
        "reopened": reopened,
        "unread": unread,
        "assigned": assigned,
        "unassigned": unassigned,
        "resolution_rate": safe_rate(
            by_status.get("RESOLVED", 0) + by_status.get("CLOSED", 0), total
        ),
    }


async def conversations_timeseries(
    session: AsyncSession, filters: AnalyticsFilters, tz, dialect: str,
) -> list[dict]:
    period: Period = filters.period
    bucket = day_bucket(Conversation.created_at, str(tz), period.start, period.end,
                        dialect=dialect)
    rows = (await session.execute(
        base_conversations_query(filters).with_only_columns(
            bucket.label("day"), func.count(Conversation.id).label("created"),
        ).group_by(bucket).order_by(bucket)
    )).all()
    return [{"day": str(row.day), "created": int(row.created)} for row in rows]


async def conversation_distribution(
    session: AsyncSession, filters: AnalyticsFilters, dimension: str,
    limit: int = 12,
) -> list[dict]:
    column_map = {
        "channel": Conversation.channel,
        "status": Conversation.status,
        "priority": Conversation.priority,
        "assigned_user": Conversation.assigned_user_id,
        "match_status": Conversation.match_status,
    }
    column = column_map.get(dimension)
    if column is None:
        raise ValueError(f"Unsupported conversation dimension: {dimension}")
    rows = (await session.execute(
        base_conversations_query(filters).with_only_columns(
            column.label("value"), func.count(Conversation.id).label("count"),
        ).group_by(column).order_by(func.count(Conversation.id).desc()).limit(limit)
    )).all()
    return [{"value": str(row.value) if row.value is not None else "Unassigned" if dimension == "assigned_user" else "Unknown",
             "count": int(row.count)} for row in rows]


async def response_analytics(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    """First-response/resolution times from REAL timestamps only (spec §13)."""
    base = base_conversations_query(filters).subquery()

    # per-conversation first inbound / first outbound timestamps
    first_in = (
        select(
            Message.conversation_id.label("conversation_id"),
            func.min(Message.created_at).label("first_in"),
        )
        .where(Message.direction == "IN")
        .group_by(Message.conversation_id)
        .subquery()
    )
    first_out = (
        select(
            Message.conversation_id.label("conversation_id"),
            func.min(Message.created_at).label("first_out"),
        )
        .where(Message.direction == "OUT")
        .group_by(Message.conversation_id)
        .subquery()
    )

    joined = (
        select(
            first_in.c.conversation_id.label("cid"),
            first_in.c.first_in,
            first_out.c.first_out,
        )
        .join(base, base.c.id == first_in.c.conversation_id)
        .outerjoin(first_out, first_out.c.conversation_id == first_in.c.conversation_id)
        .subquery()
    )

    rows = (await session.execute(
        select(
            func.count(joined.c.cid).label("with_inbound"),
            func.sum(case((joined.c.first_out.is_not(None), 1), else_=0)).label("with_both"),
            func.avg(
                func.extract("epoch", joined.c.first_out)
                - func.extract("epoch", joined.c.first_in)
            ).label("avg_first_response"),
        )
    )).first()

    resolution = (await session.execute(
        select(
            func.count(base.c.closed_at).label("closed_count"),
            func.avg(
                func.extract("epoch", base.c.closed_at)
                - func.extract("epoch", base.c.created_at)
            ).label("avg_resolution"),
        ).select_from(base)
    )).first()

    msg_counts = (await session.execute(
        select(func.count(Message.id)).join(
            base, base.c.id == Message.conversation_id,
        )
    )).scalar()
    convo_count = (await session.execute(
        select(func.count()).select_from(base)
    )).scalar()

    return {
        "conversations_with_inbound": int(rows.with_inbound or 0),
        "conversations_with_response": int(rows.with_both or 0),
        # None when no conversation qualifies — never a fabricated average
        "avg_first_response_seconds": round(float(rows.avg_first_response), 1)
        if rows.avg_first_response is not None else None,
        "closed_conversations": int(resolution.closed_count or 0),
        "avg_resolution_seconds": round(float(resolution.avg_resolution), 1)
        if resolution.avg_resolution is not None else None,
        "avg_messages_per_conversation": round(float(msg_counts or 0) / convo_count, 2)
        if convo_count else None,
    }
