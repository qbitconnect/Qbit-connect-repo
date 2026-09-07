"""Lead analytics (spec §6, §7) — real aggregates over the `leads` table only.

Metric definitions (spec §28 — mirrored in docs/metric-definitions.md):
- created        count(leads) with created_at in period
- new            created leads whose CURRENT status is NEW
- valid          leads with at least one actionable normalized contact key
                 (email_norm OR phone_norm OR website_norm)
- verified       leads whose CURRENT status is VERIFIED or later stage
- funnel stages  CURRENT status counts in the canonical stage order; statuses
                 are point-in-time facts — no transition history is invented
- duplicates     leads soft-merged away (merged_into_id IS NOT NULL)
- quality        avg(quality_score) over leads with a non-null score
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.math import safe_rate
from app.analytics.core.query_builder import base_leads_query
from app.analytics.core.time import Period, day_bucket
from app.models.lead import LeadTag, LeadTagAssignment
from app.models.scrape import Lead

#: canonical funnel order (spec §6); NOT_INTERESTED/LOST/ARCHIVED sit outside
FUNNEL_STAGES = ("NEW", "VERIFIED", "QUALIFIED", "CONTACTED", "REPLIED",
                 "INTERESTED", "CONVERTED")

STATUS_COLUMNS = {
    "new": "NEW",
    "verified": "VERIFIED",
    "qualified": "QUALIFIED",
    "contacted": "CONTACTED",
    "replied": "REPLIED",
    "interested": "INTERESTED",
    "converted": "CONVERTED",
    "lost": "LOST",
    "archived": "ARCHIVED",
}


async def _status_counts(session: AsyncSession, query) -> dict:
    rows = (await session.execute(
        query.with_only_columns(Lead.status, func.count(Lead.id))
        .group_by(Lead.status)
    )).all()
    return {status or "UNKNOWN": int(n) for status, n in rows}


async def lead_kpis(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    """KPI counts within the filtered set (current-period only; comparison is
    added by the service layer)."""
    base = base_leads_query(filters)
    total = int(await session.scalar(
        base.with_only_columns(func.count(Lead.id))
    ) or 0)

    by_status = await _status_counts(session, base_leads_query(filters))
    valid = int(await session.scalar(
        base_leads_query(filters)
        .with_only_columns(func.count(Lead.id))
        .where(
            Lead.email_norm.is_not(None)
            | Lead.phone_norm.is_not(None)
            | Lead.website_norm.is_not(None)
        )
    ) or 0)
    quality = await session.scalar(
        base_leads_query(filters)
        .with_only_columns(func.avg(Lead.quality_score))
        .where(Lead.quality_score.is_not(None))
    )
    duplicates = int(await session.scalar(
        base_leads_query(filters)
        .with_only_columns(func.count(Lead.id))
        .where(Lead.merged_into_id.is_not(None))
    ) or 0)

    def status_value(key: str) -> int:
        return by_status.get(STATUS_COLUMNS[key], 0)

    # "reached stage" counts: current status at or after the stage in the
    # canonical order (documented as point-in-time, never invented history)
    reached: dict[str, int] = {}
    for index, stage in enumerate(FUNNEL_STAGES):
        reached[stage] = sum(by_status.get(s, 0) for s in FUNNEL_STAGES[index:])

    return {
        "total": total,
        "new": status_value("new"),
        "valid": valid,
        "verified": reached["VERIFIED"],
        "qualified": reached["QUALIFIED"],
        "contacted": reached["CONTACTED"],
        "replied": reached["REPLIED"],
        "interested": reached["INTERESTED"],
        "converted": reached["CONVERTED"],
        "lost": status_value("lost"),
        "archived": status_value("archived"),
        "by_status": by_status,
        "duplicates_merged": duplicates,
        "avg_quality_score": round(float(quality), 1) if quality is not None else None,
        "valid_rate": safe_rate(valid, total),
    }


async def leads_timeseries(
    session: AsyncSession,
    filters: AnalyticsFilters,
    tz: ZoneInfo,
    dialect: str,
) -> list[dict]:
    """Leads created per day (created_at in the requested timezone)."""
    period = filters.period
    bucket = day_bucket(Lead.created_at, str(tz), period.start, period.end, dialect=dialect)
    query = base_leads_query(filters).with_only_columns(
        bucket.label("day"),
        func.count(Lead.id).label("created"),
        func.count(Lead.quality_score).label("scored"),
        func.avg(Lead.quality_score).label("avg_quality"),
    ).group_by(bucket).order_by(bucket)
    rows = (await session.execute(query)).all()
    return [
        {
            "day": str(row.day),
            "created": int(row.created),
            "avg_quality": round(float(row.avg_quality), 1) if row.avg_quality is not None else None,
        }
        for row in rows
    ]


async def distribution(
    session: AsyncSession,
    filters: AnalyticsFilters,
    dimension: str,
    limit: int = 12,
) -> list[dict]:
    """Count leads grouped by an allowlisted dimension column."""
    column_map = {
        "status": Lead.status,
        "source": Lead.source,
        "source_type": Lead.source_type,
        "category": Lead.category,
        "industry": Lead.industry,
        "city": Lead.city,
        "state": Lead.state,
        "country": Lead.country,
        "scraper": Lead.source_actor_id,
    }
    column = column_map.get(dimension)
    if column is None:
        raise ValueError(f"Unsupported lead dimension: {dimension}")
    query = base_leads_query(filters).with_only_columns(
        column.label("value"), func.count(Lead.id).label("count"),
    ).group_by(column).order_by(func.count(Lead.id).desc()).limit(limit)
    rows = (await session.execute(query)).all()
    return [
        {"value": row.value or "Unknown", "count": int(row.count)}
        for row in rows
    ]


async def tags_distribution(session: AsyncSession, filters: AnalyticsFilters,
                            limit: int = 12) -> list[dict]:
    """Leads per tag from the relational source of truth."""
    subq = base_leads_query(filters).with_only_columns(Lead.id).scalar_subquery()
    rows = (await session.execute(
        select(LeadTag.name, func.count(LeadTagAssignment.lead_id).label("count"))
        .join(LeadTagAssignment, LeadTagAssignment.tag_id == LeadTag.id)
        .where(LeadTagAssignment.lead_id.in_(subq))
        .group_by(LeadTag.name)
        .order_by(func.count(LeadTagAssignment.lead_id).desc())
        .limit(limit)
    )).all()
    return [{"value": name, "count": int(count)} for name, count in rows]


async def funnel(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    """Conversion funnel from CURRENT statuses (spec §6: no invented
    transitions). Rates are stage/total of leads reaching the previous stage
    where meaningful."""
    base = base_leads_query(filters)
    total = int(await session.scalar(base.with_only_columns(func.count(Lead.id))) or 0)
    by_status = await _status_counts(session, base)

    stages: list[dict] = []
    previous = None
    for index, stage in enumerate(FUNNEL_STAGES):
        reached = sum(by_status.get(s, 0) for s in FUNNEL_STAGES[index:])
        stages.append({
            "stage": stage,
            "count": reached,
            "share_of_total": safe_rate(reached, total),
            "step_conversion": safe_rate(reached, previous) if previous else None,
        })
        previous = reached
    return {"total": total, "stages": stages}


async def source_performance(session: AsyncSession, filters: AnalyticsFilters) -> list[dict]:
    """Per-source performance (spec §7) from lead provenance (Phase 4 source
    columns) + scrape-job counters where the source maps to an actor.

    Metrics a source cannot support (e.g. rejected records for imports with no
    scrape job) are reported as None → the UI shows "not available" rather
    than an estimate.
    """
    from sqlalchemy import case

    base = base_leads_query(filters).with_only_columns(
        Lead.source.label("source"),
        Lead.source_type.label("source_type"),
        func.count(Lead.id).label("leads"),
        func.count(Lead.email_norm).label("with_email"),
        func.count(Lead.phone_norm).label("with_phone"),
        func.avg(Lead.quality_score).label("avg_quality"),
        func.sum(case((Lead.merged_into_id.is_not(None), 1), else_=0)).label("merged"),
    )
    for status in ("QUALIFIED", "INTERESTED", "CONVERTED"):
        base = base.add_columns(
            func.sum(case((Lead.status == status, 1), else_=0))
            .label(f"status_{status.lower()}"),
        )
    base = base.group_by(Lead.source, Lead.source_type)
    rows = (await session.execute(base)).all()

    from app.models.scrape import ScrapeJob

    job_rows = (await session.execute(
        select(
            ScrapeJob.actor_id.label("actor"),
            func.count(ScrapeJob.id).label("jobs"),
            func.coalesce(func.sum(ScrapeJob.records_found), 0).label("found"),
            func.coalesce(func.sum(ScrapeJob.records_saved), 0).label("saved"),
            func.coalesce(func.sum(ScrapeJob.records_duplicate), 0).label("dupes"),
            func.coalesce(func.sum(ScrapeJob.records_failed), 0).label("rejected"),
        ).group_by(ScrapeJob.actor_id)
    )).all()
    jobs_by_actor = {row.actor: row for row in job_rows}

    out: list[dict] = []
    for row in rows:
        total = int(row.leads)
        job = jobs_by_actor.get(row.source) if row.source else None
        actionable = int(row.with_email or 0) + int(row.with_phone or 0)
        out.append({
            "source": row.source or "Unknown",
            "source_type": row.source_type or "unknown",
            "leads": total,
            "actionable": actionable,
            "valid_rate": safe_rate(min(actionable, total), total),
            "avg_quality": round(float(row.avg_quality), 1) if row.avg_quality is not None else None,
            "duplicate_rate": safe_rate(int(row.merged or 0), total),
            "qualified": int(row.status_qualified or 0),
            "interested": int(row.status_interested or 0),
            "converted": int(row.status_converted or 0),
            "qualification_rate": safe_rate(int(row.status_qualified or 0), total),
            "conversion_rate": safe_rate(int(row.status_converted or 0), total),
            "scrape": None if job is None and (row.source_type or "") != "scraper" else {
                "jobs": int(job.jobs) if job else 0,
                "records_found": int(job.found) if job else 0,
                "records_accepted": int(job.saved) if job else 0,
                "duplicates": int(job.dupes) if job else 0,
                "rejected": int(job.rejected) if job else 0,
                "duplicate_rate": safe_rate(int(job.dupes), int(job.found)) if job else None,
                "rejection_rate": safe_rate(int(job.rejected), int(job.found)) if job else None,
            },
        })
    return out


def _int_type():
    from sqlalchemy import Integer

    return Integer


async def status_transitions_over_time(
    session: AsyncSession, filters: AnalyticsFilters, tz: ZoneInfo, dialect: str,
) -> list[dict]:
    """Per-day created counts per status (chart: lead status over time)."""
    period: Period = filters.period
    bucket = day_bucket(Lead.created_at, str(tz), period.start, period.end, dialect=dialect)
    rows = (await session.execute(
        base_leads_query(filters).with_only_columns(
            bucket.label("day"), Lead.status.label("status"),
            func.count(Lead.id).label("count"),
        ).group_by(bucket, Lead.status).order_by(bucket)
    )).all()
    series: dict[str, dict] = {}
    for row in rows:
        day = str(row.day)
        series.setdefault(day, {"day": day, "by_status": {}})
        status = row.status or "UNKNOWN"
        series[day]["by_status"][status] = int(row.count)
    return list(series.values())


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
