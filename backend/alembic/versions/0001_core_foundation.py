"""core foundation: users, roles, permissions, user_roles, role_permissions,
files, audit_logs, system_settings, connections.

Non-destructive initial schema (greenfield). Downgrade drops these tables —
permitted only because no pre-existing data exists in this repository
(verified in docs/01-repository-audit.md and tests/test_migrations.py).

Revision ID: 0001_core_foundation
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_core_foundation"
down_revision = None
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")


def _datetime_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_datetime_columns(),
    )
    op.create_index("ix_roles_code", "roles", ["code"], unique=True)

    op.create_table(
        "permissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_permissions_code", "permissions", ["code"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.Uuid(), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Uuid(), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_id", sa.Uuid(), sa.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(260), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("storage_backend", sa.String(50), nullable=False, server_default="local"),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("metadata_json", JSONVariant, nullable=False, server_default=sa.text("'{}'::jsonb" if not is_sqlite else "'{}'")),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_files_category", "files", ["category"])
    op.create_index("ix_files_category_created", "files", ["category", "created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=True),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("metadata_json", JSONVariant, nullable=False, server_default=sa.text("'{}'::jsonb" if not is_sqlite else "'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_actor_action", "audit_logs", ["actor_user_id", "action"])

    op.create_table(
        "system_settings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("value", JSONVariant, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_system_settings_key", "system_settings", ["key"], unique=True)

    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("identifier", sa.String(320), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="PENDING"),
        sa.Column("status_message", sa.String(500), nullable=True),
        sa.Column("capabilities", JSONVariant, nullable=False, server_default=sa.text("'{}'::jsonb" if not is_sqlite else "'{}'")),
        sa.Column("config", JSONVariant, nullable=False, server_default=sa.text("'{}'::jsonb" if not is_sqlite else "'{}'")),
        sa.Column("secret_ref", sa.String(255), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_connections_category", "connections", ["category"])


def downgrade() -> None:
    # Greenfield rollback: drops only tables this migration created.
    op.drop_table("connections")
    op.drop_table("system_settings")
    op.drop_index("ix_audit_logs_actor_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_files_category_created", table_name="files")
    op.drop_index("ix_files_category", table_name="files")
    op.drop_table("files")
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_permissions_code", table_name="permissions")
    op.drop_table("permissions")
    op.drop_index("ix_roles_code", table_name="roles")
    op.drop_table("roles")
