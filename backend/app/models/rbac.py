"""RBAC tables: roles, permissions, user_roles, role_permissions (Brief §10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Table, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, timestamp_columns, uuid_pk

if TYPE_CHECKING:
    from app.models.user import User

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column(
        "user_id",
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column(
        "role_id",
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "permission_id",
        Uuid(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    users: Mapped[list["User"]] = relationship(
        secondary=user_roles, back_populates="roles", viewonly=True
    )
    permissions: Mapped[list["Permission"]] = relationship(
        secondary=role_permissions, back_populates="roles", lazy="selectin"
    )

    @property
    def permission_codes(self) -> list[str]:
        return sorted(p.code for p in self.permissions)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    roles: Mapped[list["Role"]] = relationship(
        secondary=role_permissions, back_populates="permissions", viewonly=True
    )
