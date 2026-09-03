"""Email marketing provider integration (Phase 7): email-specific recipient
timestamps, tracking/unsubscribe tables, campaign settings, conversation
email matching, Phase 7 permissions.

Non-destructive by design (Phase 7 §49, §52, §59):
- only ADDS tables, columns, indexes and permission rows
- never DROPs, TRUNCATEs or RESETs anything; existing data untouched
- downgrade removes ONLY what this revision created

Revision ID: 0006_email_provider
Revises: 0005_whatsapp_provider
Create Date: 2026-09-04
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_email_provider"
down_revision = "0005_whatsapp_provider"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("email.connections.view", "View email sender accounts"),
    ("email.connections.create", "Create email sender accounts"),
    ("email.connections.edit", "Edit email sender accounts"),
    ("email.connections.delete", "Remove email sender accounts"),
    ("email.connections.validate", "Validate email sender accounts with the provider"),
    ("email.connections.health", "Run email sender health checks"),
    ("email.templates.view", "View email templates"),
    ("email.templates.manage", "Manage email templates"),
    ("campaigns.email.launch", "Launch email campaigns"),
    ("campaigns.email.analytics", "View email campaign analytics"),
    ("suppression.email.view", "View EMAIL-channel suppressions"),
    ("suppression.email.manage", "Manage EMAIL-channel suppressions"),
)

PERMISSION_MATRIX = {
    "email.connections.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "email.connections.create": ("SUPER_ADMIN", "ADMIN"),
    "email.connections.edit": ("SUPER_ADMIN", "ADMIN"),
    "email.connections.delete": ("SUPER_ADMIN", "ADMIN"),
    "email.connections.validate": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "email.connections.health": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "email.templates.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "email.templates.manage": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.email.launch": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.email.analytics": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "suppression.email.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "suppression.email.manage": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
}


def _insert_permissions_if_missing(bind) -> None:
    now = datetime.now(timezone.utc)
    for code, description in NEW_PERMISSIONS:
        existing = bind.execute(
            sa.text("SELECT id FROM permissions WHERE code = :code"), {"code": code}
        ).first()
        if existing is None:
            bind.execute(
                sa.text(
                    "INSERT INTO permissions (id, code, description, created_at, updated_at) "
                    "VALUES (:id, :code, :description, :ts, :ts)"
                ),
                {
                    "id": uuid_module.uuid4().hex,
                    "code": code,
                    "description": description,
                    "ts": now,
                },
            )
        # role matrix grants (only when the pair does not exist yet)
        for role_code in PERMISSION_MATRIX.get(code, ()):
            bind.execute(sa.text("""
                INSERT INTO role_permissions (role_id, permission_id)
                SELECT r.id, p.id
                FROM roles r, permissions p
                WHERE r.code = :role_code AND p.code = :perm_code
                  AND NOT EXISTS (
                    SELECT 1 FROM role_permissions rp
                    WHERE rp.role_id = r.id AND rp.permission_id = p.id
                  )
            """), {"role_code": role_code, "perm_code": code})


def _delete_permissions(bind) -> None:
    codes = ", ".join(f"'{code}'" for code, _ in NEW_PERMISSIONS)
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code IN (" + codes + "))"
        )
    )
    bind.execute(sa.text("DELETE FROM permissions WHERE code IN (" + codes + ")"))


def upgrade() -> None:
    bind = op.get_bind()

    # --- campaigns: campaign-level settings (§31) -----------------------------
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("campaign_metadata", JSONVariant,
                                   nullable=False, server_default="{}"))

    # --- campaign_recipients: email timestamps + tracking key (§18, §29) ------
    with op.batch_alter_table("campaign_recipients") as batch:
        batch.add_column(sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("complained_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("tracking_key", sa.String(64), nullable=True))
    op.create_index("ix_campaign_recipients_tracking_key", "campaign_recipients",
                    ["tracking_key"], unique=True)

    # --- conversations: inbound-email matching (§32) ---------------------------
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("contact_email", sa.String(320), nullable=True))
    op.create_index("ix_conversations_account_email", "conversations",
                    ["sending_account_id", "contact_email"])

    # --- email_tracking_events: open/click evidence (§29, §30, §50) ------------
    op.create_table(
        "email_tracking_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("message_id", sa.String(300), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["campaign_recipients.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_email_tracking_recipient_type", "email_tracking_events",
                    ["recipient_id", "event_type"])
    op.create_index("ix_email_tracking_message", "email_tracking_events", ["message_id"])
    op.create_index("ix_email_tracking_created", "email_tracking_events", ["created_at"])
    op.create_index("ix_email_tracking_campaign_created", "email_tracking_events",
                    ["campaign_id", "created_at"])

    # --- email_unsubscribe_tokens: hash-at-rest opt-out tokens (§13, §50, §51) --
    op.create_table(
        "email_unsubscribe_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="EMAIL"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_email_unsub_token_hash"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipient_id"], ["campaign_recipients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_email_unsub_created", "email_unsubscribe_tokens", ["created_at"])
    op.create_index("ix_email_unsub_recipient", "email_unsubscribe_tokens", ["recipient_id"])

    _insert_permissions_if_missing(bind)


def downgrade() -> None:
    """Reverse only what this revision created (portable: SQLite + PostgreSQL)."""
    bind = op.get_bind()
    _delete_permissions(bind)

    op.drop_index("ix_email_unsub_recipient", table_name="email_unsubscribe_tokens")
    op.drop_index("ix_email_unsub_created", table_name="email_unsubscribe_tokens")
    op.drop_table("email_unsubscribe_tokens")
    op.drop_index("ix_email_tracking_campaign_created", table_name="email_tracking_events")
    op.drop_index("ix_email_tracking_created", table_name="email_tracking_events")
    op.drop_index("ix_email_tracking_message", table_name="email_tracking_events")
    op.drop_index("ix_email_tracking_recipient_type", table_name="email_tracking_events")
    op.drop_table("email_tracking_events")
    op.drop_index("ix_conversations_account_email", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("contact_email")
    op.drop_index("ix_campaign_recipients_tracking_key", table_name="campaign_recipients")
    with op.batch_alter_table("campaign_recipients") as batch:
        batch.drop_column("tracking_key")
        batch.drop_column("complained_at")
        batch.drop_column("bounced_at")
        batch.drop_column("clicked_at")
        batch.drop_column("opened_at")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("campaign_metadata")
