"""Phase 4 lead workspace tables.

- LeadTag / LeadTagAssignment  — relational tag source of truth (the `tags`
  JSON column on `leads` is a denormalized display mirror kept in sync here).
- LeadNote                     — multiple notes per lead, author tracked.
- LeadActivity                 — per-lead activity trail (lead created/imported/
  updated/status/tag/note/export/merge/archive). Separate from the global
  audit log but complementary to it.
- LeadMergeHistory             — before/after snapshots; never hard-delete history.
- LeadDuplicateCandidate       — persisted duplicate review queue (§8).
- SavedView                    — saved filters with PRIVATE/TEAM/GLOBAL visibility.
- ImportBatch                  — CSV/XLSX/JSON/JSONL import batches + counters.
- LeadExportRecord             — export history + background job state.

Design rules: additive-only vs Phase 1–3, JSONB via PortableJSON (SQLite-safe),
UUID PKs, cascade deletes only where the parent owns the child (notes,
activities, assignments die with their lead; merge history and export records
survive because they are historical evidence).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
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


class LeadStatus(str, enum.Enum):
    """Default workflow statuses (§3). Configurable via system setting
    `leads.statuses` (JSON list) — the system never hard-codes behavior
    beyond this default set."""

    NEW = "NEW"
    VERIFIED = "VERIFIED"
    QUALIFIED = "QUALIFIED"
    CONTACTED = "CONTACTED"
    REPLIED = "REPLIED"
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    CONVERTED = "CONVERTED"
    LOST = "LOST"
    ARCHIVED = "ARCHIVED"


class ImportStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExportStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DuplicateStatus(str, enum.Enum):
    PENDING = "PENDING"
    MERGED = "MERGED"
    KEPT_BOTH = "KEPT_BOTH"
    IGNORED = "IGNORED"


class DuplicateConfidence(str, enum.Enum):
    """Workspace-level confidence ladder (§7) — stricter than the pipeline's."""

    EXACT = "EXACT"      # normalized email/phone/source-id identical
    HIGH = "HIGH"        # normalized website/domain identical
    MEDIUM = "MEDIUM"    # business name + city, or name + phone
    LOW = "LOW"          # fuzzy business name only — never auto-anything


class LeadTag(Base):
    __tablename__ = "lead_tags"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_system: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "color": self.color,
            "is_system": self.is_system,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LeadTagAssignment(Base):
    __tablename__ = "lead_tag_assignments"
    __table_args__ = (
        UniqueConstraint("lead_id", "tag_id", name="uq_lead_tag_once"),
        Index("ix_lead_tag_tag", "tag_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("lead_tags.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]


class LeadNote(Base):
    __tablename__ = "lead_notes"
    __table_args__ = (Index("ix_lead_notes_lead_created", "lead_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class LeadActivity(Base):
    """Per-lead activity trail (§6). Written by the service layer only."""

    __tablename__ = "lead_activities"
    __table_args__ = (Index("ix_lead_activities_lead_created", "lead_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "event_type": self.event_type,
            "message": self.message,
            "metadata": self.metadata_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LeadMergeHistory(Base):
    """Evidence row for every merge (§9). Rows are never deleted by merges."""

    __tablename__ = "lead_merge_history"
    __table_args__ = (Index("ix_lead_merge_primary", "primary_lead_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    primary_lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    merged_lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    before_data: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    after_data: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: field → {"primary": x, "merged": y} for every conflicting value kept on record
    conflicts: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "primary_lead_id": str(self.primary_lead_id),
            "merged_lead_id": str(self.merged_lead_id),
            "before_data": self.before_data or {},
            "after_data": self.after_data or {},
            "conflicts": self.conflicts or {},
            "user_id": str(self.user_id) if self.user_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LeadDuplicateCandidate(Base):
    """Persisted duplicate review queue. Lead ids are stored in canonical
    (uuid-ordered) pairs so the same pair can never appear twice."""

    __tablename__ = "lead_duplicate_candidates"
    __table_args__ = (
        UniqueConstraint("lead_a_id", "lead_b_id", name="uq_duplicate_pair"),
        Index("ix_dup_candidates_status_created", "status", "created_at"),
        Index("ix_dup_candidates_lead_a", "lead_a_id"),
        Index("ix_dup_candidates_lead_b", "lead_b_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_a_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lead_b_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    matched_on: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=DuplicateStatus.PENDING)
    #: scraper | import | scan
    origin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detected_by_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "lead_a_id": str(self.lead_a_id),
            "lead_b_id": str(self.lead_b_id),
            "confidence": self.confidence,
            "matched_on": self.matched_on,
            "status": self.status,
            "origin": self.origin,
            "detected_by_job_id": str(self.detected_by_job_id) if self.detected_by_job_id else None,
            "resolution_note": self.resolution_note,
            "resolved_by": str(self.resolved_by) if self.resolved_by else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SavedView(Base):
    __tablename__ = "saved_views"
    __table_args__ = (Index("ix_saved_views_owner", "owner_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    entity: Mapped[str] = mapped_column(String(50), nullable=False, default="leads")
    filters: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: PRIVATE (owner only) | TEAM (any signed-in operator) | GLOBAL
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, default="PRIVATE")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "entity": self.entity,
            "filters": self.filters or {},
            "visibility": self.visibility,
            "owner_id": str(self.owner_id) if self.owner_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        Index("ix_import_batches_status_created", "status", "created_at"),
        Index("ix_import_batches_created_by", "created_by"),
        Index("ix_import_batches_organization", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    filename: Mapped[str] = mapped_column(String(260), nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    #: csv | xlsx | json | jsonl
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ImportStatus.QUEUED)
    #: {column_name: lead_field} mapping chosen in the wizard
    mapping: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: {duplicate_strategy, default_status, tags, sheet, ...}
    options: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: [{row, field, error}] — bounded; full rejected rows go to the error file
    error_summary: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)
    error_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # --- Phase 11: tenancy -------------------------------------------------------
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "filename": self.filename,
            "file_id": str(self.file_id) if self.file_id else None,
            "format": self.format,
            "status": self.status,
            "mapping": self.mapping or {},
            "options": self.options or {},
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "imported_rows": self.imported_rows,
            "duplicate_rows": self.duplicate_rows,
            "updated_rows": self.updated_rows,
            "review_rows": self.review_rows,
            "error_summary": (self.error_summary or [])[:100],
            "error_file_id": str(self.error_file_id) if self.error_file_id else None,
            "created_by": str(self.created_by) if self.created_by else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LeadExportRecord(Base):
    __tablename__ = "lead_exports"
    __table_args__ = (
        Index("ix_lead_exports_status_created", "status", "created_at"),
        Index("ix_lead_exports_created_by", "created_by"),
        Index("ix_lead_exports_organization", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    #: csv | xlsx | json | jsonl
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    #: selected | filtered | page | all | lead
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="filtered")
    filters: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    fields: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ExportStatus.QUEUED)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # --- Phase 11: tenancy -------------------------------------------------------
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "format": self.format,
            "scope": self.scope,
            "filters": self.filters or {},
            "fields": self.fields or [],
            "row_count": self.row_count,
            "status": self.status,
            "file_id": str(self.file_id) if self.file_id else None,
            "error": self.error,
            "created_by": str(self.created_by) if self.created_by else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
