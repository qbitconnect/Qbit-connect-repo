"""QBIT ACTOR PLATFORM — storage models (spec §10-§14, §20, §22, §26).

Tables (all additive; migration 0013 — brief §37: no drops, no rewrites):
- actor_tasks             saved actor configurations ("Run Task" = reusable input)
- actor_datasets          one dataset per run (columns/row-count/status)
- actor_dataset_items     normalized rows (JSON per item, batched inserts)
- actor_kv_entries        key-value storage (actor state, checkpoints, metadata)
- actor_request_queue     persistent per-run URL queue (priority/depth/retries)
- actor_health_checks     health-monitor history per actor
- run_webhooks            run-lifecycle webhook subscriptions
- run_webhook_deliveries  delivery attempts with retry bookkeeping

ScrapeJob gains nullable columns: task_id, name, trigger, outcome
(SUCCEEDED / PARTIAL / TIMED_OUT — derived honestly from real run state).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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
from app.models.scrape import PortableJSON


class DatasetStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    READY = "READY"
    EMPTY = "EMPTY"
    FAILED = "FAILED"


class QueueItemStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class ActorTask(Base):
    """Saved actor configuration (spec §20): run / duplicate / edit / schedule."""

    __tablename__ = "actor_tasks"
    __table_args__ = (
        Index("ix_actor_tasks_actor", "actor_id"),
        Index("ix_actor_tasks_organization", "organization_id"),
        Index("ix_actor_tasks_created_by", "created_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    input: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    config: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "actor_id": self.actor_id,
            "name": self.name,
            "description": self.description,
            "input": self.input or {},
            "config": self.config or {},
            "enabled": self.enabled,
            "run_count": self.run_count,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_job_id": str(self.last_job_id) if self.last_job_id else None,
            "created_by": str(self.created_by) if self.created_by else None,
            "organization_id": str(self.organization_id) if self.organization_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ActorDataset(Base):
    """Dataset abstraction (spec §10): every run can produce one dataset."""

    __tablename__ = "actor_datasets"
    __table_args__ = (
        Index("ix_actor_datasets_job", "job_id"),
        Index("ix_actor_datasets_actor", "actor_id"),
        Index("ix_actor_datasets_created", "created_at"),
        Index("ix_actor_datasets_organization", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("scrape_jobs.id", ondelete="SET NULL"), nullable=True
    )
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=DatasetStatus.RUNNING.value
    )
    #: clean | partial — partial = stopped early by a limit (honest, spec §42)
    clean_status: Mapped[str] = mapped_column(String(12), nullable=False, default="clean")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: ordered list of top-level fields seen across items (dataset "schema")
    schema_fields: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "job_id": str(self.job_id) if self.job_id else None,
            "actor_id": self.actor_id,
            "actor_version": self.actor_version,
            "name": self.name,
            "status": self.status,
            "clean_status": self.clean_status,
            "item_count": self.item_count,
            "schema_fields": self.schema_fields or [],
            "organization_id": str(self.organization_id) if self.organization_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ActorDatasetItem(Base):
    """One dataset row. `idx` preserves actor yield order for stable paging."""

    __tablename__ = "actor_dataset_items"
    __table_args__ = (
        Index("ix_actor_dataset_items_dataset_idx", "dataset_id", "idx"),
        Index("ix_actor_dataset_items_dataset_created", "dataset_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("actor_datasets.id", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    data: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = timestamp_columns()[0]


class ActorKvEntry(Base):
    """Key-value storage (spec §14): actor state, checkpoints, metadata."""

    __tablename__ = "actor_kv_entries"
    __table_args__ = (
        Index("ix_actor_kv_scope", "scope"),
        Index("ix_actor_kv_scope_key", "scope", "key", unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(String(200), nullable=False, default="global")
    key: Mapped[str] = mapped_column(String(300), nullable=False)
    value: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    content_type: Mapped[str] = mapped_column(String(40), nullable=False, default="json")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "scope": self.scope,
            "key": self.key,
            "value": self.value,
            "content_type": self.content_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ActorRequestQueueItem(Base):
    """Persistent request queue (spec §13): large crawls without RAM hoarding."""

    __tablename__ = "actor_request_queue"
    __table_args__ = (
        Index("ix_actor_rq_queue_status", "queue_name", "status"),
        Index("ix_actor_rq_queue_key", "queue_name", "url_norm", unique=True),
        Index("ix_actor_rq_job", "job_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    queue_name: Mapped[str] = mapped_column(String(200), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: lowercased scheme://host/path?query — dedup key (fragment stripped)
    url_norm: Mapped[str] = mapped_column(String(1000), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=QueueItemStatus.PENDING.value
    )
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = timestamp_columns()[0]
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unique_keys: dict = {}

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "queue_name": self.queue_name,
            "job_id": str(self.job_id) if self.job_id else None,
            "url": self.url,
            "method": self.method,
            "priority": self.priority,
            "status": self.status,
            "retries": self.retries,
            "depth": self.depth,
            "parent_url": self.parent_url,
            "error": self.error,
            "discovered_at": self.discovered_at.isoformat() if self.discovered_at else None,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
        }


class ActorHealthCheck(Base):
    """Health-monitor history (spec §26): HEALTHY/DEGRADED/FAILING/UNKNOWN."""

    __tablename__ = "actor_health_checks"
    __table_args__ = (
        Index("ix_actor_health_actor_time", "actor_id", "checked_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    dependencies: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: registry | probe | synthetic — which check produced the row
    check_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="registry")
    checked_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "actor_id": self.actor_id,
            "status": self.status,
            "detail": self.detail,
            "dependencies": self.dependencies or {},
            "duration_ms": self.duration_ms,
            "check_kind": self.check_kind,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


class RunWebhook(Base):
    """Run-lifecycle webhook subscription (spec §22)."""

    __tablename__ = "run_webhooks"
    __table_args__ = (
        Index("ix_run_webhooks_actor", "actor_id"),
        Index("ix_run_webhooks_organization", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: HMAC-SHA256 secret; write-only (never returned by the API)
    secret: Mapped[str] = mapped_column(String(300), nullable=False)
    events: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)
    #: None = all actors
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "url": self.url,
            "events": self.events or [],
            "actor_id": self.actor_id,
            "enabled": self.enabled,
            "has_secret": bool(self.secret),
            "organization_id": str(self.organization_id) if self.organization_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class RunWebhookDelivery(Base):
    """One delivery per (webhook, event) with retry bookkeeping (spec §22)."""

    __tablename__ = "run_webhook_deliveries"
    __table_args__ = (
        Index("ix_run_whd_webhook_time", "webhook_id", "created_at"),
        Index("ix_run_whd_status_next", "status", "next_retry_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("run_webhooks.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "webhook_id": str(self.webhook_id),
            "event": self.event,
            "job_id": str(self.job_id) if self.job_id else None,
            "actor_id": self.actor_id,
            "status": self.status,
            "attempts": self.attempts,
            "last_status_code": self.last_status_code,
            "last_error": self.last_error,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
