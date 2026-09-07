"""Aggregation layer (spec §20–§21, §29).

Daily aggregate tables are DERIVED, idempotent caches over operational tables:
- operational tables stay the source of truth; dashboards compute from live
  indexed queries so a stale aggregate can never change a displayed number
- incremental refresh recomputes the last aggregated day (overlap) through
  yesterday and upserts (day, dimension) rows — safe after interruption
- manual rebuild recomputes an explicit date range; it NEVER touches
  operational data (spec §39)
- every run writes an AnalyticsAggregationRun bookkeeping row
- diagnostics compare aggregates against live counts and flag anomalies
  (negative counts, impossible rates, orphans, duplicate events) WITHOUT
  modifying production data (spec §29)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.time import day_bucket
from app.models.analytics import (
    AnalyticsAggregationRun,
    AnalyticsDailyAutomation,
    AnalyticsDailyCampaign,
    AnalyticsDailyConversation,
    AnalyticsDailyLead,
    AnalyticsDailyMessage,
    AnalyticsDailyScraping,
)
from app.models.automation import Workflow, WorkflowExecution, WorkflowExecutionStep
from app.models.marketing import Campaign, CampaignEvent, CampaignRecipient
from app.models.messaging import Conversation, ConversationEvent, Message
from app.models.scrape import Lead, ScrapeJob

#: today is excluded — an in-progress day would make aggregates disagree with
#: the "current period" live numbers; the live layer owns today.
UTC = dt_timezone.utc

TABLE_BY_DOMAIN = {
    "leads": AnalyticsDailyLead,
    "campaigns": AnalyticsDailyCampaign,
    "messages": AnalyticsDailyMessage,
    "conversations": AnalyticsDailyConversation,
    "scraping": AnalyticsDailyScraping,
    "automation": AnalyticsDailyAutomation,
}


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


async def _covered_range(session: AsyncSession, table) -> tuple[date, date]:
    """(earliest operational day, latest complete day) for one domain."""
    today = datetime.now(UTC).date()
    source_bounds = {
        AnalyticsDailyLead: func.min(Lead.created_at),
        AnalyticsDailyCampaign: func.min(CampaignEvent.created_at),
        AnalyticsDailyMessage: func.min(Message.created_at),
        AnalyticsDailyConversation: func.min(Conversation.created_at),
        AnalyticsDailyScraping: func.min(ScrapeJob.created_at),
        AnalyticsDailyAutomation: func.min(WorkflowExecution.created_at),
    }
    earliest = await session.scalar(source_bounds[table])
    first_day = earliest.astimezone(UTC).date() if earliest else today - timedelta(days=1)
    last_day = today - timedelta(days=1)
    if last_day < first_day:
        last_day = first_day
    return first_day, last_day


async def _last_aggregated_day(session: AsyncSession, table) -> date | None:
    return await session.scalar(select(func.max(table.day)))


async def _upsert(session: AsyncSession, table, keys: dict, metrics: dict) -> int:
    """Idempotent get-or-update on the (day, dims) unique key (spec §21).
    Day labels arrive as strings from both dialects' date expressions and are
    normalized to date objects for the Date column."""
    normalized = dict(keys)
    day_value = normalized.get("day")
    if isinstance(day_value, str):
        normalized["day"] = date.fromisoformat(day_value)
    row = (await session.execute(
        select(table).where(
            *[getattr(table, col) == value for col, value in normalized.items()]
        )
    )).scalar_one_or_none()
    if row is None:
        row = table(**normalized, metrics=metrics)
        session.add(row)
    else:
        row.metrics = metrics
    return 1


class AggregationService:
    """Incremental refresh + range rebuild + diagnostics (read-only)."""

    def __init__(self, dialect: str = "sqlite") -> None:
        self.dialect = dialect

    # ------------------------------------------------------------ per-domain
    async def _aggregate_leads(self, session, day: date) -> int:
        start, end = _day_bounds(day)
        bucket = day_bucket(Lead.created_at, "UTC", start, end, dialect=self.dialect)
        rows = (await session.execute(
            select(
                bucket.label("day"),
                func.coalesce(Lead.source, "").label("source"),
                func.count(Lead.id).label("created"),
                func.sum(case_status(Lead.status, "NEW")).label("new"),
                func.sum(case_status(Lead.status, "CONVERTED")).label("converted"),
                func.sum(case_status(Lead.status, "QUALIFIED")).label("qualified"),
                func.sum(case_status(Lead.status, "CONTACTED")).label("contacted"),
                func.sum(case_status(Lead.status, "REPLIED")).label("replied"),
                func.sum(case_status(Lead.status, "INTERESTED")).label("interested"),
                func.sum(case_status(Lead.status, "LOST")).label("lost"),
                func.sum(case_status(Lead.status, "ARCHIVED")).label("archived"),
                func.avg(Lead.quality_score).label("avg_quality"),
                func.count(Lead.quality_score).label("scored"),
                func.sum(_merged_case()).label("merged"),
            ).where(Lead.created_at >= start, Lead.created_at < end)
            .group_by(bucket, func.coalesce(Lead.source, ""))
        )).all()
        upserted = 0
        for row in rows:
            upserted += await _upsert(
                session, AnalyticsDailyLead,
                {"day": row.day, "source": row.source},
                {
                    "created": int(row.created),
                    "by_status": {s: int(getattr(row, s) or 0) for s in
                                  ("new", "converted", "qualified", "contacted",
                                   "replied", "interested", "lost", "archived")},
                    "quality_sum": round(float(row.avg_quality) * int(row.scored), 2)
                    if row.avg_quality is not None else 0.0,
                    "quality_n": int(row.scored or 0),
                    "merged": int(row.merged or 0),
                },
            )
        return upserted

    async def _aggregate_campaigns(self, session, day: date) -> int:
        start, end = _day_bounds(day)
        bucket = day_bucket(CampaignEvent.created_at, "UTC", start, end, dialect=self.dialect)
        rows = (await session.execute(
            select(
                bucket.label("day"),
                func.coalesce(Campaign.channel, "").label("channel"),
                CampaignEvent.event_type.label("event_type"),
                func.count(CampaignEvent.id).label("count"),
            )
            .join(Campaign, Campaign.id == CampaignEvent.campaign_id)
            .where(CampaignEvent.created_at >= start, CampaignEvent.created_at < end)
            .group_by(bucket, func.coalesce(Campaign.channel, ""), CampaignEvent.event_type)
        )).all()
        per_key: dict[tuple, dict] = {}
        for row in rows:
            key = (str(row.day), row.channel)
            per_key.setdefault(key, {})[row.event_type] = int(row.count)
        upserted = 0
        for (day_label, channel), events in per_key.items():
            upserted += await _upsert(
                session, AnalyticsDailyCampaign,
                {"day": day_label, "channel": channel},
                {"events": events,
                 "sent": events.get("MESSAGE_SENT", 0),
                 "delivered": events.get("MESSAGE_DELIVERED", 0),
                 "failed": events.get("MESSAGE_FAILED", 0),
                 "replied": events.get("MESSAGE_REPLIED", 0)},
            )
        return upserted

    async def _aggregate_messages(self, session, day: date) -> int:
        start, end = _day_bounds(day)
        bucket = day_bucket(Message.created_at, "UTC", start, end, dialect=self.dialect)
        rows = (await session.execute(
            select(
                bucket.label("day"),
                func.coalesce(Conversation.channel, "").label("channel"),
                func.coalesce(Message.direction, "").label("direction"),
                func.count(Message.id).label("messages"),
                func.count(Message.delivered_at).label("delivered"),
                func.count(Message.read_at).label("read"),
                func.count(Message.failed_at).label("failed"),
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.created_at >= start, Message.created_at < end)
            .group_by(bucket, func.coalesce(Conversation.channel, ""),
                      func.coalesce(Message.direction, ""))
        )).all()
        upserted = 0
        for row in rows:
            upserted += await _upsert(
                session, AnalyticsDailyMessage,
                {"day": str(row.day), "channel": row.channel, "direction": row.direction},
                {
                    "messages": int(row.messages),
                    "delivered": int(row.delivered or 0),
                    "read": int(row.read or 0),
                    "failed": int(row.failed or 0),
                },
            )
        return upserted

    async def _aggregate_conversations(self, session, day: date) -> int:
        start, end = _day_bounds(day)
        bucket = day_bucket(Conversation.created_at, "UTC", start, end, dialect=self.dialect)
        created_rows = (await session.execute(
            select(
                bucket.label("day"),
                func.coalesce(Conversation.channel, "").label("channel"),
                func.count(Conversation.id).label("created"),
            )
            .where(Conversation.created_at >= start, Conversation.created_at < end)
            .group_by(bucket, func.coalesce(Conversation.channel, ""))
        )).all()
        per_key = {(str(row.day), row.channel): {"created": int(row.created)}
                   for row in created_rows}

        closed_bucket = day_bucket(Conversation.closed_at, "UTC", start, end, dialect=self.dialect)
        closed_rows = (await session.execute(
            select(
                closed_bucket.label("day"),
                func.coalesce(Conversation.channel, "").label("channel"),
                func.count(Conversation.id).label("resolved"),
            )
            .where(Conversation.closed_at.is_not(None),
                   Conversation.closed_at >= start, Conversation.closed_at < end)
            .group_by(closed_bucket, func.coalesce(Conversation.channel, ""))
        )).all()
        for row in closed_rows:
            key = (str(row.day), row.channel)
            per_key.setdefault(key, {})["resolved"] = int(row.resolved)

        event_bucket = day_bucket(ConversationEvent.created_at, "UTC", start, end,
                                  dialect=self.dialect)
        event_rows = (await session.execute(
            select(
                event_bucket.label("day"),
                func.coalesce(Conversation.channel, "").label("channel"),
                ConversationEvent.event_type.label("event_type"),
                func.count(ConversationEvent.id).label("count"),
            )
            .join(Conversation, Conversation.id == ConversationEvent.conversation_id)
            .where(ConversationEvent.created_at >= start, ConversationEvent.created_at < end)
            .group_by(event_bucket, func.coalesce(Conversation.channel, ""),
                      ConversationEvent.event_type)
        )).all()
        for row in event_rows:
            key = (str(row.day), row.channel)
            entry = per_key.setdefault(key, {})
            entry.setdefault("events", {})[row.event_type] = int(row.count)

        upserted = 0
        for (day_label, channel), metrics in per_key.items():
            upserted += await _upsert(
                session, AnalyticsDailyConversation,
                {"day": day_label, "channel": channel}, metrics,
            )
        return upserted

    async def _aggregate_scraping(self, session, day: date) -> int:
        start, end = _day_bounds(day)
        bucket = day_bucket(ScrapeJob.created_at, "UTC", start, end, dialect=self.dialect)
        rows = (await session.execute(
            select(
                bucket.label("day"),
                func.coalesce(ScrapeJob.actor_id, "").label("actor"),
                func.coalesce(ScrapeJob.actor_version, "").label("version"),
                func.count(ScrapeJob.id).label("jobs"),
                func.sum(case_job(ScrapeJob.status, "COMPLETED")).label("completed"),
                func.sum(case_job(ScrapeJob.status, "FAILED")).label("failed"),
                func.sum(case_job(ScrapeJob.status, "CANCELLED")).label("cancelled"),
                func.coalesce(func.sum(ScrapeJob.records_found), 0).label("found"),
                func.coalesce(func.sum(ScrapeJob.records_saved), 0).label("saved"),
                func.coalesce(func.sum(ScrapeJob.records_duplicate), 0).label("duplicates"),
                func.coalesce(func.sum(ScrapeJob.records_failed), 0).label("rejected"),
                func.sum(
                    func.extract("epoch", ScrapeJob.completed_at)
                    - func.extract("epoch", ScrapeJob.started_at)
                ).label("runtime_sum"),
                func.count(
                    func.extract("epoch", ScrapeJob.completed_at)
                    - func.extract("epoch", ScrapeJob.started_at)
                ).label("runtime_n"),
            )
            .where(ScrapeJob.created_at >= start, ScrapeJob.created_at < end)
            .group_by(bucket, func.coalesce(ScrapeJob.actor_id, ""),
                      func.coalesce(ScrapeJob.actor_version, ""))
        )).all()
        upserted = 0
        for row in rows:
            upserted += await _upsert(
                session, AnalyticsDailyScraping,
                {"day": str(row.day), "actor_id": row.actor, "actor_version": row.version},
                {
                    "jobs": int(row.jobs),
                    "completed": int(row.completed or 0),
                    "failed": int(row.failed or 0),
                    "cancelled": int(row.cancelled or 0),
                    "records_found": int(row.found or 0),
                    "records_saved": int(row.saved or 0),
                    "records_duplicate": int(row.duplicates or 0),
                    "records_failed": int(row.rejected or 0),
                    "runtime_sum": round(float(row.runtime_sum or 0), 3),
                    "runtime_n": int(row.runtime_n or 0),
                },
            )
        return upserted

    async def _aggregate_automation(self, session, day: date) -> int:
        start, end = _day_bounds(day)
        bucket = day_bucket(WorkflowExecution.created_at, "UTC", start, end, dialect=self.dialect)
        rows = (await session.execute(
            select(
                bucket.label("day"),
                func.coalesce(WorkflowExecution.workflow_id, "").label("workflow"),
                func.count(WorkflowExecution.id).label("executions"),
                func.sum(case_job(WorkflowExecution.status, "COMPLETED")).label("completed"),
                func.sum(case_job(WorkflowExecution.status, "FAILED")).label("failed"),
                func.sum(case_job(WorkflowExecution.status, "CANCELLED")).label("cancelled"),
                func.sum(
                    func.extract("epoch", WorkflowExecution.completed_at)
                    - func.extract("epoch", WorkflowExecution.started_at)
                ).label("duration_sum"),
                func.count(
                    func.extract("epoch", WorkflowExecution.completed_at)
                    - func.extract("epoch", WorkflowExecution.started_at)
                ).label("duration_n"),
            )
            .where(WorkflowExecution.created_at >= start, WorkflowExecution.created_at < end)
            .group_by(bucket, func.coalesce(WorkflowExecution.workflow_id, ""))
        )).all()

        steps = (await session.execute(
            select(
                func.coalesce(WorkflowExecution.workflow_id, "").label("workflow"),
                func.count(WorkflowExecutionStep.id).label("actions"),
            )
            .join(WorkflowExecutionStep,
                  WorkflowExecutionStep.execution_id == WorkflowExecution.id)
            .where(WorkflowExecution.created_at >= start,
                   WorkflowExecution.created_at < end,
                   WorkflowExecutionStep.node_type == "ACTION")
            .group_by(func.coalesce(WorkflowExecution.workflow_id, ""))
        )).all()
        actions_by_workflow = {row.workflow: int(row.actions) for row in steps}

        upserted = 0
        for row in rows:
            upserted += await _upsert(
                session, AnalyticsDailyAutomation,
                {"day": str(row.day), "workflow_id": str(row.workflow)},
                {
                    "executions": int(row.executions),
                    "completed": int(row.completed or 0),
                    "failed": int(row.failed or 0),
                    "cancelled": int(row.cancelled or 0),
                    "duration_sum": round(float(row.duration_sum or 0), 3),
                    "duration_n": int(row.duration_n or 0),
                    "actions": actions_by_workflow.get(row.workflow, 0),
                },
            )
        return upserted

    _AGGREGATORS = {
        AnalyticsDailyLead: _aggregate_leads,
        AnalyticsDailyCampaign: _aggregate_campaigns,
        AnalyticsDailyMessage: _aggregate_messages,
        AnalyticsDailyConversation: _aggregate_conversations,
        AnalyticsDailyScraping: _aggregate_scraping,
        AnalyticsDailyAutomation: _aggregate_automation,
    }

    # -------------------------------------------------------------- public
    async def refresh_incremental(
        self, session: AsyncSession, *, domains: list[str] | None = None,
        triggered_by: str = "WORKER", max_days: int = 400,
    ) -> list[dict]:
        """Refresh each table from its last aggregated day (−1 day overlap)
        through yesterday. Recoverable: interruption simply re-runs the range."""
        tables = self._tables_for(domains)
        results = []
        for table in tables:
            first_day, last_day = await _covered_range(session, table)
            last_done = await _last_aggregated_day(session, table)
            start_day = last_done if last_done is not None else first_day  # overlap day
            if start_day > last_day:
                results.append({"table": table.__tablename__, "status": "UP_TO_DATE",
                                "days": 0})
                continue
            day = start_day
            count = 0
            run = AnalyticsAggregationRun(
                table_name=table.__tablename__, triggered_by=triggered_by,
                status="RUNNING", day_start=start_day, day_end=last_day,
                started_at=datetime.now(UTC),
            )
            session.add(run)
            try:
                while day <= last_day and count < max_days:
                    aggregator = self._AGGREGATORS[table]
                    count += await aggregator(self, session, day)
                    day += timedelta(days=1)
                run.status = "COMPLETED"
                run.rows_upserted = count
                run.completed_at = datetime.now(UTC)
            except Exception as exc:  # noqa: BLE001 — record + re-raise for loop guard
                run.status = "FAILED"
                run.error = str(exc)[:2000]
                run.completed_at = datetime.now(UTC)
                await session.commit()
                raise
            results.append({"table": table.__tablename__, "status": "COMPLETED",
                            "days": count})
        await session.commit()
        return results

    async def rebuild_range(
        self, session: AsyncSession, day_start: date, day_end: date,
        *, domains: list[str] | None = None, triggered_by: str = "MANUAL",
    ) -> list[dict]:
        """Deterministic rebuild of an explicit day range (admin action).
        Upserts overwrite derived rows; operational data is never touched."""
        if day_end < day_start:
            raise ValueError("day_end must be on or after day_start")
        tables = self._tables_for(domains)
        results = []
        for table in tables:
            count = 0
            run = AnalyticsAggregationRun(
                table_name=table.__tablename__, triggered_by=triggered_by,
                status="RUNNING", day_start=day_start, day_end=day_end,
                started_at=datetime.now(UTC),
            )
            session.add(run)
            try:
                day = day_start
                while day <= day_end:
                    aggregator = self._AGGREGATORS[table]
                    count += await aggregator(self, session, day)
                    day += timedelta(days=1)
                run.status = "COMPLETED"
                run.rows_upserted = count
                run.completed_at = datetime.now(UTC)
            except Exception as exc:  # noqa: BLE001
                run.status = "FAILED"
                run.error = str(exc)[:2000]
                run.completed_at = datetime.now(UTC)
                await session.commit()
                raise
            results.append({"table": table.__tablename__, "status": "COMPLETED",
                            "days": count})
        await session.commit()
        return results

    async def recent_runs(self, session: AsyncSession, limit: int = 20) -> list[dict]:
        rows = (await session.execute(
            select(AnalyticsAggregationRun)
            .order_by(AnalyticsAggregationRun.created_at.desc())
            .limit(limit)
        )).scalars().all()
        return [row.to_public_dict() for row in rows]

    def _tables_for(self, domains: list[str] | None) -> list:
        if not domains:
            return list(TABLE_BY_DOMAIN.values())
        tables = []
        for domain in domains:
            table = TABLE_BY_DOMAIN.get(domain)
            if table is None:
                raise ValueError(f"Unknown aggregate domain: {domain}")
            tables.append(table)
        return tables


# ----------------------------------------------------------- diagnostics (§29)
async def run_diagnostics(session: AsyncSession, dialect: str = "sqlite") -> dict:
    """Data-quality checks. Flags anomalies ONLY — never modifies data."""
    findings: list[dict] = []

    def add(check: str, ok: bool, count: int, detail: str = "") -> None:
        findings.append({"check": check, "status": "ok" if ok else "anomaly",
                         "count": int(count), "detail": detail})

    # 1. negative counts inside aggregate metrics
    negative = 0
    for table in TABLE_BY_DOMAIN.values():
        rows = (await session.execute(select(table).limit(500))).scalars().all()
        for row in rows:
            for key, value in (row.metrics or {}).items():
                if isinstance(value, (int, float)) and value < 0:
                    negative += 1
    add("negative_aggregate_counts", negative == 0, negative)

    # 2. delivered > sent per campaign (impossible progression)
    rows = (await session.execute(
        select(
            Campaign.id,
            func.count(CampaignRecipient.id).label("recipients"),
            func.count(CampaignRecipient.sent_at).label("sent"),
            func.count(CampaignRecipient.delivered_at).label("delivered"),
        ).outerjoin(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .group_by(Campaign.id)
    )).all()
    impossible = sum(1 for r in rows if int(r.delivered or 0) > int(r.sent or 0))
    add("delivered_le_sent", impossible == 0, impossible,
        "campaigns where delivered_at count exceeds sent_at count")

    # 3. delivered_at present but sent_at missing
    orphans = int(await session.scalar(
        select(func.count(CampaignRecipient.id))
        .where(CampaignRecipient.delivered_at.is_not(None),
               CampaignRecipient.sent_at.is_(None))
    ) or 0)
    add("recipients_delivered_without_sent", orphans == 0, orphans)

    # 4. outbound messages delivered without sent timestamp
    orphans = int(await session.scalar(
        select(func.count(Message.id))
        .where(Message.direction == "OUT",
               Message.delivered_at.is_not(None),
               Message.sent_at.is_(None))
    ) or 0)
    add("messages_delivered_without_sent", orphans == 0, orphans)

    # 5. completed jobs with impossible timestamps
    rows = (await session.execute(
        select(func.count(ScrapeJob.id))
        .where(ScrapeJob.status == "COMPLETED",
               ScrapeJob.started_at.is_not(None),
               ScrapeJob.completed_at.is_not(None),
               ScrapeJob.completed_at < ScrapeJob.started_at)
    )).scalar()
    add("scrape_jobs_negative_duration", rows == 0, rows)

    # 6. aggregate mismatch vs live counts (leads domain, covered days)
    agg_rows = (await session.execute(
        select(AnalyticsDailyLead.day, AnalyticsDailyLead.metrics)
        .order_by(AnalyticsDailyLead.day)
    )).all()
    mismatch = None
    if agg_rows:
        agg_total = sum(int((metrics or {}).get("created", 0)) for _d, metrics in agg_rows)
        start_dt = datetime.combine(agg_rows[0][0], time.min, tzinfo=UTC)
        end_dt = datetime.combine(agg_rows[-1][0] + timedelta(days=1), time.min, tzinfo=UTC)
        live = int(await session.scalar(
            select(func.count(Lead.id))
            .where(Lead.created_at >= start_dt, Lead.created_at < end_dt)
        ) or 0)
        mismatch = {"aggregate": agg_total, "live": live,
                    "difference": agg_total - live}
        add("aggregate_leads_matches_live", agg_total == live, abs(agg_total - live),
            f"aggregate={agg_total} live={live}")

    # 7. executions referencing missing workflows
    orphan_rows = int(await session.scalar(
        select(func.count(WorkflowExecution.id))
        .outerjoin(Workflow, Workflow.id == WorkflowExecution.workflow_id)
        .where(Workflow.id.is_(None))
    ) or 0)
    add("workflow_executions_orphaned", orphan_rows == 0, orphan_rows)

    # 8. duplicate provider events (same campaign + provider event id)
    dupe_rows = (await session.execute(
        select(
            CampaignEvent.campaign_id,
            CampaignEvent.provider_event_id,
            func.count().label("count"),
        )
        .where(CampaignEvent.provider_event_id.is_not(None))
        .group_by(CampaignEvent.campaign_id, CampaignEvent.provider_event_id)
        .having(func.count() > 1)
        .limit(200)
    )).all()
    dupes = sum(int(row.count) for row in dupe_rows)
    add("duplicate_campaign_events", dupes == 0, dupes)

    anomalies = [f for f in findings if f["status"] == "anomaly"]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "checks_total": len(findings),
        "anomalies": len(anomalies),
        "findings": findings,
        "aggregate_mismatch": mismatch,
        "note": "Diagnostics are read-only; anomalies are flagged, never auto-fixed (spec §29)",
    }


def case_status(column, status: str):
    from sqlalchemy import case

    return case((column == status, 1), else_=0)


def case_job(column, status: str):
    from sqlalchemy import case

    return case((column == status, 1), else_=0)


def _merged_case():
    from sqlalchemy import case

    return case((Lead.merged_into_id.is_not(None), 1), else_=0)
