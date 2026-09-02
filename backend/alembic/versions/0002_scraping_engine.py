"""scraping engine: scrape_jobs, scrape_job_events, scrape_job_checkpoints,
leads + extended scraping permissions.

Non-destructive: only ADDS tables, columns, indexes and permission rows.
Existing Phase 2 data (users, roles, files, ...) is never touched.

Revision ID: 0002_scraping_engine
Revises: 0001_core_foundation
Create Date: 2026-09-02
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_scraping_engine"
down_revision = "0001_core_foundation"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("scraping.pause", "Pause running scrape jobs"),
    ("scraping.cancel", "Cancel scrape jobs"),
    ("scraping.export", "Export scrape job results"),
    ("scraping.manage", "Enable/disable scrapers and manage scraper settings"),
)


def _datetime_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _insert_permissions_if_missing(bind) -> None:
    """Idempotent permission rows + role links (works on SQLite and PostgreSQL).

    Timestamps are bound from Python (NOT NOW()) because SQLite has no NOW().
    """
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
                    # hex32 matches SQLAlchemy Uuid storage on SQLite; PostgreSQL
                    # accepts the 32-digit form too (dashed strings break the
                    # ORM's UPDATE-by-PK on SQLite later).
                    "id": uuid_module.uuid4().hex,
                    "code": code,
                    "description": description,
                    "ts": now,
                },
            )

    # SUPER_ADMIN and ADMIN receive all four; OPERATOR receives pause/cancel/export.
    for code, role_codes in (
        ("scraping.pause", ("SUPER_ADMIN", "ADMIN", "OPERATOR")),
        ("scraping.cancel", ("SUPER_ADMIN", "ADMIN", "OPERATOR")),
        ("scraping.export", ("SUPER_ADMIN", "ADMIN", "OPERATOR")),
        ("scraping.manage", ("SUPER_ADMIN", "ADMIN")),
    ):
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


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "scrape_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("actor_version", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("input", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("config", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(100), nullable=True),
        sa.Column("records_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_duplicate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("resumed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stop_requested", sa.String(10), nullable=False, server_default="NONE"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_scrape_jobs_status_created", "scrape_jobs", ["status", "created_at"])
    op.create_index("ix_scrape_jobs_actor_created", "scrape_jobs", ["actor_id", "created_at"])
    op.create_index("ix_scrape_jobs_created_by", "scrape_jobs", ["created_by"])

    op.create_table(
        "scrape_job_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("scrape_jobs.id", ondelete="CASCADE", name="fk_scrape_job_events_job_id_scrape_jobs"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("message", sa.String(1000), nullable=True),
        sa.Column("metadata_json", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scrape_job_events_job_created", "scrape_job_events", ["job_id", "created_at"])

    op.create_table(
        "scrape_job_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("scrape_jobs.id", ondelete="CASCADE", name="fk_scrape_job_checkpoints_job_id_scrape_jobs"),
            nullable=False,
        ),
        sa.Column("cursor", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("records_processed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scrape_job_checkpoints_job", "scrape_job_checkpoints", ["job_id", "created_at"])

    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("business_name", sa.String(300), nullable=True),
        sa.Column("contact_name", sa.String(300), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("address", sa.String(500), nullable=True),
        sa.Column("city", sa.String(150), nullable=True),
        sa.Column("state", sa.String(150), nullable=True),
        sa.Column("country", sa.String(150), nullable=True),
        sa.Column("category", sa.String(150), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("social_links", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metadata_json", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("tags", JSONVariant, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("email_norm", sa.String(320), nullable=True),
        sa.Column("phone_norm", sa.String(40), nullable=True),
        sa.Column("website_norm", sa.String(300), nullable=True),
        sa.Column("name_key", sa.String(500), nullable=True),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("source_actor_id", sa.String(100), nullable=True),
        sa.Column("source_actor_version", sa.String(20), nullable=True),
        sa.Column("source_job_id", sa.Uuid(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_leads_email_norm", "leads", ["email_norm"])
    op.create_index("ix_leads_phone_norm", "leads", ["phone_norm"])
    op.create_index("ix_leads_website_norm", "leads", ["website_norm"])
    op.create_index("ix_leads_name_key", "leads", ["name_key"])
    op.create_index("ix_leads_source_job", "leads", ["source_job_id"])
    op.create_index("ix_leads_source_actor", "leads", ["source_actor_id"])
    op.create_index("ix_leads_business_name", "leads", ["business_name"])

    _insert_permissions_if_missing(bind)


def downgrade() -> None:
    # Non-destructive policy: downgrade drops only Phase 3 scraping tables.
    op.drop_index("ix_leads_business_name", table_name="leads")
    op.drop_index("ix_leads_source_actor", table_name="leads")
    op.drop_index("ix_leads_source_job", table_name="leads")
    op.drop_index("ix_leads_name_key", table_name="leads")
    op.drop_index("ix_leads_website_norm", table_name="leads")
    op.drop_index("ix_leads_phone_norm", table_name="leads")
    op.drop_index("ix_leads_email_norm", table_name="leads")
    op.drop_table("leads")
    op.drop_index("ix_scrape_job_checkpoints_job", table_name="scrape_job_checkpoints")
    op.drop_table("scrape_job_checkpoints")
    op.drop_index("ix_scrape_job_events_job_created", table_name="scrape_job_events")
    op.drop_table("scrape_job_events")
    op.drop_index("ix_scrape_jobs_created_by", table_name="scrape_jobs")
    op.drop_index("ix_scrape_jobs_actor_created", table_name="scrape_jobs")
    op.drop_index("ix_scrape_jobs_status_created", table_name="scrape_jobs")
    op.drop_table("scrape_jobs")
