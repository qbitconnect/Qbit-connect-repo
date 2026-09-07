"""Unified inbox + conversations (Phase 8): conversation workflow fields,
message delivery-timeline fields, notes / activity / outbox tables, list
indexes and inbox permissions.

Non-destructive by design (Phase 8 §53, §64):
- only ADDS tables, columns, indexes and permission rows
- never DROPs, TRUNCATEs or RESETs anything; existing data untouched
- existing conversation statuses (PENDING/OPEN/CLOSED) remain valid members
  of the extended status vocabulary; unread_count backfills to 0
- downgrade removes ONLY what this revision created

Revision ID: 0007_inbox_conversations
Revises: 0006_email_provider
Create Date: 2026-09-07
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_inbox_conversations"
down_revision = "0006_email_provider"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("inbox.view", "View the unified inbox"),
    ("inbox.reply", "Send replies from the inbox"),
    ("inbox.assign", "Assign/unassign conversations"),
    ("inbox.manage", "Manage inbox visibility and configuration"),
    ("inbox.add_notes", "Add internal conversation notes"),
    ("inbox.change_status", "Change conversation status"),
    ("inbox.change_priority", "Change conversation priority"),
    ("inbox.link_lead", "Link/unlink conversations to leads"),
    ("inbox.create_lead", "Create leads from inbox contacts"),
    ("inbox.whatsapp.reply", "Send WhatsApp replies from the inbox"),
    ("inbox.email.reply", "Send email replies from the inbox"),
)

PERMISSION_MATRIX = {
    "inbox.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "inbox.reply": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "inbox.assign": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "inbox.manage": ("SUPER_ADMIN", "ADMIN"),
    "inbox.add_notes": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "inbox.change_status": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "inbox.change_priority": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "inbox.link_lead": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "inbox.create_lead": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "inbox.whatsapp.reply": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "inbox.email.reply": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
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

    # --- conversations: unified inbox workflow fields (§2) ---------------------
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("subject", sa.String(300), nullable=True))
        batch.add_column(sa.Column("priority", sa.String(20), nullable=False,
                                   server_default="NORMAL"))
        batch.add_column(sa.Column("assigned_user_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("assigned_team_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("unread_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("match_status", sa.String(40), nullable=True))
        batch.create_foreign_key(
            "fk_conversations_assigned_user", "users",
            ["assigned_user_id"], ["id"], ondelete="SET NULL",
        )
    op.create_index("ix_conversations_status", "conversations", ["status"])
    op.create_index("ix_conversations_assigned_user", "conversations", ["assigned_user_id"])
    op.create_index("ix_conversations_priority", "conversations", ["priority"])
    op.create_index("ix_conversations_unread", "conversations", ["unread_count"])
    op.create_index("ix_conversations_channel", "conversations", ["channel"])

    # --- messages: display + delivery-timeline fields (§3, §17) -----------------
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("lead_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("external_message_id", sa.String(300), nullable=True))
        batch.add_column(sa.Column("sender", sa.String(320), nullable=True))
        batch.add_column(sa.Column("recipient", sa.String(320), nullable=True))
        batch.add_column(sa.Column("subject", sa.String(300), nullable=True))
        batch.add_column(sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_messages_lead", "leads", ["lead_id"], ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_messages_external", "messages",
                    ["conversation_id", "external_message_id"])

    # --- conversation_notes: internal team notes (§27) --------------------------
    op.create_table(
        "conversation_notes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_conversation_notes_conversation", "conversation_notes",
                    ["conversation_id", "created_at"])

    # --- conversation_events: activity + assignment history (§29, §34) ----------
    op.create_table(
        "conversation_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("previous_value", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("new_value", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("metadata", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_conversation_events_conversation", "conversation_events",
                    ["conversation_id", "created_at"])
    op.create_index("ix_conversation_events_type", "conversation_events", ["event_type"])

    # --- inbox_outbox: queued reply delivery (§24, §25) --------------------------
    op.create_table(
        "inbox_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("sending_account_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="WAITING"),
        sa.Column("idempotency_key", sa.String(300), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_inbox_outbox_idempotency"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sending_account_id"], ["sending_accounts.id"],
                                ondelete="SET NULL"),
    )
    op.create_index("ix_inbox_outbox_claim", "inbox_outbox", ["status", "available_at"])

    _insert_permissions_if_missing(bind)


def downgrade() -> None:
    """Reverse only what this revision created (portable: SQLite + PostgreSQL)."""
    bind = op.get_bind()
    _delete_permissions(bind)

    op.drop_index("ix_inbox_outbox_claim", table_name="inbox_outbox")
    op.drop_table("inbox_outbox")
    op.drop_index("ix_conversation_events_type", table_name="conversation_events")
    op.drop_index("ix_conversation_events_conversation", table_name="conversation_events")
    op.drop_table("conversation_events")
    op.drop_index("ix_conversation_notes_conversation", table_name="conversation_notes")
    op.drop_table("conversation_notes")
    op.drop_index("ix_messages_external", table_name="messages")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("failed_at")
        batch.drop_column("read_at")
        batch.drop_column("delivered_at")
        batch.drop_column("sent_at")
        batch.drop_column("subject")
        batch.drop_column("recipient")
        batch.drop_column("sender")
        batch.drop_column("external_message_id")
        batch.drop_column("lead_id")
    op.drop_index("ix_conversations_channel", table_name="conversations")
    op.drop_index("ix_conversations_unread", table_name="conversations")
    op.drop_index("ix_conversations_priority", table_name="conversations")
    op.drop_index("ix_conversations_assigned_user", table_name="conversations")
    op.drop_index("ix_conversations_status", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_constraint("fk_conversations_assigned_user", type_="foreignkey")
        batch.drop_column("match_status")
        batch.drop_column("closed_at")
        batch.drop_column("unread_count")
        batch.drop_column("last_outbound_at")
        batch.drop_column("last_inbound_at")
        batch.drop_column("assigned_team_id")
        batch.drop_column("assigned_user_id")
        batch.drop_column("priority")
        batch.drop_column("subject")
