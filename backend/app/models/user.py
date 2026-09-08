"""users table (Brief §9)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, timestamp_columns, uuid_pk
from app.models.enterprise import UserStatus

if TYPE_CHECKING:
    from app.models.rbac import Role


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # --- Phase 11: user lifecycle ------------------------------------------------
    #: INVITED | ACTIVE | SUSPENDED | DEACTIVATED (nullable = pre-Phase-11 row)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    #: access tokens issued BEFORE this instant are invalid (session revocation)
    tokens_revoked_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    roles: Mapped[list["Role"]] = relationship(
        secondary="user_roles", back_populates="users", lazy="selectin"
    )

    @property
    def role_codes(self) -> list[str]:
        return sorted(r.code for r in self.roles)

    @property
    def effective_status(self) -> str:
        """Phase 11 status; pre-Phase-11 rows derive from is_active."""
        if self.status:
            return self.status
        return UserStatus.ACTIVE.value if self.is_active else UserStatus.DEACTIVATED.value

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "email": self.email,
            "full_name": self.full_name,
            "is_active": self.is_active,
            "status": self.effective_status,
            "roles": self.role_codes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }
