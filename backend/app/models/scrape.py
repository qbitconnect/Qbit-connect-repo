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
    Boolean,
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


class RunOutcome(str, enum.Enum):
    """Terminal-run outcome refinement (Actor Platform spec §11).

    `status` stays the workflow state machine (arch doc 09); `outcome` records
    HOW a run ended, derived from real events only (spec §42 — no faking):
    - SUCCEEDED: ran to natural completion
    - PARTIAL:   completed, but stopped early by a configured limit
    - TIMED_OUT: paused at a checkpoint because the wall-clock budget expired
                 (resumable — the run can continue)
    """

    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    TIMED_OUT = "TIMED_OUT"


class JobTrigger(str, enum.Enum):
    MANUAL = "MANUAL"
    TASK = "TASK"
    SCHEDULE = "SCHEDULE"
    API = "API"
    RETRY = "RETRY"


class EnrichmentStatus(str, enum.Enum):
    """Lifecycle of the contact-enrichment layer for one lead (spec §20)."""

    UNRICHED = "UNRICHED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    ENRICHED = "ENRICHED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # no enrichment path available (e.g. no website)


class ScheduleType(str, enum.Enum):
    ONCE = "ONCE"
    INTERVAL = "INTERVAL"
    DAILY = "DAILY"


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
    #: Phase 4 §26 — existing leads updated (merged into) by this job
    records_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: --- Actor Platform (spec §11/§20): run identity + outcome -------------
    #: optional operator label (from a Task or the API)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: saved configuration that produced this run (nullable FK)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("actor_tasks.id", ondelete="SET NULL"), nullable=True
    )
    #: MANUAL | TASK | SCHEDULE | API | RETRY
    trigger: Mapped[str] = mapped_column(
        String(12), nullable=False, default=JobTrigger.MANUAL.value,
        server_default=JobTrigger.MANUAL.value,
    )
    #: SUCCEEDED | PARTIAL | TIMED_OUT — only ever set from real run events
    outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)
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
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
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
            "name": self.name,
            "task_id": str(self.task_id) if self.task_id else None,
            "trigger": self.trigger,
            "outcome": self.outcome,
            "input": self.input or {},
            "config": self.config or {},
            "progress": self.progress,
            "stage": self.stage,
            "records_found": self.records_found,
            "records_saved": self.records_saved,
            "records_updated": self.records_updated,
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
    """Canonical lead record (brief §9, §21, §22, §23; extended in Phase 4).

    Normalized match keys (email_norm / phone_norm / website_norm) are indexed
    so dedup does single batched lookups instead of per-item scans.

    Phase 4 additions: contact name split, postal_code/industry, full provenance
    (source_id/source_type/import_file/batch), workflow status, deterministic
    quality_score, soft-merge pointer. The `tags` JSON column is a denormalized
    MIRROR of LeadTagAssignment names (kept in sync by the services); the
    relational tables in models/lead.py are the source of truth.
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
        # --- Phase 4 workspace indexes ---------------------------------------
        Index("ix_leads_status_created", "status", "created_at"),
        Index("ix_leads_source", "source"),
        Index("ix_leads_source_type", "source_type"),
        Index("ix_leads_city", "city"),
        Index("ix_leads_state", "state"),
        Index("ix_leads_country", "country"),
        Index("ix_leads_quality_score", "quality_score"),
        Index("ix_leads_created_at", "created_at"),
        Index("ix_leads_updated_at", "updated_at"),
        Index("ix_leads_import_batch", "import_batch_id"),
        Index("ix_leads_merged_into", "merged_into_id"),
        # --- Phase 11: tenancy + assignment ---------------------------------
        Index("ix_leads_organization", "organization_id"),
        Index("ix_leads_assigned_user", "assigned_user_id"),
        Index("ix_leads_assigned_team", "assigned_team_id"),
        Index("ix_leads_enrichment_status", "enrichment_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city: Mapped[str | None] = mapped_column(String(150), nullable=True)
    state: Mapped[str | None] = mapped_column(String(150), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(150), nullable=True)
    category: Mapped[str | None] = mapped_column(String(150), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(150), nullable=True)
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
    #: scraper | import | manual | api — how the lead entered the system
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    #: record id at the ORIGINAL source (e.g. provider place id)
    source_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_actor_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    imported_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- lifecycle counters -------------------------------------------------------
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: workflow status (LeadStatus); legacy values are mapped by migration 0003
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="NEW")
    #: deterministic 0-100 completeness score (Phase 4 §15) — not an AI prediction
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: soft-merge pointer — the surviving lead after a merge (never hard-delete history)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: enrichment lifecycle (spec §20) — UNRICHED until the enrichment layer runs
    enrichment_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EnrichmentStatus.UNRICHED.value,
        server_default=EnrichmentStatus.UNRICHED.value,
    )
    #: extracted/enriched-data confidence 0-100 (distinct from completeness
    #: quality_score); set by the enrichment layer / high-confidence matches
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # --- Phase 11: tenancy + assignment ------------------------------------------
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "business_name": self.business_name,
            "contact_name": self.contact_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "phone": self.phone,
            "website": self.website,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
            "country": self.country,
            "category": self.category,
            "industry": self.industry,
            "rating": self.rating,
            "review_count": self.review_count,
            "social_links": self.social_links or {},
            "metadata": self.metadata_json or {},
            "tags": self.tags or [],
            "source": self.source,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_actor_id": self.source_actor_id,
            "source_actor_version": self.source_actor_version,
            "source_job_id": str(self.source_job_id) if self.source_job_id else None,
            "import_batch_id": str(self.import_batch_id) if self.import_batch_id else None,
            "scraped_at": self.scraped_at.isoformat() if self.scraped_at else None,
            # --- normalized dedup keys (exposed for provenance/debug) ---------
            "email_norm": self.email_norm,
            "phone_norm": self.phone_norm,
            "website_norm": self.website_norm,
            "seen_count": self.seen_count,
            "status": self.status,
            "quality_score": self.quality_score,
            "enrichment_status": self.enrichment_status,
            "confidence": self.confidence,
            "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
            "merged_into_id": str(self.merged_into_id) if self.merged_into_id else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "assigned_user_id": str(self.assigned_user_id) if self.assigned_user_id else None,
            "assigned_team_id": str(self.assigned_team_id) if self.assigned_team_id else None,
            "organization_id": str(self.organization_id) if self.organization_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ScrapeSchedule(Base):
    """Recurring actor execution (spec §SCHEDULING, §RECURRING INTELLIGENCE).

    Dependency-free recurrence: ONCE (next_run_at), INTERVAL (every N
    seconds), DAILY (at hh:mm in an IANA timezone). The worker loop claims
    due rows with a guarded UPDATE (same lease discipline as automation
    executions) and enqueues a scrape job with the saved input/config.
    """

    __tablename__ = "scrape_schedules"
    __table_args__ = (
        Index("ix_scrape_schedules_next_run", "next_run_at"),
        Index("ix_scrape_schedules_actor", "actor_id"),
        Index("ix_scrape_schedules_organization", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    config: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)

    schedule_type: Mapped[str] = mapped_column(String(10), nullable=False)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM"
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: stop after N runs (None = unlimited; ONCE schedules use 1)
    max_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "actor_id": self.actor_id,
            "name": self.name,
            "input": self.input or {},
            "config": self.config or {},
            "schedule_type": self.schedule_type,
            "interval_seconds": self.interval_seconds,
            "daily_time": self.daily_time,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_job_id": str(self.last_job_id) if self.last_job_id else None,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "max_runs": self.max_runs,
            "last_error": self.last_error,
            "created_by": str(self.created_by) if self.created_by else None,
            "organization_id": str(self.organization_id) if self.organization_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class EntityLink(Base):
    """Source-graph evidence (spec §QBIT DIFFERENTIATION #1/#2).

    Records that two lead records — typically captured by DIFFERENT source
    actors (e.g. google-maps + website + email-finder) — refer to the same
    real-world business. Written by the dedup pipeline (MEDIUM matches stay
    separate leads) and the workspace duplicate scan; merge resolves the
    link (relation resolved_by=MERGED via leads.merged_into_id).
    """

    __tablename__ = "entity_links"
    __table_args__ = (
        Index("ix_entity_links_lead", "lead_id"),
        Index("ix_entity_links_related", "related_lead_id"),
        Index("ix_entity_links_organization", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    related_lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[str] = mapped_column(String(30), nullable=False, default="SAME_BUSINESS")
    #: evidence type: email | phone | website | name_key | manual
    matched_by: Mapped[str] = mapped_column(String(30), nullable=False)
    #: MatchConfidence bucket of the evidence (HIGH/MEDIUM/LOW)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="ACTIVE")
    resolved_by: Mapped[str | None] = mapped_column(String(20), nullable=True)  # MERGED/KEPT_BOTH
    source_actor_a: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_actor_b: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detected_by_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id),
            "related_lead_id": str(self.related_lead_id),
            "relation": self.relation,
            "matched_by": self.matched_by,
            "confidence": self.confidence,
            "status": self.status,
            "resolved_by": self.resolved_by,
            "source_actor_a": self.source_actor_a,
            "source_actor_b": self.source_actor_b,
            "detected_by_job_id": str(self.detected_by_job_id) if self.detected_by_job_id else None,
            "organization_id": str(self.organization_id) if self.organization_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
