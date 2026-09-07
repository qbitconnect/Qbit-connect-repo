"""Shared, portable query assembly for analytics (spec §3, §5, §32).

One applier per operational table family — every domain module composes the
SAME building blocks so filters behave identically across KPI cards, charts
and tables. No string SQL is ever produced; values are bound parameters.
"""

from __future__ import annotations

from sqlalchemy import Select, select

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.time import Period
from app.models.automation import WorkflowExecution
from app.models.lead import LeadTagAssignment
from app.models.marketing import Campaign, CampaignRecipient
from app.models.messaging import Conversation, Message
from app.models.scrape import Lead, ScrapeJob


def in_period(column, period: Period | None):
    """Closed-open [start, end) range on a UTC-aware timestamp column."""
    if period is None:
        return True
    return (column >= period.start) & (column < period.end)


def _any(column, values: list[str]):
    if not values:
        return True
    return column.in_(values)


def base_leads_query(filters: AnalyticsFilters) -> Select:
    query = select(Lead)
    if filters.period is not None:
        query = query.where(in_period(Lead.created_at, filters.period))
    query = query.where(
        _any(Lead.source, filters.source),
        _any(Lead.source_type, filters.source_type),
        _any(Lead.status, filters.lead_status),
        _any(Lead.category, filters.lead_category),
        _any(Lead.industry, filters.industry),
        _any(Lead.city, filters.city),
        _any(Lead.state, filters.state),
        _any(Lead.country, filters.country),
        _any(Lead.source_actor_id, filters.scraper),
        _any(Lead.source_actor_version, filters.scraper_version),
    )
    if filters.tags:
        # relational tag assignment is the source of truth (models/lead.py);
        # the JSON column is only a display mirror and is never filtered on
        subq = lead_ids_for_tags_subquery(filters)
        if subq is not None:
            query = query.where(Lead.id.in_(subq))
    return query


def _text_type():
    from sqlalchemy import Text

    return Text


def base_scrape_jobs_query(filters: AnalyticsFilters) -> Select:
    query = select(ScrapeJob)
    if filters.period is not None:
        query = query.where(in_period(ScrapeJob.created_at, filters.period))
    return query.where(
        _any(ScrapeJob.actor_id, filters.scraper),
        _any(ScrapeJob.actor_version, filters.scraper_version),
    )


def base_campaigns_query(filters: AnalyticsFilters) -> Select:
    query = select(Campaign)
    if filters.period is not None:
        query = query.where(in_period(Campaign.created_at, filters.period))
    return query.where(
        _any(Campaign.channel, filters.channel),
        _any(Campaign.status, filters.campaign_status),
        _any(Campaign.sending_account_id, filters.sending_account_id),
    )


def base_recipients_query(filters: AnalyticsFilters) -> Select:
    """CampaignRecipient rows joined to Campaign so channel/account filters apply."""
    query = (
        select(CampaignRecipient)
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
    )
    if filters.period is not None:
        query = query.where(in_period(CampaignRecipient.created_at, filters.period))
    return query.where(
        _any(Campaign.channel, filters.channel),
        _any(Campaign.sending_account_id, filters.sending_account_id),
        _any(Campaign.id, filters.campaign_id),
    )


def base_conversations_query(filters: AnalyticsFilters) -> Select:
    query = select(Conversation)
    if filters.period is not None:
        query = query.where(in_period(Conversation.created_at, filters.period))
    return query.where(
        _any(Conversation.channel, filters.channel),
        _any(Conversation.status, filters.conversation_status),
        _any(Conversation.priority, filters.conversation_priority),
        _any(Conversation.assigned_user_id, filters.assigned_user_id),
        _any(Conversation.sending_account_id, filters.sending_account_id),
    )


def base_messages_query(filters: AnalyticsFilters) -> Select:
    query = select(Message).join(
        Conversation, Conversation.id == Message.conversation_id
    )
    if filters.period is not None:
        query = query.where(in_period(Message.created_at, filters.period))
    return query.where(
        _any(Conversation.channel, filters.channel),
        _any(Conversation.sending_account_id, filters.sending_account_id),
    )


def base_workflow_executions_query(filters: AnalyticsFilters) -> Select:
    # no join to workflows: executions are counted as they exist (orphans are
    # surfaced by diagnostics, never silently hidden — spec §29)
    query = select(WorkflowExecution)
    if filters.period is not None:
        query = query.where(in_period(WorkflowExecution.created_at, filters.period))
    return query.where(
        _any(WorkflowExecution.workflow_id, filters.workflow_id),
        _any(WorkflowExecution.status, filters.execution_status),
    )


def lead_ids_for_tags_subquery(filters: AnalyticsFilters):
    """Relational tag filter (source of truth) as a subquery of lead ids."""
    if not filters.tags:
        return None
    from sqlalchemy import select as _select

    from app.models.lead import LeadTag, LeadTagAssignment

    return (
        _select(LeadTagAssignment.lead_id)
        .join(LeadTag, LeadTag.id == LeadTagAssignment.tag_id)
        .where(LeadTag.name.in_(filters.tags))
    )
