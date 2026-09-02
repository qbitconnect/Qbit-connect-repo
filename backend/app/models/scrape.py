"""Scraping engine models (Phase 3): scrape_jobs, scrape_job_events,
scrape_job_checkpoints, leads.

Design rules (Phase 3 brief §12, §13, §22, §44):
- Job state lives in Postgres; Redis holds only ephemeral queue/lease data.
- No giant scraped datasets inside scrape_jobs — raw/normalized streams go to
  QBIT_DATA_DIR/scraper-results/{actor}/{job_id}/ as JSONL.
- Events are batched/aggregated; trivial item events never create millions of rows.
- Leads keep normalized match keys (email/phone/website) for efficient dedup.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, timestamp_columns, uuid_pk

PortableJSON = JSON().with_variant(JSONB(), "postgresql")


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: Legal transitions (architecture doc 09 §2). Enforced by the job engine.
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.CANCELLED},
    JobStatus.RUNNING: {
        JobStatus.PAUSED,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.PAUSED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    # terminal states
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.QUEUED},  # operator retry
    JobStatus.CANCELLED: set(),
}

#: DB-side request the operator can make on a RUNNING job (cooperative control).
STOP_REQUESTS = ("NONE", "PAUSE", "CANCEL")


class ScrapeJob(Base):
    __tablename__ = "scrape_jobs"
    __table_args__ = (
        Index("ix_scrape_jobs_status_created", "status", "created_at"),
        Index("ix_scrape_jobs_actor_created", "actor_id", "created_at"),
        Index("ix_scrape_jobs_created_by", "created_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_version: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JobStatus.QUEUED)
    #: Validated actor input (already schema-checked before the row is created).
    input: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: Advanced limits (max_runtime, max_pages, rps, ...) — job-level overrides.
    config: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    stage: Mapped[str | None] = mapped_column(String(100), nullable=True)
    records_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_saved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    resumed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_requested: Mapped[str] = mapped_column(String(10), nullable=False, default="NONE")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            JobStatus.COMPLETED,
            JobStatus.CANCELLED,
        ) or (self.status == JobStatus.FAILED)

    @property
    def is_active(self) -> bool:
        return self.status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "actor_id": self.actor_id,
            "actor_version": self.actor_version,
            "status": self.status,
            "input": self.input or {},
            "config": self.config or {},
            "progress": self.progress,
            "stage": self.stage,
            "records_found": self.records_found,
            "records_saved": self.records_saved,
            "records_duplicate": self.records_duplicate,
            "records_failed": self.records_failed,
            "attempt": self.attempt,
            "resumed_count": self.resumed_count,
            "stop_requested": self.stop_requested,
            "error": self.error,
            "error_code": self.error_code,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "paused_at": self.paused_at.isoformat() if self.paused_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": str(self.created_by) if self.created_by else None,
        }


class ScrapeJobEvent(Base):
    __tablename__ = "scrape_job_events"
    __table_args__ = (Index("ix_scrape_job_events_job_created", "job_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("scrape_jobs.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = timestamp_columns()[0]


class ScrapeJobCheckpoint(Base):
    """Persistent checkpoint storage (brief §18: never only in Redis)."""

    __tablename__ = "scrape_job_checkpoints"
    __table_args__ = (Index("ix_scrape_job_checkpoints_job", "job_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("scrape_jobs.id", ondelete="CASCADE"), nullable=False
    )
    cursor: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    records_processed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = timestamp_columns()[0]


class Lead(Base):
    """Canonical lead record (brief §9, §21, §22, §23).

    Normalized match keys (email_norm / phone_norm / website_norm) are indexed
    so dedup does single batched lookups instead of per-item scans.
    """

    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_email_norm", "email_norm"),
        Index("ix_leads_phone_norm", "phone_norm"),
        Index("ix_leads_website_norm", "website_norm"),
        Index("ix_leads_name_key", "name_key"),
        Index("ix_leads_source_job", "source_job_id"),
        Index("ix_leads_source_actor", "source_actor_id"),
        Index("ix_leads_business_name", "business_name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city: Mapped[str | None] = mapped_column(String(150), nullable=True)
    state: Mapped[str | None] = mapped_column(String(150), nullable=True)
    country: Mapped[str | None] = mapped_column(String(150), nullable=True)
    category: Mapped[str | None] = mapped_column(String(150), nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    social_links: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    metadata_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    tags: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)

    # --- normalized dedup keys -------------------------------------------------
    email_norm: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone_norm: Mapped[str | None] = mapped_column(String(40), nullable=True)
    website_norm: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: lowercase business_name + "|" + lowercase city + "|" + lowercase country
    name_key: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- source tracking (brief §23) --------------------------------------------
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_actor_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- lifecycle counters -------------------------------------------------------
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "business_name": self.business_name,
            "contact_name": self.contact_name,
            "email": self.email,
            "phone": self.phone,
            "website": self.website,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "category": self.category,
            "rating": self.rating,
            "review_count": self.review_count,
            "social_links": self.social_links or {},
            "metadata": self.metadata_json or {},
            "tags": self.tags or [],
            "source": self.source,
            "source_url": self.source_url,
            "source_actor_id": self.source_actor_id,
            "source_actor_version": self.source_actor_version,
            "source_job_id": str(self.source_job_id) if self.source_job_id else None,
            "scraped_at": self.scraped_at.isoformat() if self.scraped_at else None,
            "seen_count": self.seen_count,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
