"""Actor platform foundation — enrichment columns, schedules, entity links.

PHASE 3 foundation per the product-upgrade spec. NON-DESTRUCTIVE:
- adds two nullable/defaulted columns to `leads`
  (enrichment_status server_default 'UNRICHED', confidence nullable)
- creates `scrape_schedules` (recurring actor execution)
- creates `entity_links` (source-graph evidence store)
No existing data is modified or removed.

Revision ID: 0012_actor_platform_foundation
Revises: 0011_login_lockout
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision = "0012_actor_platform_foundation"
down_revision = "0011_login_lockout"
branch_labels = None
depends_on = None

PortableJSON = JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "enrichment_status",
            sa.String(length=20),
            nullable=False,
            server_default="UNRICHED",
        ),
    )
    op.add_column(
        "leads", sa.Column("confidence", sa.Integer(), nullable=True)
    )
    op.create_index("ix_leads_enrichment_status", "leads", ["enrichment_status"])

    op.create_table(
        "scrape_schedules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("input", PortableJSON, nullable=False),
        sa.Column("config", PortableJSON, nullable=False),
        sa.Column("schedule_type", sa.String(length=10), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("daily_time", sa.String(length=5), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_job_id", sa.Uuid(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_scrape_schedules_next_run", "scrape_schedules", ["next_run_at"])
    op.create_index("ix_scrape_schedules_actor", "scrape_schedules", ["actor_id"])
    op.create_index(
        "ix_scrape_schedules_organization", "scrape_schedules", ["organization_id"]
    )

    op.create_table(
        "entity_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "lead_id",
            sa.Uuid(),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "related_lead_id",
            sa.Uuid(),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(length=30), nullable=False),
        sa.Column("matched_by", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("resolved_by", sa.String(length=20), nullable=True),
        sa.Column("source_actor_a", sa.String(length=100), nullable=True),
        sa.Column("source_actor_b", sa.String(length=100), nullable=True),
        sa.Column("detected_by_job_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_entity_links_lead", "entity_links", ["lead_id"])
    op.create_index("ix_entity_links_related", "entity_links", ["related_lead_id"])
    op.create_index("ix_entity_links_organization", "entity_links", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_entity_links_organization", table_name="entity_links")
    op.drop_index("ix_entity_links_related", table_name="entity_links")
    op.drop_index("ix_entity_links_lead", table_name="entity_links")
    op.drop_table("entity_links")
    op.drop_index("ix_scrape_schedules_organization", table_name="scrape_schedules")
    op.drop_index("ix_scrape_schedules_actor", table_name="scrape_schedules")
    op.drop_index("ix_scrape_schedules_next_run", table_name="scrape_schedules")
    op.drop_table("scrape_schedules")
    op.drop_index("ix_leads_enrichment_status", table_name="leads")
    op.drop_column("leads", "confidence")
    op.drop_column("leads", "enrichment_status")
