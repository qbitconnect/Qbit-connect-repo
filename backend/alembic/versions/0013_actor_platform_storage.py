"""Actor Platform storage layer — datasets, KV, request queue, tasks,
health history, run webhooks + ScrapeJob outcome columns.

PHASE B/C of the Actor Platform spec. NON-DESTRUCTIVE (spec §37):
- creates 8 NEW tables (actor_tasks, actor_datasets, actor_dataset_items,
  actor_kv_entries, actor_request_queue, actor_health_checks, run_webhooks,
  run_webhook_deliveries)
- adds 4 nullable/defaulted columns to `scrape_jobs`
  (name, task_id, trigger server_default 'MANUAL', outcome)
No existing data is modified or removed.

Revision ID: 0013_actor_platform_storage
Revises: 0012_actor_platform_foundation
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision = "0013_actor_platform_storage"
down_revision = "0012_actor_platform_foundation"
branch_labels = None
depends_on = None

PortableJSON = JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # --- scrape_jobs: run identity + honest outcome --------------------------
    op.add_column("scrape_jobs", sa.Column("name", sa.String(length=200), nullable=True))
    op.add_column("scrape_jobs", sa.Column("task_id", sa.Uuid(), nullable=True))
    op.add_column(
        "scrape_jobs",
        sa.Column(
            "trigger", sa.String(length=12), nullable=False, server_default="MANUAL"
        ),
    )
    op.add_column("scrape_jobs", sa.Column("outcome", sa.String(length=12), nullable=True))

    # --- actor_tasks ---------------------------------------------------------
    op.create_table(
        "actor_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("input", PortableJSON, nullable=False),
        sa.Column("config", PortableJSON, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_job_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_actor_tasks_actor", "actor_tasks", ["actor_id"])
    op.create_index("ix_actor_tasks_organization", "actor_tasks", ["organization_id"])
    op.create_index("ix_actor_tasks_created_by", "actor_tasks", ["created_by"])

    # --- actor_datasets ------------------------------------------------------
    op.create_table(
        "actor_datasets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("scrape_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("actor_version", sa.String(length=20), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="RUNNING"),
        sa.Column("clean_status", sa.String(length=12), nullable=False, server_default="clean"),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("schema_fields", PortableJSON, nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_actor_datasets_job", "actor_datasets", ["job_id"])
    op.create_index("ix_actor_datasets_actor", "actor_datasets", ["actor_id"])
    op.create_index("ix_actor_datasets_created", "actor_datasets", ["created_at"])
    op.create_index("ix_actor_datasets_organization", "actor_datasets", ["organization_id"])

    # --- actor_dataset_items -------------------------------------------------
    op.create_table(
        "actor_dataset_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id", sa.Uuid(),
            sa.ForeignKey("actor_datasets.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("idx", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("data", PortableJSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_actor_dataset_items_dataset_idx", "actor_dataset_items", ["dataset_id", "idx"])
    op.create_index("ix_actor_dataset_items_dataset_created", "actor_dataset_items", ["dataset_id", "created_at"])

    # --- actor_kv_entries ------------------------------------------------------
    op.create_table(
        "actor_kv_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope", sa.String(length=200), nullable=False, server_default="global"),
        sa.Column("key", sa.String(length=300), nullable=False),
        sa.Column("value", PortableJSON, nullable=False),
        sa.Column("content_type", sa.String(length=40), nullable=False, server_default="json"),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_actor_kv_scope", "actor_kv_entries", ["scope"])
    op.create_index("ix_actor_kv_scope_key", "actor_kv_entries", ["scope", "key"], unique=True)

    # --- actor_request_queue -------------------------------------------------
    op.create_table(
        "actor_request_queue",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("queue_name", sa.String(length=200), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("url_norm", sa.String(length=1000), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False, server_default="GET"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="PENDING"),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parent_url", sa.String(length=1000), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_actor_rq_queue_status", "actor_request_queue", ["queue_name", "status"])
    op.create_index("ix_actor_rq_queue_key", "actor_request_queue", ["queue_name", "url_norm"], unique=True)
    op.create_index("ix_actor_rq_job", "actor_request_queue", ["job_id"])

    # --- actor_health_checks -------------------------------------------------
    op.create_table(
        "actor_health_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("dependencies", PortableJSON, nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("check_kind", sa.String(length=20), nullable=False, server_default="registry"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_actor_health_actor_time", "actor_health_checks", ["actor_id", "checked_at"])

    # --- run_webhooks ----------------------------------------------------------
    op.create_table(
        "run_webhooks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("secret", sa.String(length=300), nullable=False),
        sa.Column("events", PortableJSON, nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_webhooks_actor", "run_webhooks", ["actor_id"])
    op.create_index("ix_run_webhooks_organization", "run_webhooks", ["organization_id"])

    # --- run_webhook_deliveries ---------------------------------------------
    op.create_table(
        "run_webhook_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "webhook_id", sa.Uuid(),
            sa.ForeignKey("run_webhooks.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.String(length=100), nullable=True),
        sa.Column("payload", PortableJSON, nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_whd_webhook_time", "run_webhook_deliveries", ["webhook_id", "created_at"])
    op.create_index("ix_run_whd_status_next", "run_webhook_deliveries", ["status", "next_retry_at"])


def downgrade() -> None:
    # Reverse of upgrade: only drops objects THIS migration created.
    op.drop_index("ix_run_whd_status_next", table_name="run_webhook_deliveries")
    op.drop_index("ix_run_whd_webhook_time", table_name="run_webhook_deliveries")
    op.drop_table("run_webhook_deliveries")
    op.drop_index("ix_run_webhooks_organization", table_name="run_webhooks")
    op.drop_index("ix_run_webhooks_actor", table_name="run_webhooks")
    op.drop_table("run_webhooks")
    op.drop_index("ix_actor_health_actor_time", table_name="actor_health_checks")
    op.drop_table("actor_health_checks")
    op.drop_index("ix_actor_rq_job", table_name="actor_request_queue")
    op.drop_index("ix_actor_rq_queue_key", table_name="actor_request_queue")
    op.drop_index("ix_actor_rq_queue_status", table_name="actor_request_queue")
    op.drop_table("actor_request_queue")
    op.drop_index("ix_actor_kv_scope_key", table_name="actor_kv_entries")
    op.drop_index("ix_actor_kv_scope", table_name="actor_kv_entries")
    op.drop_table("actor_kv_entries")
    op.drop_index("ix_actor_dataset_items_dataset_created", table_name="actor_dataset_items")
    op.drop_index("ix_actor_dataset_items_dataset_idx", table_name="actor_dataset_items")
    op.drop_table("actor_dataset_items")
    op.drop_index("ix_actor_datasets_organization", table_name="actor_datasets")
    op.drop_index("ix_actor_datasets_created", table_name="actor_datasets")
    op.drop_index("ix_actor_datasets_actor", table_name="actor_datasets")
    op.drop_index("ix_actor_datasets_job", table_name="actor_datasets")
    op.drop_table("actor_datasets")
    op.drop_index("ix_actor_tasks_created_by", table_name="actor_tasks")
    op.drop_index("ix_actor_tasks_organization", table_name="actor_tasks")
    op.drop_index("ix_actor_tasks_actor", table_name="actor_tasks")
    op.drop_table("actor_tasks")
    op.drop_column("scrape_jobs", "outcome")
    op.drop_column("scrape_jobs", "trigger")
    op.drop_column("scrape_jobs", "task_id")
    op.drop_column("scrape_jobs", "name")
