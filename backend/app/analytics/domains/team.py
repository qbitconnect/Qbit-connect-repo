"""Team performance analytics (spec §14) — attributed activity only.

Visibility rules (spec §14): the service layer passes `allowed_user_ids` —
the caller (API/UI) computes the set of users whose data the current viewer
may see according to the established ALL/TEAM/ASSIGNED_ONLY visibility rules.
No user outside that set is ever aggregated.

Data sources (existing attribution fields only):
- leads created/updated   → Lead.created_by / LeadActivity(user_id, event_type)
- replies sent            → ConversationEvent(actor_user_id, MESSAGE_SENT)
- conversations assigned  → ConversationEvent(actor_user_id, ASSIGNED)
- conversations resolved  → ConversationEvent(STATUS_CHANGED, new_value.status
  in RESOLVED/CLOSED) — payload matching done in Python for portability
- campaign/automation activity by the user surfaces through the audit trail
  and workflow executions; counts shown where attribution exists
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.query_builder import in_period
from app.models.lead import LeadActivity
from app.models.messaging import ConversationEvent
from app.models.scrape import Lead
from app.models.user import User

RESOLVED_STATUSES = ("RESOLVED", "CLOSED")


async def team_overview(
    session: AsyncSession,
    filters: AnalyticsFilters,
    allowed_user_ids: list,
) -> list[dict]:
    """Per-user activity rows for users in `allowed_user_ids` (spec §14)."""
    if not allowed_user_ids:
        return []

    users = (await session.execute(
        select(User.id, User.full_name, User.email)
        .where(User.id.in_(allowed_user_ids))
        .order_by(User.email)
    )).all()

    leads_created = dict((await session.execute(
        select(Lead.created_by, func.count(Lead.id))
        .where(
            Lead.created_by.in_(allowed_user_ids),
            in_period(Lead.created_at, filters.period),
        )
        .group_by(Lead.created_by)
    )).all())

    activity_rows = (await session.execute(
        select(LeadActivity.user_id, LeadActivity.event_type, func.count(LeadActivity.id))
        .where(
            LeadActivity.user_id.in_(allowed_user_ids),
            in_period(LeadActivity.created_at, filters.period),
        )
        .group_by(LeadActivity.user_id, LeadActivity.event_type)
    )).all()
    activity: dict = {}
    for user_id, event_type, count in activity_rows:
        activity.setdefault(user_id, {})[event_type] = int(count)

    event_rows = (await session.execute(
        select(
            ConversationEvent.actor_user_id,
            ConversationEvent.event_type,
            func.count(ConversationEvent.id),
        )
        .where(
            ConversationEvent.actor_user_id.in_(allowed_user_ids),
            in_period(ConversationEvent.created_at, filters.period),
        )
        .group_by(ConversationEvent.actor_user_id, ConversationEvent.event_type)
    )).all()
    convo_events: dict = {}
    for user_id, event_type, count in event_rows:
        convo_events.setdefault(user_id, {})[event_type] = int(count)

    resolved_map = await _resolved_counts(session, allowed_user_ids, filters)

    out = []
    for user_id, full_name, email in users:
        acts = activity.get(user_id, {})
        events = convo_events.get(user_id, {})
        out.append({
            "user_id": str(user_id),
            "name": full_name or email,
            "email": email,
            "leads_created": int(leads_created.get(user_id, 0)),
            "leads_updated": acts.get("updated", 0) + acts.get("status", 0),
            "leads_archived": acts.get("archived", 0),
            "replies_sent": events.get("MESSAGE_SENT", 0),
            "conversations_assigned": events.get("ASSIGNED", 0),
            "notes_added": events.get("NOTE_ADDED", 0),
            "conversations_resolved": resolved_map.get(str(user_id), 0),
        })
    return out


async def _resolved_counts(
    session: AsyncSession, allowed_user_ids: list, filters: AnalyticsFilters,
) -> dict:
    """Resolutions = STATUS_CHANGED events whose new_value.status is terminal.
    JSON payload matching happens in Python over bounded period rows to stay
    portable across SQLite and PostgreSQL."""
    rows = (await session.execute(
        select(
            ConversationEvent.actor_user_id,
            ConversationEvent.new_value,
        )
        .where(
            ConversationEvent.actor_user_id.in_(allowed_user_ids),
            ConversationEvent.event_type == "STATUS_CHANGED",
            in_period(ConversationEvent.created_at, filters.period),
        )
    )).all()
    counts: dict = {}
    for user_id, new_value in rows:
        status = str((new_value or {}).get("status") or "").upper()
        if status in RESOLVED_STATUSES:
            key = str(user_id)
            counts[key] = counts.get(key, 0) + 1
    return counts
