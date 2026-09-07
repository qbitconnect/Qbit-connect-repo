"""WhatsApp analytics (spec §11) — REAL provider data only.

Source tables: `messages` + `conversations` (Phase 6/8) and sending accounts.
- read counts come exclusively from `messages.read_at` written by actual
  WhatsApp read-event webhooks — if the provider never delivered read events,
  read metrics are 0/None and rates are None ("not available"), never inferred
- response activity = outbound messages following an inbound message in the
  same conversation (computed from stored timestamps; nothing simulated)
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.math import safe_rate
from app.analytics.core.query_builder import base_conversations_query, base_messages_query
from app.models.marketing import SendingAccount
from app.models.messaging import Conversation, Message

OUTBOUND = "OUT"
INBOUND = "IN"

SENT_STATUSES = ("SENT", "DELIVERED", "READ", "REPLIED")
DELIVERED_STATUSES = ("DELIVERED", "READ", "REPLIED")
READ_STATUSES = ("READ", "REPLIED")


def _channel_clause(filters: AnalyticsFilters):
    return Conversation.channel == "WHATSAPP"


async def whatsapp_kpis(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    messages = base_messages_query(filters).where(_channel_clause(filters))
    outbound = messages.where(Message.direction == OUTBOUND)
    inbound = messages.where(Message.direction == INBOUND)

    out_rows = (await session.execute(
        outbound.with_only_columns(
            func.count(Message.id).label("total"),
            func.count(Message.delivered_at).label("delivered"),
            func.count(Message.read_at).label("read"),
            func.count(Message.failed_at).label("failed"),
            func.count(Message.sent_at).label("sent"),
        )
    )).first()
    in_total = int(await session.scalar(
        inbound.with_only_columns(func.count(Message.id))
    ) or 0)
    replies = in_total  # inbound messages ARE replies on WhatsApp threads

    conversations = base_conversations_query(filters).where(_channel_clause(filters))
    convo_total = int(await session.scalar(
        conversations.with_only_columns(func.count(Conversation.id))
    ) or 0)
    active = int(await session.scalar(
        conversations.with_only_columns(func.count(Conversation.id))
        .where(Conversation.status.in_(("OPEN", "PENDING", "WAITING")))
    ) or 0)

    sent = int(out_rows.sent or 0)
    delivered = int(out_rows.delivered or 0)
    failed = int(out_rows.failed or 0)
    read = int(out_rows.read or 0)

    return {
        "channel": "WHATSAPP",
        "messages_sent": sent,
        "messages_delivered": delivered,
        "messages_read": read,
        "messages_failed": failed,
        "replies": replies,
        "incoming_messages": in_total,
        "outgoing_messages": int(out_rows.total or 0),
        "conversations_total": convo_total,
        "conversations_active": active,
        "rates": {
            # reads are real webhook events only; if none were received the
            # rate is None and the UI shows "not available" (spec §11)
            "delivery_rate": safe_rate(delivered, sent),
            "failure_rate": safe_rate(failed, sent + failed),
            "read_rate": safe_rate(read, delivered) if read else None,
            "reply_rate": safe_rate(replies, delivered),
        },
        "read_events_available": bool(read),
    }


async def per_account(session: AsyncSession, filters: AnalyticsFilters) -> list[dict]:
    """Per sending-account WhatsApp metrics + stored health status."""
    accounts = (await session.execute(
        select(
            SendingAccount.id.label("id"),
            SendingAccount.display_identifier.label("identifier"),
            SendingAccount.provider.label("provider"),
            SendingAccount.health_status.label("health"),
        ).where(SendingAccount.channel == "WHATSAPP")
    )).all()

    convo_rows = (await session.execute(
        base_conversations_query(filters)
        .where(_channel_clause(filters), Conversation.sending_account_id.is_not(None))
        .with_only_columns(
            Conversation.sending_account_id.label("account_id"),
            func.count(Conversation.id).label("conversations"),
        ).group_by(Conversation.sending_account_id)
    )).all()
    convo_by_account = {row.account_id: int(row.conversations) for row in convo_rows}

    msg_rows = (await session.execute(
        base_messages_query(filters)
        .where(_channel_clause(filters), Message.direction == OUTBOUND,
               Conversation.sending_account_id.is_not(None))
        .with_only_columns(
            Conversation.sending_account_id.label("account_id"),
            func.count(Message.id).label("sent"),
            func.count(Message.delivered_at).label("delivered"),
            func.count(Message.read_at).label("read"),
            func.count(Message.failed_at).label("failed"),
        ).group_by(Conversation.sending_account_id)
    )).all()
    msg_by_account = {row.account_id: row for row in msg_rows}

    out = []
    for account in accounts:
        row = msg_by_account.get(account.id)
        sent = int(row.sent or 0) if row else 0
        delivered = int(row.delivered or 0) if row else 0
        failed = int(row.failed or 0) if row else 0
        read = int(row.read or 0) if row else 0
        out.append({
            "account_id": str(account.id),
            "identifier": account.identifier or account.provider,
            "provider": account.provider,
            "health_status": account.health,
            "messages_sent": sent,
            "delivery_rate": safe_rate(delivered, sent),
            "failure_rate": safe_rate(failed, sent + failed),
            "reads": read,
            "read_rate": safe_rate(read, delivered) if read else None,
            "active_conversations": convo_by_account.get(account.id, 0),
        })
    return out


async def response_activity(
    session: AsyncSession, filters: AnalyticsFilters, tz, dialect: str,
) -> list[dict]:
    """Inbound vs outbound message volume per day (real timestamps only)."""
    from app.analytics.core.time import Period, day_bucket

    period: Period = filters.period
    bucket = day_bucket(Message.created_at, str(tz), period.start, period.end, dialect=dialect)
    rows = (await session.execute(
        base_messages_query(filters)
        .where(_channel_clause(filters))
        .with_only_columns(
            bucket.label("day"),
            func.sum(case((Message.direction == OUTBOUND, 1), else_=0)).label("outbound"),
            func.sum(case((Message.direction == INBOUND, 1), else_=0)).label("inbound"),
        ).group_by(bucket).order_by(bucket)
    )).all()
    return [
        {
            "day": str(row.day),
            "outbound": int(row.outbound or 0),
            "inbound": int(row.inbound or 0),
        }
        for row in rows
    ]
