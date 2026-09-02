"""system_settings table (Brief §11).

Ordinary settings only — sensitive provider secrets never live here; they belong
to the encrypted credential vault (later phase, architecture doc 17).
"""

from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 - required by SQLAlchemy annotation eval
from typing import Any

from sqlalchemy import String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, timestamp_columns, uuid_pk

PortableJSON = JSON().with_variant(JSONB(), "postgresql")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    value: Mapped[Any] = mapped_column(PortableJSON, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]  # type: ignore[misc]
    updated_at: Mapped[datetime] = timestamp_columns()[1]  # type: ignore[misc]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "key": self.key,
            "value": self.value,
            "description": self.description,
            "updated_by": str(self.updated_by) if self.updated_by else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
