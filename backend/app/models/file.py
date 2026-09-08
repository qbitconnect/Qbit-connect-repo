"""files table — metadata only; binary content lives on QBIT storage (Brief §7)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, timestamp_columns, uuid_pk

PortableJSON = JSON().with_variant(JSONB(), "postgresql")


class FileCategory(str, enum.Enum):
    IMPORT = "IMPORT"
    EXPORT = "EXPORT"
    SCRAPER_RESULT = "SCRAPER_RESULT"
    CAMPAIGN_ATTACHMENT = "CAMPAIGN_ATTACHMENT"
    BACKUP = "BACKUP"
    OTHER = "OTHER"


class FileRecord(Base):
    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_category_created", "category", "created_at"),
        Index("ix_files_organization", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(260), nullable=False)
    #: Storage key RELATIVE to the category root. Never an absolute filesystem path.
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    storage_backend: Mapped[str] = mapped_column(String(50), nullable=False, default="local")
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )  # nullable until auth exists; set from authenticated user
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]
    metadata_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "mime_type": self.mime_type,
            "size": self.size,
            "storage_backend": self.storage_backend,
            "category": self.category,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "checksum_sha256": self.checksum_sha256,
            "metadata": self.metadata_json or {},
            "is_deleted": self.is_deleted,
        }
