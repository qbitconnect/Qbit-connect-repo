"""connections table (Brief §8) — schema only in Phase 2; endpoints come later."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, timestamp_columns, uuid_pk

PortableJSON = JSON().with_variant(JSONB(), "postgresql")


class ConnectionCategory(str, enum.Enum):
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    SMS = "SMS"
    STORAGE = "STORAGE"
    OTHER = "OTHER"


class ConnectionStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    DISCONNECTED = "DISCONNECTED"
    NEEDS_REAUTH = "NEEDS_REAUTH"


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = uuid_pk()
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    #: public identifier (phone id / email address / account ref) — NOT a secret
    identifier: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="PENDING")
    status_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    capabilities: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: non-secret configuration only; secrets go to the encrypted vault (later phase)
    config: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: vault reference (key id), never the secret itself
    secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]
