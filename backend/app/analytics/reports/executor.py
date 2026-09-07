"""Report execution engine — computes a stored report configuration through
the SAME domain modules the dashboards use (spec §28: never a different
metric definition), snapshots the result and, for large tabular output,
writes an export file through StorageService.

Background contract (spec §19): a ReportRun row is claimed with a lease,
executed, and completed with a ReportSnapshot. Failures are recorded on the
run — they never crash the worker loop.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.exceptions import AnalyticsValidationError
from app.analytics.core.filters import filters_from_payload
from app.analytics.core.time import resolve_period
from app.analytics.domains import (
    automation,
    email as email_domain,
    inbox,
    leads as leads_domain,
    marketing as marketing_domain,
    scraping as scraping_domain,
    whatsapp as whatsapp_domain,
)
from app.models.analytics import (
    ReportRun,
    ReportRunStatus,
    ReportSnapshot,
)

#: per-domain "rows by dimension" resolvers → (rows, note)
_MAX_SNAPSHOT_ROWS_DEFAULT = 10_000


def _flatten(kpis: dict) -> dict:
    out = dict(kpis)
    rates = kpis.get("rates")
    if isinstance(rates, dict):
        out.update(rates)
    return out


def _resolve_filters(config: dict, tz_name: str):
    period_key = str(config.get("period", "30d"))
    date_from = config.get("date_from")
    date_to = config.get("date_to")
    period = resolve_period(period=period_key, date_from=date_from, date_to=date_to,
                            tz_name=tz_name)
    filters = filters_from_payload(config.get("filters") or {}, period)
    return period, filters


class ReportExecutor:
    def __init__(self, dialect: str = "sqlite") -> None:
        self.dialect = dialect

    async def compute(self, session: AsyncSession, run: ReportRun) -> dict:
        """Returns {"rows": [...], "meta": {...}} from the frozen run config."""
        config = dict(run.config_snapshot or {})
        domain = str(config.get("domain", "")).upper()
        metrics = list(config.get("metrics") or [])
        dimensions = list(config.get("dimensions") or [])
        visualization = str(config.get("visualization", "table"))
        period, filters = _resolve_filters(config, run.timezone or "UTC")
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(run.timezone or "UTC")

        rows: list[dict] = []
        notes: list[str] = []

        if visualization == "funnel" and domain == "LEADS":
            funnel = await leads_domain.funnel(session, filters)
            rows = [
                {
                    "stage": stage["stage"],
                    "count": stage["count"],
                    "share_of_total": stage["share_of_total"],
                    "step_conversion": stage["step_conversion"],
                }
                for stage in funnel["stages"]
            ]
            meta = {"row_kind": "funnel_stages", "total": funnel["total"]}
        elif visualization in ("line", "area", "bar") and config.get("grouping", "day") == "day" \
                and not dimensions:
            series = await self._timeseries(session, domain, filters, tz)
            rows = [
                {**{m: _series_value(row, m) for m in metrics}, "day": row.get("day")}
                for row in series
            ]
            meta = {"row_kind": "timeseries", "granularity": "day"}
        elif dimensions:
            rows, notes = await self._dimension_rows(session, domain, dimensions, metrics, filters)
            meta = {"row_kind": "dimension", "dimension": dimensions[0]}
        else:
            kpis = await self._kpis(session, domain, filters)
            flat = _flatten(kpis)
            rows = [{m: flat.get(m) for m in metrics}]
            meta = {"row_kind": "kpi"}

        rows = _sort_rows(rows, config, meta)
        limit = int(config.get("limit", 100) or 100)
        truncated = len(rows) > limit
        rows = rows[:limit]
        meta["notes"] = notes
        meta["truncated_to_limit"] = truncated
        meta["period"] = period.to_dict()
        return {"rows": rows, "meta": meta}

    async def _kpis(self, session, domain: str, filters) -> dict:
        if domain == "LEADS":
            return await leads_domain.lead_kpis(session, filters)
        if domain == "SCRAPING":
            return await scraping_domain.scraping_kpis(session, filters)
        if domain == "MARKETING":
            return await marketing_domain.marketing_kpis(session, filters)
        if domain == "WHATSAPP":
            return await whatsapp_domain.whatsapp_kpis(session, filters)
        if domain == "EMAIL":
            return await email_domain.email_kpis(session, filters)
        if domain == "INBOX":
            return await inbox.inbox_kpis(session, filters)
        if domain == "AUTOMATION":
            return await automation.automation_kpis(session, filters)
        raise AnalyticsValidationError(f"Domain {domain} does not support KPI rows")

    async def _timeseries(self, session, domain: str, filters, tz) -> list[dict]:
        if domain == "LEADS":
            return await leads_domain.leads_timeseries(session, filters, tz, self.dialect)
        if domain == "SCRAPING":
            return await scraping_domain.jobs_timeseries(session, filters, tz, self.dialect)
        if domain == "MARKETING":
            return await marketing_domain.campaigns_timeseries(session, filters, tz, self.dialect)
        if domain == "INBOX":
            return await inbox.conversations_timeseries(session, filters, tz, self.dialect)
        if domain == "AUTOMATION":
            return await automation.executions_timeseries(session, filters, tz, self.dialect)
        if domain == "WHATSAPP":
            return await whatsapp_domain.response_activity(session, filters, tz, self.dialect)
        raise AnalyticsValidationError(
            f"Domain {domain} does not support day-grouped series; use a dimension or KPI report"
        )

    async def _dimension_rows(self, session, domain: str, dimensions: list[str],
                              metrics: list[str], filters) -> tuple[list[dict], list[str]]:
        dimension = dimensions[0]
        notes: list[str] = []
        rows: list[dict] = []
        if domain == "LEADS":
            if dimension == "source":
                return await leads_domain.source_performance(session, filters), notes
            if dimension == "tag":
                raw = await leads_domain.tags_distribution(session, filters)
            else:
                raw = await leads_domain.distribution(session, filters, dimension)
            rows = [{"value": r["value"], **{m: _dist_value(r, m) for m in metrics}}
                    for r in raw]
        elif domain == "SCRAPING":
            rows = await scraping_domain.scraper_performance(session, filters)
            rows = [{"value": f"{r['actor_id']}@{r['actor_version']}", **r} for r in rows]
        elif domain == "MARKETING":
            if dimension == "channel":
                rows = await marketing_domain.channel_comparison(session, filters)
            else:
                rows = await marketing_domain.campaign_list_performance(session, filters)
                rows = [{"value": r["name"], **r} for r in rows]
        elif domain == "INBOX":
            raw = await inbox.conversation_distribution(session, filters, dimension)
            rows = [{"value": r["value"], "conversations_total": r["count"]}
                    for r in raw]
        elif domain == "AUTOMATION":
            rows = await automation.most_executed(session, filters)
            rows = [{"value": r["name"], **r} for r in rows]
        elif domain == "WHATSAPP":
            rows = await whatsapp_domain.per_account(session, filters)
            rows = [{"value": r["identifier"], **r} for r in rows]
        elif domain == "EMAIL":
            rows = await email_domain.per_sender_account(session, filters)
            rows = [{"value": r["identifier"], **r} for r in rows]
        else:
            raise AnalyticsValidationError(f"Domain {domain} does not support dimensions")

        if dimension != "source" and domain == "LEADS" and any(
            m not in ("total",) for m in metrics
        ):
            notes.append(
                "Only the lead count is available per this dimension; other "
                "requested metrics are reported as null (never estimated)."
            )
        normalized = []
        for row in rows:
            flat = dict(row)
            rates = flat.pop("rates", None)
            if isinstance(rates, dict):
                flat.update(rates)
            value = flat.pop("value", None)
            normalized.append({"value": value, **flat})
        return normalized, notes


def _dist_value(row: dict, metric: str) -> int | None:
    if metric == "total":
        return row.get("count")
    return None


def _series_value(row: dict, metric: str) -> int | None:
    """Timeseries rows carry domain-specific keys; map metric names honestly."""
    aliases = {
        "total": "created",
        "new": "created",
        "jobs_total": "jobs",
        "records_accepted": "records_saved",
        "messages_sent": "sent",
        "messages_delivered": "delivered",
        "messages_failed": "failed",
        "replies": "replied",
        "conversations_total": "created",
        "executions": "executions",
        "executions_completed": "completed",
        "executions_failed": "failed",
        "campaigns_total": "campaigns_created",
        "emails_sent": "sent",
    }
    key = aliases.get(metric, metric)
    value = row.get(key)
    return value if value is not None else None


def _sort_rows(rows: list[dict], config: dict, meta: dict) -> list[dict]:
    direction = str(config.get("sort_dir", "desc"))
    sort_by = config.get("sort_by")
    if not rows:
        return rows
    if not sort_by or sort_by not in rows[0]:
        # fall back to the first numeric column (count) when available
        sample = rows[0]
        numeric = [k for k, v in sample.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not numeric:
            return rows
        sort_by = "count" if "count" in numeric else numeric[0]

    def sort_key(row: dict):
        value = row.get(sort_by)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (0, value)
        if value is None:
            return (2, 0)
        return (1, str(value))

    return sorted(rows, key=sort_key, reverse=(direction == "desc"))


class ReportWorker:
    """Claims QUEUED runs with a lease and executes them (spec §19)."""

    def __init__(self, *, owner: str, storage_files, max_snapshot_rows: int =
                 _MAX_SNAPSHOT_ROWS_DEFAULT) -> None:
        self.owner = owner
        self.files = storage_files
        self.max_snapshot_rows = max_snapshot_rows

    async def claim_run(self, session: AsyncSession) -> ReportRun | None:
        from datetime import timedelta

        now = datetime.now(dt_timezone.utc)
        stale = now - timedelta(minutes=10)
        run = (await session.execute(
            select_run()
            .where(
                ReportRun.status == ReportRunStatus.QUEUED,
            )
            .limit(1)
        )).scalar_one_or_none()
        claimed = (await session.execute(
            select_run().where(
                ReportRun.status == ReportRunStatus.RUNNING,
                ReportRun.leased_at.is_not(None),
                ReportRun.leased_at < stale,
            ).limit(1)
        )).scalar_one_or_none()
        target = run or claimed
        if target is None:
            return None
        target.status = ReportRunStatus.RUNNING
        target.leased_at = now
        target.lease_owner = self.owner
        target.started_at = now
        await session.commit()
        return target

    async def process_cycle(self, session: AsyncSession) -> int:
        run = await self.claim_run(session)
        if run is None:
            return 0
        try:
            executor = ReportExecutor(dialect=session.bind.dialect.name
                                      if session.bind is not None else "sqlite")
            result = await executor.compute(session, run)
            rows = result["rows"]
            snapshot = ReportSnapshot(
                report_id=run.report_id,
                run_id=run.id,
                row_count=len(rows),
                data={"rows": rows, "meta": result["meta"]},
                generated_at=datetime.now(dt_timezone.utc),
            )
            export_file_id = None
            if len(rows) > self.max_snapshot_rows:
                from app.services.export import ExportService

                export = await ExportService(self.files).export(
                    session, format_name=run.format or "json", rows=rows,
                    base_name=f"report-run-{str(run.id)[:8]}",
                    created_by=run.requested_by,
                    metadata={"report_id": str(run.report_id), "run_id": str(run.id)},
                )
                export_file_id = export.id
                snapshot.data = {"rows": [], "meta": {**result["meta"],
                                                      "overflow": "export_file"}}
                snapshot.row_count = len(rows)
                snapshot.export_file_id = export_file_id
            session.add(snapshot)
            run.status = ReportRunStatus.COMPLETED
            run.completed_at = datetime.now(dt_timezone.utc)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 — the run fails honestly
            run.status = ReportRunStatus.FAILED
            run.error = str(exc)[:2000]
            run.completed_at = datetime.now(dt_timezone.utc)
            await session.commit()
        return 1


def select_run():
    from sqlalchemy import select

    return select(ReportRun).order_by(ReportRun.created_at.asc())
