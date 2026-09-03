"""WhatsApp provider integration: encrypted credential vault, provider event
idempotency, conversations/messages foundation, provider template fields on
campaign_templates, multi-account fields on sending_accounts, Phase 6
permissions.

Non-destructive by design (Phase 6 §39, §43):
- only ADDS tables, columns, indexes and permission rows
- never DROPs, TRUNCATEs or RESETs anything; existing data untouched
- downgrade removes ONLY what this revision created (drop new tables, drop
  added columns where the backend supports it, delete only Phase 6 permissions)

Revision ID: 0005_whatsapp_provider
Revises: 0004_marketing_foundation
Create Date: 2026-09-03
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_whatsapp_provider"
down_revision = "0004_marketing_foundation"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("connections.view", "View messaging connections"),
    ("connections.create", "Create messaging connections"),
    ("connections.edit", "Edit messaging connections"),
    ("connections.delete", "Remove messaging connections"),
    ("connections.validate", "Validate connection credentials with the provider"),
    ("connections.health", "Run connection health checks"),
    ("connections.sync_templates", "Synchronize provider templates"),
    ("campaigns.whatsapp.launch", "Launch WhatsApp campaigns"),
    ("templates.whatsapp.view", "View WhatsApp provider templates"),
    ("templates.whatsapp.manage", "Manage WhatsApp provider templates"),
    ("webhooks.whatsapp.receive", "Receive WhatsApp webhook events (internal ingestion)"),
)

PERMISSION_MATRIX = {
    "connections.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "connections.create": ("SUPER_ADMIN", "ADMIN"),
    "connections.edit": ("SUPER_ADMIN", "ADMIN"),
    "connections.delete": ("SUPER_ADMIN", "ADMIN"),
    "connections.validate": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "connections.health": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "connections.sync_templates": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.whatsapp.launch": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "templates.whatsapp.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "templates.whatsapp.manage": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "webhooks.whatsapp.receive": ("SUPER_ADMIN", "ADMIN"),
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
    for code, role_codes in PERMISSION_MATRIX.items():
        for role_code in role_codes:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id, created_at) "
                    "SELECT r.id, p.id, :ts FROM roles r, permissions p "
                    "WHERE r.code = :role_code AND p.code = :perm_code "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM role_permissions rp "
                    "  WHERE rp.role_id = r.id AND rp.permission_id = p.id)"
                ),
                {"role_code": role_code, "perm_code": code, "ts": now},
            )


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

    # --- sending_accounts: multi-account provider fields (§3) ----------------
    op.add_column("sending_accounts", sa.Column("credential_ref", sa.String(255), nullable=True))
    op.add_column("sending_accounts", sa.Column("phone_number_id", sa.String(100), nullable=True))
    op.add_column("sending_accounts", sa.Column("business_account_id", sa.String(100), nullable=True))

    # --- campaign_templates: provider template fields (§8, §9) ---------------
    # batch_alter_table: plain ALTERs on PostgreSQL; transparent table rebuild
    # on SQLite (which cannot ALTER-add FK/indexed columns)
    with op.batch_alter_table("campaign_templates") as batch:
        batch.add_column(sa.Column("origin", sa.String(20), nullable=False, server_default="LOCAL"))
        batch.add_column(sa.Column("provider_template_id", sa.String(200), nullable=True))
        batch.add_column(sa.Column("provider_status", sa.String(20), nullable=True))
        batch.add_column(sa.Column("category", sa.String(50), nullable=True))
        batch.add_column(sa.Column("components", JSONVariant, nullable=False, server_default="{}"))
        batch.add_column(sa.Column("account_id", sa.Uuid(),
                                   sa.ForeignKey("sending_accounts.id", ondelete="SET NULL"),
                                   nullable=True))
        batch.add_column(sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rejected_reason", sa.String(300), nullable=True))
    op.create_index("ix_campaign_templates_account", "campaign_templates", ["account_id"])
    op.create_index("ix_campaign_templates_provider_status", "campaign_templates", ["provider_status"])

    # --- provider_credentials: encrypted secret vault (§4) --------------------
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("hints", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- provider_events: webhook idempotency layer (§20) ---------------------
    op.create_table(
        "provider_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_event_id", sa.String(300), nullable=False),
        sa.Column("sending_account_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(20), nullable=False, server_default="OTHER"),
        sa.Column("event_type", sa.String(50), nullable=True),
        sa.Column("provider_message_id", sa.String(300), nullable=True),
        sa.Column("normalized", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("raw_metadata", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "provider_event_id", name="uq_provider_event_dedupe"),
        sa.ForeignKeyConstraint(["sending_account_id"], ["sending_accounts.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_provider_events_account_received", "provider_events",
                    ["sending_account_id", "received_at"])
    op.create_index("ix_provider_events_message", "provider_events", ["provider_message_id"])

    # --- conversations + messages: inbound foundation (§22, §23) --------------
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("sending_account_id", sa.Uuid(), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("external_contact_id", sa.String(300), nullable=True),
        sa.Column("contact_phone", sa.String(40), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["sending_account_id"], ["sending_accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_conversations_account_phone", "conversations",
                    ["sending_account_id", "contact_phone"])
    op.create_index("ix_conversations_lead", "conversations", ["lead_id"])
    op.create_index("ix_conversations_last_message", "conversations", ["last_message_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("provider_message_id", sa.String(300), nullable=True),
        sa.Column("message_type", sa.String(30), nullable=False, server_default="TEXT"),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="RECEIVED"),
        sa.Column("metadata", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])
    op.create_index("ix_messages_provider_message", "messages", ["provider_message_id"])

    _insert_permissions_if_missing(bind)


def downgrade() -> None:
    """Reverse only what this revision created (portable: SQLite + PostgreSQL).

    batch_alter_table transparently rebuilds tables on SQLite (needed because
    SQLite cannot DROP COLUMNs that participate in FK/index definitions) and
    issues plain ALTERs on PostgreSQL."""
    bind = op.get_bind()
    _delete_permissions(bind)

    op.drop_index("ix_messages_provider_message", table_name="messages")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_last_message", table_name="conversations")
    op.drop_index("ix_conversations_lead", table_name="conversations")
    op.drop_index("ix_conversations_account_phone", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_provider_events_message", table_name="provider_events")
    op.drop_index("ix_provider_events_account_received", table_name="provider_events")
    op.drop_table("provider_events")
    op.drop_table("provider_credentials")

    op.drop_index("ix_campaign_templates_provider_status", table_name="campaign_templates")
    op.drop_index("ix_campaign_templates_account", table_name="campaign_templates")
    with op.batch_alter_table("campaign_templates") as batch:
        batch.drop_column("rejected_reason")
        batch.drop_column("last_synced_at")
        batch.drop_column("account_id")
        batch.drop_column("components")
        batch.drop_column("category")
        batch.drop_column("provider_status")
        batch.drop_column("provider_template_id")
        batch.drop_column("origin")
    with op.batch_alter_table("sending_accounts") as batch:
        batch.drop_column("business_account_id")
        batch.drop_column("phone_number_id")
        batch.drop_column("credential_ref")
