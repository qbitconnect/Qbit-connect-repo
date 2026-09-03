"""marketing foundation: campaigns, recipients, events, templates,
sending accounts, suppression, opt-outs, campaign queue + granular
campaigns/templates/sending_accounts/suppression permissions.

Non-destructive by design (Phase 5 §41, §47):
- only ADDS tables, indexes and permission rows
- never DROPs, TRUNCATEs or RESETs anything; existing lead data untouched
- downgrade removes ONLY the tables this revision created

Revision ID: 0004_marketing_foundation
Revises: 0003_lead_workspace
Create Date: 2026-09-03
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_marketing_foundation"
down_revision = "0003_lead_workspace"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("campaigns.view", "View campaigns"),
    ("campaigns.create", "Create campaigns"),
    ("campaigns.edit", "Edit campaigns"),
    ("campaigns.validate", "Run campaign validation"),
    ("campaigns.launch", "Launch campaigns"),
    ("campaigns.pause", "Pause campaigns"),
    ("campaigns.resume", "Resume paused campaigns"),
    ("campaigns.cancel", "Cancel campaigns"),
    ("campaigns.export", "Export campaign recipients/analytics"),
    ("campaigns.analytics", "View campaign analytics"),
    ("templates.view", "View marketing templates"),
    ("templates.create", "Create marketing templates"),
    ("templates.edit", "Edit marketing templates"),
    ("templates.delete", "Archive/delete marketing templates"),
    ("sending_accounts.view", "View sending accounts"),
    ("sending_accounts.manage", "Create/modify sending accounts"),
    ("suppression.view", "View suppression list and opt-outs"),
    ("suppression.manage", "Add/remove suppression entries"),
)

PERMISSION_MATRIX = {
    "campaigns.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "campaigns.create": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.edit": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.validate": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.launch": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.pause": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.resume": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.cancel": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "campaigns.export": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "campaigns.analytics": ("SUPER_ADMIN", "ADMIN", "MANAGER", "VIEWER"),
    "templates.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "templates.create": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "templates.edit": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "templates.delete": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "sending_accounts.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "sending_accounts.manage": ("SUPER_ADMIN", "ADMIN"),
    "suppression.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "suppression.manage": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
}


def _json_column(name: str, nullable: bool, default) -> sa.Column:
    column = sa.Column(name, JSONVariant, nullable=nullable)
    return column


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    ]


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


def _create_tables() -> None:
    op.create_table(
        "campaign_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(300), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("language", sa.String(20), nullable=False, server_default="en"),
        _json_column("variables", False, list),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_campaign_templates_channel_status", "campaign_templates",
        ["channel", "status"],
    )

    op.create_table(
        "sending_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("identifier", sa.String(300), nullable=False),
        sa.Column("display_identifier", sa.String(300), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        _json_column("capabilities", False, dict),
        _json_column("config_metadata", False, dict),
        sa.Column("health_status", sa.String(20), nullable=False, server_default="UNKNOWN"),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        _json_column("audience_definition", False, dict),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("sending_account_id", sa.Uuid(), nullable=True),
        sa.Column("schedule_type", sa.String(20), nullable=False, server_default="SEND_NOW"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        _json_column("validation_report", False, dict),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["template_id"], ["campaign_templates.id"]),
        sa.ForeignKeyConstraint(["sending_account_id"], ["sending_accounts.id"]),
    )
    op.create_index("ix_campaigns_status_created", "campaigns", ["status", "created_at"])
    op.create_index("ix_campaigns_channel", "campaigns", ["channel"])
    op.create_index("ix_campaigns_scheduled_at", "campaigns", ["scheduled_at"])

    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_address", sa.String(320), nullable=False),
        sa.Column("recipient_name", sa.String(300), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("eligibility_status", sa.String(20), nullable=True),
        sa.Column("skip_reason", sa.String(100), nullable=True),
        sa.Column("provider_message_id", sa.String(300), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("campaign_id", "lead_id", name="uq_campaign_recipient_lead"),
    )
    op.create_index(
        "ix_campaign_recipients_campaign_status", "campaign_recipients",
        ["campaign_id", "status"],
    )
    op.create_index("ix_campaign_recipients_lead", "campaign_recipients", ["lead_id"])

    op.create_table(
        "campaign_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("provider_event_id", sa.String(300), nullable=True),
        _json_column("payload_metadata", False, dict),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["campaign_recipients.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_campaign_events_campaign_created", "campaign_events",
                    ["campaign_id", "created_at"])
    op.create_index("ix_campaign_events_recipient", "campaign_events", ["recipient_id"])
    op.create_index("ix_campaign_events_type_created", "campaign_events",
                    ["event_type", "created_at"])

    op.create_table(
        "suppression_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("channel", sa.String(20), nullable=True),
        sa.Column("channel_key", sa.String(20), nullable=False, server_default=""),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("source", sa.String(200), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("type", "address", "channel_key", name="uq_suppression_entry"),
    )
    op.create_index("ix_suppression_address", "suppression_entries", ["address"])
    op.create_index("ix_suppression_channel", "suppression_entries", ["channel_key"])

    op.create_table(
        "opt_out_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("channel_key", sa.String(20), nullable=False, server_default=""),
        sa.Column("reason", sa.String(40), nullable=False, server_default="UNSUBSCRIBED"),
        sa.Column("source", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("channel_key", "address", name="uq_opt_out_address_channel"),
    )
    op.create_index("ix_opt_out_created", "opt_out_records", ["created_at"])

    op.create_table(
        "campaign_queue",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("sending_account_id", sa.Uuid(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("status", sa.String(20), nullable=False, server_default="WAITING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["campaign_recipients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sending_account_id"], ["sending_accounts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("campaign_id", "recipient_id", "message_version",
                            name="uq_queue_idempotency"),
    )
    op.create_index("ix_campaign_queue_status_available", "campaign_queue",
                    ["status", "available_at"])
    op.create_index("ix_campaign_queue_campaign", "campaign_queue", ["campaign_id"])
    op.create_index("ix_campaign_queue_account_status", "campaign_queue",
                    ["sending_account_id", "status"])


def upgrade() -> None:
    bind = op.get_bind()
    _create_tables()
    _insert_permissions_if_missing(bind)


def downgrade() -> None:
    """Reverse only what this revision created. Marketing data is Phase 5 data
    and is removed with it; no lead/scrape/core table is touched."""
    for index_name, table in (
        ("ix_campaign_queue_account_status", "campaign_queue"),
        ("ix_campaign_queue_campaign", "campaign_queue"),
        ("ix_campaign_queue_status_available", "campaign_queue"),
        ("ix_opt_out_created", "opt_out_records"),
        ("ix_suppression_channel", "suppression_entries"),
        ("ix_suppression_address", "suppression_entries"),
        ("ix_campaign_events_type_created", "campaign_events"),
        ("ix_campaign_events_recipient", "campaign_events"),
        ("ix_campaign_events_campaign_created", "campaign_events"),
        ("ix_campaign_recipients_lead", "campaign_recipients"),
        ("ix_campaign_recipients_campaign_status", "campaign_recipients"),
        ("ix_campaigns_scheduled_at", "campaigns"),
        ("ix_campaigns_channel", "campaigns"),
        ("ix_campaigns_status_created", "campaigns"),
    ):
        op.drop_index(index_name, table_name=table)
    op.drop_table("campaign_queue")
    op.drop_table("opt_out_records")
    op.drop_table("suppression_entries")
    op.drop_table("campaign_events")
    op.drop_table("campaign_recipients")
    op.drop_table("campaigns")
    op.drop_table("sending_accounts")
    op.drop_index("ix_campaign_templates_channel_status", table_name="campaign_templates")
    op.drop_table("campaign_templates")
