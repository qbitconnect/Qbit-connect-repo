"""marketing engine + email provider: sending accounts, templates, campaigns,
recipients/events, suppression/consent, unsubscribe tokens, tracking events,
provider webhook events, conversations/messages + Phase 7 RBAC permissions.

Non-destructive by design (Phase 7 §52, §59):
- only ADDS tables, indexes and permission rows
- never DROPs, TRUNCATEs or RESETs anything
- safe on SQLite (tests/dev) and PostgreSQL (production)

Revision ID: 0004_marketing_email
Revises: 0003_lead_workspace
Create Date: 2026-09-03
"""

from __future__ import annotations

import uuid as uuid_module

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_marketing_email"
down_revision = "0003_lead_workspace"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("email.connections.view", "View email sending accounts"),
    ("email.connections.create", "Add email sending accounts"),
    ("email.connections.edit", "Modify email sending accounts"),
    ("email.connections.delete", "Remove email sending accounts"),
    ("email.connections.validate", "Run sender validation for email accounts"),
    ("email.connections.health", "Run health checks for email accounts"),
    ("whatsapp.connections.view", "View WhatsApp sending accounts"),
    ("whatsapp.connections.create", "Add WhatsApp sending accounts"),
    ("whatsapp.connections.edit", "Modify WhatsApp sending accounts"),
    ("whatsapp.connections.delete", "Remove WhatsApp sending accounts"),
    ("whatsapp.connections.validate", "Validate WhatsApp accounts"),
    ("whatsapp.connections.health", "Health-check WhatsApp accounts"),
    ("email.templates.view", "View email templates"),
    ("email.templates.manage", "Create/modify email templates"),
    ("whatsapp.templates.view", "View WhatsApp templates"),
    ("whatsapp.templates.manage", "Manage WhatsApp templates and sync"),
    ("campaigns.email.launch", "Launch email campaigns"),
    ("campaigns.whatsapp.launch", "Launch WhatsApp campaigns"),
    ("campaigns.email.analytics", "View email campaign analytics"),
    ("campaigns.whatsapp.analytics", "View WhatsApp campaign analytics"),
    ("suppression.email.view", "View email suppression list"),
    ("suppression.email.manage", "Add/remove email suppressions"),
    ("suppression.whatsapp.view", "View WhatsApp suppression list"),
    ("suppression.whatsapp.manage", "Manage WhatsApp suppressions"),
)

