"""Scraper analytics (spec §8) — aggregates over scrape_jobs / scrape_job_events.

Metric definitions:
- success rate   completed / (completed + failed) — cancelled jobs excluded
- records        counters are the job's own persisted fields (records_found,
                 records_saved accepted, records_duplicate, records_failed)
- avg duration   avg(completed_at - started_at) over jobs with both timestamps
- versioning     per-scraper rows are grouped by (actor_id, actor_version) so
                 version performance is never blended misleadingly
"""

from __future__ import annotations

from sqlalchemy import case, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.math import safe_rate
from app.analytics.core.query_builder import base_scrape_jobs_query
from app.analytics.core.time import Period, day_bucket
from app.models.scrape import JobStatus, ScrapeJob

ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)


async def scraping_kpis(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    base = base_scrape_jobs_query(filters)
    rows = (await session.execute(
        base.with_only_columns(ScrapeJob.status, func.count(ScrapeJob.id))
        .group_by(ScrapeJob.status)
    )).all()
    by_status = {status: int(n) for status, n in rows}

    totals = (await session.execute(
        base.with_only_columns(
            func.coalesce(func.sum(ScrapeJob.records_found), 0).label("found"),
            func.coalesce(func.sum(ScrapeJob.records_saved), 0).label("saved"),
            func.coalesce(func.sum(ScrapeJob.records_updated), 0).label("updated"),
            func.coalesce(func.sum(ScrapeJob.records_duplicate), 0).label("duplicates"),
            func.coalesce(func.sum(ScrapeJob.records_failed), 0).label("rejected"),
            func.avg(
                func.extract("epoch", ScrapeJob.completed_at)
                - func.extract("epoch", ScrapeJob.started_at)
            ).label("avg_runtime"),
        )
    )).first()

    completed = by_status.get(JobStatus.COMPLETED, 0)
    failed = by_status.get(JobStatus.FAILED, 0)
    return {
        "jobs_total": sum(by_status.values()),
        "running": by_status.get(JobStatus.RUNNING, 0) + by_status.get(JobStatus.PAUSED, 0),
        "queued": by_status.get(JobStatus.QUEUED, 0),
        "completed": completed,
        "failed": failed,
        "cancelled": by_status.get(JobStatus.CANCELLED, 0),
        "by_status": by_status,
        "records_found": int(totals.found or 0),
        "records_accepted": int(totals.saved or 0),
        "records_updated": int(totals.updated or 0),
        "records_duplicate": int(totals.duplicates or 0),
        "records_rejected": int(totals.rejected or 0),
        "avg_runtime_seconds": round(float(totals.avg_runtime), 1) if totals.avg_runtime is not None else None,
        "success_rate": safe_rate(completed, completed + failed),
        "acceptance_rate": safe_rate(int(totals.saved or 0), int(totals.found or 0)),
    }


async def jobs_timeseries(
    session: AsyncSession, filters: AnalyticsFilters, tz, dialect: str,
) -> list[dict]:
    """Jobs per day with success/failure split + records saved."""
    period: Period = filters.period
    bucket = day_bucket(ScrapeJob.created_at, str(tz), period.start, period.end, dialect=dialect)
    rows = (await session.execute(
        base_scrape_jobs_query(filters).with_only_columns(
            bucket.label("day"),
            func.count(ScrapeJob.id).label("jobs"),
            func.sum(case((ScrapeJob.status == JobStatus.COMPLETED, 1), else_=0)).label("completed"),
            func.sum(case((ScrapeJob.status == JobStatus.FAILED, 1), else_=0)).label("failed"),
            func.coalesce(func.sum(ScrapeJob.records_saved), 0).label("saved"),
        ).group_by(bucket).order_by(bucket)
    )).all()
    return [
        {
            "day": str(row.day),
            "jobs": int(row.jobs),
            "completed": int(row.completed or 0),
            "failed": int(row.failed or 0),
            "records_saved": int(row.saved or 0),
        }
        for row in rows
    ]


async def scraper_performance(session: AsyncSession, filters: AnalyticsFilters) -> list[dict]:
    """Per (actor, version) performance rows (spec §8 'Per scraper')."""
    rows = (await session.execute(
        base_scrape_jobs_query(filters).with_only_columns(
            ScrapeJob.actor_id.label("actor"),
            ScrapeJob.actor_version.label("version"),
            func.count(ScrapeJob.id).label("jobs"),
            func.sum(case((ScrapeJob.status == JobStatus.COMPLETED, 1), else_=0)).label("completed"),
            func.sum(case((ScrapeJob.status == JobStatus.FAILED, 1), else_=0)).label("failed"),
            func.coalesce(func.sum(ScrapeJob.records_found), 0).label("found"),
            func.coalesce(func.sum(ScrapeJob.records_saved), 0).label("accepted"),
            func.coalesce(func.sum(ScrapeJob.records_duplicate), 0).label("duplicates"),
            func.avg(
                func.extract("epoch", ScrapeJob.completed_at)
                - func.extract("epoch", ScrapeJob.started_at)
            ).label("avg_runtime"),
            func.max(ScrapeJob.created_at).label("last_run"),
        ).group_by(ScrapeJob.actor_id, ScrapeJob.actor_version)
        .order_by(func.count(ScrapeJob.id).desc())
    )).all()
    out = []
    for row in rows:
        jobs = int(row.jobs)
        completed = int(row.completed or 0)
        failed = int(row.failed or 0)
        out.append({
            "actor_id": row.actor,
            "actor_version": row.version,
            "jobs": jobs,
            "completed": completed,
            "failed": failed,
            "records_generated": int(row.found or 0),
            "records_accepted": int(row.accepted or 0),
            "duplicates": int(row.duplicates or 0),
            "avg_runtime_seconds": round(float(row.avg_runtime), 1) if row.avg_runtime is not None else None,
            "success_rate": safe_rate(completed, completed + failed),
            "last_run": row.last_run.isoformat() if row.last_run else None,
        })
    return out


async def error_distribution(session: AsyncSession, filters: AnalyticsFilters,
                             limit: int = 8) -> list[dict]:
    """Failed jobs grouped by error_code (message fallback truncated)."""
    rows = (await session.execute(
        base_scrape_jobs_query(filters)
        .with_only_columns(
            func.coalesce(ScrapeJob.error_code, "UNKNOWN").label("code"),
            func.count(ScrapeJob.id).label("count"),
        )
        .where(ScrapeJob.status == JobStatus.FAILED)
        .group_by(func.coalesce(ScrapeJob.error_code, "UNKNOWN"))
        .order_by(func.count(ScrapeJob.id).desc())
        .limit(limit)
    )).all()
    return [{"value": row.code, "count": int(row.count)} for row in rows]


async def runtime_distribution(session: AsyncSession, filters: AnalyticsFilters) -> list[dict]:
    """Average runtime buckets (per scraper, completed jobs only)."""
    rows = (await session.execute(
        base_scrape_jobs_query(filters).with_only_columns(
            ScrapeJob.actor_id.label("actor"),
            func.avg(
                func.extract("epoch", ScrapeJob.completed_at)
                - func.extract("epoch", ScrapeJob.started_at)
            ).label("avg_runtime"),
        )
        .where(ScrapeJob.status == JobStatus.COMPLETED)
        .where(ScrapeJob.started_at.is_not(None), ScrapeJob.completed_at.is_not(None))
        .group_by(ScrapeJob.actor_id)
        .order_by(func.avg(
            func.extract("epoch", ScrapeJob.completed_at)
            - func.extract("epoch", ScrapeJob.started_at)
        ).desc())
    )).all()
    return [
        {
            "value": row.actor,
            "count": round(float(row.avg_runtime), 1) if row.avg_runtime is not None else 0.0,
        }
        for row in rows
    ]
