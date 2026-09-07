"""Phase 10 analytics & reporting tables (models only — query logic lives in
app/analytics).

Tables:
- Report                  saved report configuration (persisted, versioned)
- ReportRun               one background execution of a report (QUEUED→…)
- ReportSnapshot          immutable result payload of a completed run
- AnalyticsDailyLead      aggregate: leads created per (day, source)
- AnalyticsDailyCampaign  aggregate: campaign/message events per (day, channel)
- AnalyticsDailyMessage   aggregate: conversation messages per (day, channel, direction)
- AnalyticsDailyConversation aggregate: inbox conversations per (day, channel)
- AnalyticsDailyScraping  aggregate: scrape jobs per (day, actor, version)
- AnalyticsDailyAutomation aggregate: workflow executions per (day, workflow)
- AnalyticsAggregationRun  bookkeeping for aggregate refresh/rebuild runs

Design rules (Phase 10 spec §20–§21, §39):
- additive-only vs Phase 1–9; PortableJSON so SQLite tests and PG prod match
- aggregates are DERIVED data: operational tables remain the source of truth;
  every aggregate row can be rebuilt from them at any time
- aggregates are idempotent: one row per unique (day, dimension) key, upserted
- snapshots store bounded JSON; large exports go through StorageService files
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, timestamp_columns, uuid_pk
from app.models.scrape import PortableJSON


class ReportStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ReportVisibility(str, enum.Enum):
    PRIVATE = "PRIVATE"
    TEAM = "TEAM"      # any signed-in operator with reports.view (no team model yet)
    GLOBAL = "GLOBAL"


class ReportRunStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReportDomain(str, enum.Enum):
    OVERVIEW = "OVERVIEW"
    LEADS = "LEADS"
    SCRAPING = "SCRAPING"
    MARKETING = "MARKETING"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    INBOX = "INBOX"
    AUTOMATION = "AUTOMATION"
    TEAM = "TEAM"


class Report(Base):
    """A saved report: metric/dimension/filter configuration persisted for
    reproducible background execution (spec §17–§18)."""

    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_owner_status", "owner_id", "status"),
        Index("ix_reports_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: OVERVIEW | LEADS | SCRAPING | MARKETING | WHATSAPP | EMAIL | INBOX | AUTOMATION | TEAM
    domain: Mapped[str] = mapped_column(String(20), nullable=False)
    #: validated, allowlisted configuration (metrics/dimensions/filters/range/
    #: grouping/sort/visualization) — schema checked by reports.schemas
    config: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: bumped when the metric/query implementation changes → snapshot provenance
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: PRIVATE (owner + managers) | TEAM (any reports.view holder) | GLOBAL
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, default="PRIVATE")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ReportStatus.ACTIVE)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: IANA timezone name the report's day boundaries are computed in
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "domain": self.domain,
            "config": self.config or {},
            "config_version": self.config_version,
            "visibility": self.visibility,
            "status": self.status,
            "owner_id": str(self.owner_id) if self.owner_id else None,
            "timezone": self.timezone,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ReportRun(Base):
    """One execution of a report. Background worker claims QUEUED rows with a
    lease (same pattern as imports/exports/outbox)."""

    __tablename__ = "report_runs"
    __table_args__ = (
        Index("ix_report_runs_report_created", "report_id", "created_at"),
        Index("ix_report_runs_status", "status"),
        Index("ix_report_runs_requested_by", "requested_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ReportRunStatus.QUEUED)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    #: frozen copy of the report config used for THIS run (reproducibility)
    config_snapshot: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    format: Mapped[str] = mapped_column(String(10), nullable=False, default="json")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "report_id": str(self.report_id),
            "status": self.status,
            "requested_by": str(self.requested_by) if self.requested_by else None,
            "config_version": self.config_version,
            "timezone": self.timezone,
            "format": self.format,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ReportSnapshot(Base):
    """Immutable result of a completed run. `data` is bounded
    (QBIT_ANALYTICS_MAX_SNAPSHOT_ROWS); wide/large tabular output is written to
    StorageService instead and referenced via export_file_id."""

    __tablename__ = "report_snapshots"
    __table_args__ = (
        Index("ix_report_snapshots_run", "run_id"),
        Index("ix_report_snapshots_report_created", "report_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("report_runs.id", ondelete="CASCADE"), nullable=False
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    export_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL"), nullable=True
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self, include_data: bool = False) -> dict:
        out = {
            "id": str(self.id),
            "report_id": str(self.report_id),
            "run_id": str(self.run_id),
            "row_count": self.row_count,
            "export_file_id": str(self.export_file_id) if self.export_file_id else None,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_data:
            out["data"] = self.data or {}
        return out


class AnalyticsAggregationRun(Base):
    """Bookkeeping row for every aggregate refresh/rebuild (spec §21):
    deterministic recovery, status visibility, audit support."""

    __tablename__ = "analytics_aggregation_runs"
    __table_args__ = (
        Index("ix_analytics_agg_runs_table_created", "table_name", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: WORKER | MANUAL
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False, default="WORKER")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    day_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    day_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    rows_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "table_name": self.table_name,
            "triggered_by": self.triggered_by,
            "status": self.status,
            "day_start": self.day_start.isoformat() if self.day_start else None,
            "day_end": self.day_end.isoformat() if self.day_end else None,
            "rows_upserted": self.rows_upserted,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class _DailyAggregateBase(Base):
    """Shared shape for daily aggregate rows: one row per (day, dimension key)
    with a bounded metrics payload. Upserted idempotently by the aggregation
    worker; always rebuildable from operational tables."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = uuid_pk()
    day: Mapped[date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]


