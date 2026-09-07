"""Workflow Automation Engine models (Phase 9).

Additive-only — no existing tables are modified. Statuses follow the codebase
convention (String columns + Python str-Enum vocabularies, never native DB
enums). All PKs are UUID. `workflow_executions` doubles as the queue
(`status=QUEUED` + `next_execution_at` + `locked_at/lease_owner`) using the
same Postgres guarded-UPDATE lease pattern as `campaign_queue` / `inbox_outbox`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, timestamp_columns, uuid_pk
from app.models.scrape import PortableJSON


class WorkflowStatus:
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"

    ALL = {DRAFT, ACTIVE, PAUSED, ARCHIVED}


class WorkflowVersionStatus:
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"

    ALL = {DRAFT, PUBLISHED, RETIRED}


class ExecutionStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"

    ACTIVE_STATUSES = {QUEUED, RUNNING, WAITING}
    TERMINAL_STATUSES = {COMPLETED, FAILED, CANCELLED}


class StepStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"

    ALL = {PENDING, RUNNING, COMPLETED, SKIPPED, FAILED}


class NodeTypes:
    TRIGGER = "TRIGGER"
    CONDITION = "CONDITION"
    ACTION = "ACTION"
    WAIT = "WAIT"
    BRANCH = "BRANCH"
    END = "END"

    ALL = {TRIGGER, CONDITION, ACTION, WAIT, BRANCH, END}


class ErrorClass:
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    CONFIGURATION = "CONFIGURATION"
    PERMISSION = "PERMISSION"

    ALL = {TRANSIENT, PERMANENT, CONFIGURATION, PERMISSION}


class Workflow(Base):
    """A named automation. `current_version` points at the latest PUBLISHED
    version number (0 = never published). Definitions are immutable after
    publication — edits always create a new draft version (§2, §60)."""

    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=WorkflowStatus.DRAFT, index=True)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = timestamp_columns()

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "current_version": self.current_version,
            "created_by": str(self.created_by) if self.created_by else None,
            "updated_by": str(self.updated_by) if self.updated_by else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WorkflowVersion(Base):
    """Immutable definition snapshot. A running execution always references
    the exact version row it started with (§3, §60)."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(PortableJSON, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=WorkflowVersionStatus.DRAFT)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at, updated_at = timestamp_columns()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "workflow_id": str(self.workflow_id),
            "version": self.version,
            "definition": self.definition,
            "checksum": self.checksum,
            "status": self.status,
            "created_by": str(self.created_by) if self.created_by else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class WorkflowExecution(Base):
    """One run of one workflow version for one triggering event. The table is
    the execution queue: QUEUED rows with a due `next_execution_at` are claimed
    by guarded UPDATE (§32, §58, §59)."""

    __tablename__ = "workflow_executions"
    __table_args__ = (
        # Event idempotency (§13): the same event can start an execution of a
        # given workflow exactly once.
        UniqueConstraint("workflow_id", "trigger_event_id", name="uq_workflow_executions_event"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"), nullable=False
    )
    trigger_event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ExecutionStatus.QUEUED, index=True)
    current_node_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    context: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_execution_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = timestamp_columns()

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "workflow_id": str(self.workflow_id),
            "workflow_version_id": str(self.workflow_version_id),
            "trigger_event_id": self.trigger_event_id,
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id) if self.entity_id else None,
            "status": self.status,
            "current_node_id": self.current_node_id,
            "context": self.context,
            "attempts": self.attempts,
            "next_execution_at": self.next_execution_at.isoformat() if self.next_execution_at else None,
            "error": self.error,
            "error_class": self.error_class,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class WorkflowExecutionStep(Base):
    """Audit trail of every node execution (§33). Snapshots hold configuration
    and safe outputs only — never secrets or raw provider responses."""

    __tablename__ = "workflow_execution_steps"
    __table_args__ = ()

    id: Mapped[uuid.UUID] = uuid_pk()
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(60), nullable=False)
    node_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=StepStatus.PENDING, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_snapshot: Mapped[dict | None] = mapped_column(PortableJSON, nullable=True)
    output_snapshot: Mapped[dict | None] = mapped_column(PortableJSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at, updated_at = timestamp_columns()

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "execution_id": str(self.execution_id),
            "node_id": self.node_id,
            "node_type": self.node_type,
            "status": self.status,
            "attempt": self.attempt,
            "input_snapshot": self.input_snapshot,
            "output_snapshot": self.output_snapshot,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class WorkflowEvent(Base):
    """Intake log of system events seen by the automation engine (§12, §41).

    `event_id` is globally unique (idempotency at intake); `causation_id`
    records the workflow execution that produced this event (loop protection
    and debugging — §40, §41)."""

    __tablename__ = "workflow_events"
    __table_args__ = ()

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    payload: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    causation_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at, updated_at = timestamp_columns()

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "event_id": self.event_id,
            "event_type": self.event_type,
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id) if self.entity_id else None,
            "payload": self.payload,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