#: role → new permissions granted in this migration (mirrors services/rbac.py)
ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "ADMIN": (
        "email.connections.view", "email.connections.create", "email.connections.edit",
        "email.connections.delete", "email.connections.validate", "email.connections.health",
        "whatsapp.connections.view", "whatsapp.connections.create", "whatsapp.connections.edit",
        "whatsapp.connections.delete", "whatsapp.connections.validate", "whatsapp.connections.health",
        "email.templates.view", "email.templates.manage",
        "whatsapp.templates.view", "whatsapp.templates.manage",
        "campaigns.email.launch", "campaigns.whatsapp.launch",
        "campaigns.email.analytics", "campaigns.whatsapp.analytics",
        "suppression.email.view", "suppression.email.manage",
        "suppression.whatsapp.view", "suppression.whatsapp.manage",
    ),
    "MANAGER": (
        "email.connections.view", "email.connections.validate", "email.connections.health",
        "whatsapp.connections.view",
        "email.templates.view", "email.templates.manage",
        "whatsapp.templates.view",
        "campaigns.email.launch", "campaigns.email.analytics",
        "suppression.email.view", "suppression.email.manage",
    ),
    "VIEWER": (
        "email.connections.view", "whatsapp.connections.view",
        "email.templates.view", "whatsapp.templates.view",
        "campaigns.email.analytics",
        "suppression.email.view",
    ),
}


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _upgrade_schema() -> None:
    # --- secret vault -------------------------------------------------------
    op.create_table(
        "secret_vault",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref", sa.String(200), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
        sa.UniqueConstraint("ref", name="uq_secret_vault_ref"),
    )
    op.create_index("ix_secret_vault_ref", "secret_vault", ["ref"])

    # --- sending accounts ----------------------------------------------------
    op.create_table(
        "sending_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("health_status", sa.String(30), nullable=False, server_default="UNKNOWN"),
        sa.Column("sender_name", sa.String(200), nullable=True),
        sa.Column("sender_email", sa.String(320), nullable=True),
        sa.Column("reply_to", sa.String(320), nullable=True),
        sa.Column("phone_number_id", sa.String(100), nullable=True),
        sa.Column("business_account_id", sa.String(100), nullable=True),
        sa.Column("config", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("capabilities", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("credential_ref", sa.String(200), nullable=True),
        sa.Column("status_message", sa.String(500), nullable=True),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_message", sa.String(500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
    )
    op.create_index("ix_sending_accounts_channel", "sending_accounts", ["channel"])
    op.create_index("ix_sending_accounts_channel_status", "sending_accounts", ["channel", "status"])
    op.create_index("ix_sending_accounts_sender_email", "sending_accounts", ["sender_email"])

    # --- templates ------------------------------------------------------------
    op.create_table(
        "marketing_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="DRAFT"),
        sa.Column("provider_status", sa.String(30), nullable=False, server_default="UNSYNCED"),
        sa.Column("provider_template_id", sa.String(200), nullable=True),
        sa.Column("language", sa.String(20), nullable=False, server_default="en"),
        sa.Column("category", sa.String(50), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("variables", JSONVariant, nullable=False, server_default="[]"),
        sa.Column("metadata_json", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
    )
    op.create_index("ix_marketing_templates_channel", "marketing_templates", ["channel"])
    op.create_index(
        "ix_marketing_templates_channel_status", "marketing_templates", ["channel", "status"]
    )
    op.create_index(
        "ix_marketing_templates_provider_template", "marketing_templates", ["provider_template_id"]
    )

    # --- campaigns ---------------------------------------------------------------
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="DRAFT"),
        sa.Column(
            "template_id", sa.Uuid(),
            sa.ForeignKey("marketing_templates.id", name="fk_campaigns_template_id_marketing_templates"),
            nullable=True,
        ),
        sa.Column(
            "sending_account_id", sa.Uuid(),
            sa.ForeignKey("sending_accounts.id", name="fk_campaigns_sending_account_id_sending_accounts"),
            nullable=True,
        ),
        sa.Column("audience", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("schedule_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_config", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("track_opens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("track_clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("audience_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queued_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bounced_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("complained_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opened_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replied_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unsubscribed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("validation_result", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
    )
    op.create_index("ix_campaigns_channel", "campaigns", ["channel"])
    op.create_index("ix_campaigns_channel_status", "campaigns", ["channel", "status"])
    op.create_index("ix_campaigns_created_at", "campaigns", ["created_at"])
    op.create_index("ix_campaigns_template", "campaigns", ["template_id"])
    op.create_index("ix_campaigns_account", "campaigns", ["sending_account_id"])

    # --- campaign recipients -------------------------------------------------------
    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "campaign_id", sa.Uuid(),
            sa.ForeignKey("campaigns.id", name="fk_recipients_campaign_id_campaigns"),
            nullable=False,
        ),
        sa.Column(
            "lead_id", sa.Uuid(),
            sa.ForeignKey("leads.id", name="fk_recipients_lead_id_leads"),
            nullable=True,
        ),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("address_norm", sa.String(320), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("reason", sa.String(100), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("message_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider_message_id", sa.String(300), nullable=True),
        sa.Column("variables", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("complained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("idempotency_key", name="uq_recipient_idempotency"),
    )
    op.create_index("ix_recipients_campaign_status", "campaign_recipients", ["campaign_id", "status"])
    op.create_index("ix_recipients_campaign_address", "campaign_recipients", ["campaign_id", "address_norm"])
    op.create_index("ix_recipients_provider_message", "campaign_recipients", ["provider_message_id"])
    op.create_index("ix_recipients_next_retry", "campaign_recipients", ["next_retry_at"])
    op.create_index("ix_recipients_lead", "campaign_recipients", ["lead_id"])

    # --- campaign events ---------------------------------------------------------
    op.create_table(
        "campaign_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "campaign_id", sa.Uuid(),
            sa.ForeignKey("campaigns.id", name="fk_events_campaign_id_campaigns"),
            nullable=False,
        ),
        sa.Column(
            "recipient_id", sa.Uuid(),
            sa.ForeignKey("campaign_recipients.id", name="fk_events_recipient_id_campaign_recipients"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("provider_message_id", sa.String(300), nullable=True),
        sa.Column("provider_event_id", sa.String(300), nullable=True),
        sa.Column("payload", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_campaign_events_campaign_created", "campaign_events", ["campaign_id", "created_at"])
    op.create_index("ix_campaign_events_recipient", "campaign_events", ["recipient_id"])
    op.create_index("ix_campaign_events_type", "campaign_events", ["event_type"])
    op.create_index("ix_campaign_events_provider_message", "campaign_events", ["provider_message_id"])

    # --- suppression + consent ------------------------------------------------------
    op.create_table(
        "suppressions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("address_norm", sa.String(320), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("source", sa.String(50), nullable=False, server_default="manual"),
        sa.Column(
            "lead_id", sa.Uuid(),
            sa.ForeignKey("leads.id", name="fk_suppressions_lead_id_leads"),
            nullable=True,
        ),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.Column("metadata_json", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_ts(),
        sa.UniqueConstraint("channel", "address_norm", name="uq_suppression_channel_address"),
    )
    op.create_index("ix_suppressions_channel", "suppressions", ["channel"])
    op.create_index("ix_suppressions_channel_reason", "suppressions", ["channel", "reason"])
    op.create_index("ix_suppressions_lead", "suppressions", ["lead_id"])

    op.create_table(
        "marketing_consents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("address_norm", sa.String(320), nullable=False),
        sa.Column(
            "lead_id", sa.Uuid(),
            sa.ForeignKey("leads.id", name="fk_consents_lead_id_leads"),
            nullable=True,
        ),
        sa.Column("opt_in_status", sa.String(30), nullable=False, server_default="UNKNOWN"),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("evidence", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("notes", sa.String(1000), nullable=True),
        *_ts(),
        sa.UniqueConstraint("channel", "address_norm", name="uq_consent_channel_address"),
    )
    op.create_index("ix_consents_channel", "marketing_consents", ["channel"])
    op.create_index("ix_consents_lead", "marketing_consents", ["lead_id"])

    # --- unsubscribe tokens + tracking --------------------------------------------
    op.create_table(
        "unsubscribe_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="EMAIL"),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("address_norm", sa.String(320), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_ip", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="uq_unsubscribe_token_hash"),
    )
    op.create_index("ix_unsubscribe_tokens_token_hash", "unsubscribe_tokens", ["token_hash"])
    op.create_index("ix_unsubscribe_tokens_created", "unsubscribe_tokens", ["created_at"])

    op.create_table(
        "email_tracking_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "campaign_id", sa.Uuid(),
            sa.ForeignKey("campaigns.id", name="fk_tracking_campaign_id_campaigns"),
            nullable=False,
        ),
        sa.Column(
            "recipient_id", sa.Uuid(),
            sa.ForeignKey("campaign_recipients.id", name="fk_tracking_recipient_id_campaign_recipients"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("url", sa.String(2000), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tracking_events_recipient", "email_tracking_events", ["recipient_id", "event_type"])
    op.create_index("ix_tracking_events_campaign", "email_tracking_events", ["campaign_id", "event_type"])
    op.create_index("ix_tracking_events_created", "email_tracking_events", ["created_at"])

    # --- provider webhook events ------------------------------------------------------
    op.create_table(
        "provider_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_event_id", sa.String(300), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="RECEIVED"),
        sa.Column("payload", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("channel", "provider", "provider_event_id", name="uq_provider_event_once"),
    )
    op.create_index("ix_provider_events_status", "provider_events", ["status"])
    op.create_index("ix_provider_events_received", "provider_events", ["received_at"])

    # --- inbox foundation ---------------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column(
            "sending_account_id", sa.Uuid(),
            sa.ForeignKey("sending_accounts.id", name="fk_conversations_account_id_sending_accounts"),
            nullable=True,
        ),
        sa.Column(
            "lead_id", sa.Uuid(),
            sa.ForeignKey("leads.id", name="fk_conversations_lead_id_leads"),
            nullable=True,
        ),
        sa.Column("external_address", sa.String(320), nullable=False),
        sa.Column("external_address_norm", sa.String(320), nullable=False),
        sa.Column("external_contact_id", sa.String(300), nullable=True),
        sa.Column("external_contact_name", sa.String(300), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint(
            "channel", "sending_account_id", "external_address_norm",
            name="uq_conversation_channel_contact",
        ),
    )
    op.create_index("ix_conversations_channel", "conversations", ["channel"])
    op.create_index("ix_conversations_last_message", "conversations", ["last_message_at"])
    op.create_index("ix_conversations_lead", "conversations", ["lead_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id", sa.Uuid(),
            sa.ForeignKey("conversations.id", name="fk_messages_conversation_id_conversations"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("provider_message_id", sa.String(300), nullable=True),
        sa.Column("message_type", sa.String(30), nullable=False, server_default="TEXT"),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("headers", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(30), nullable=False, server_default="RECEIVED"),
        sa.Column("metadata_json", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])
    op.create_index("ix_messages_provider_message", "messages", ["provider_message_id"])


def _seed_permissions(bind) -> None:
    """Add Phase 7 permission rows + role grants (idempotent, additive)."""
    for code, description in NEW_PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (id, code, description, created_at, updated_at) "
                "VALUES (:id, :code, :description, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": uuid_module.uuid4().hex, "code": code, "description": description},
        )
    for role_code, grants in ROLE_GRANTS.items():
        for code in grants:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id, created_at) "
                    "SELECT r.id, p.id, CURRENT_TIMESTAMP FROM roles r, permissions p "
                    "WHERE r.code = :role_code AND p.code = :perm_code"
                ),
                {"role_code": role_code, "perm_code": code},
            )


def upgrade() -> None:
    _upgrade_schema()
    _seed_permissions(op.get_bind())


def downgrade() -> None:
    # Additive-only phase: downgrade removes only Phase 7 structures.
    for table in (
        "messages", "conversations", "provider_events", "email_tracking_events",
        "unsubscribe_tokens", "marketing_consents", "suppressions", "campaign_events",
        "campaign_recipients", "campaigns", "marketing_templates", "sending_accounts",
        "secret_vault",
    ):
        op.drop_table(table)