class AnalyticsDailyLead(_DailyAggregateBase):
    __tablename__ = "analytics_daily_leads"
    __table_args__ = (
        UniqueConstraint("day", "source", name="uq_analytics_daily_leads_day_source"),
        Index("ix_analytics_daily_leads_day", "day"),
    )
    #: '' = unknown/unset source (lead rows with NULL source group here)
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="")


class AnalyticsDailyCampaign(_DailyAggregateBase):
    __tablename__ = "analytics_daily_campaigns"
    __table_args__ = (
        UniqueConstraint("day", "channel", name="uq_analytics_daily_campaigns_day_channel"),
        Index("ix_analytics_daily_campaigns_day", "day"),
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="")


class AnalyticsDailyMessage(_DailyAggregateBase):
    __tablename__ = "analytics_daily_messages"
    __table_args__ = (
        UniqueConstraint("day", "channel", "direction", name="uq_analytics_daily_messages_key"),
        Index("ix_analytics_daily_messages_day", "day"),
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    #: IN | OUT
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="")


class AnalyticsDailyConversation(_DailyAggregateBase):
    __tablename__ = "analytics_daily_conversations"
    __table_args__ = (
        UniqueConstraint("day", "channel", name="uq_analytics_daily_conversations_key"),
        Index("ix_analytics_daily_conversations_day", "day"),
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="")


class AnalyticsDailyScraping(_DailyAggregateBase):
    __tablename__ = "analytics_daily_scraping"
    __table_args__ = (
        UniqueConstraint("day", "actor_id", "actor_version",
                         name="uq_analytics_daily_scraping_key"),
        Index("ix_analytics_daily_scraping_day", "day"),
    )
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    actor_version: Mapped[str] = mapped_column(String(20), nullable=False, default="")


class AnalyticsDailyAutomation(_DailyAggregateBase):
    __tablename__ = "analytics_daily_automation"
    __table_args__ = (
        UniqueConstraint("day", "workflow_id", name="uq_analytics_daily_automation_key"),
        Index("ix_analytics_daily_automation_day", "day"),
    )
    workflow_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
