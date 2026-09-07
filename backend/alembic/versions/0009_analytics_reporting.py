"""Analytics & reporting engine (Phase 10): saved reports, report runs,
snapshots, daily aggregate tables, aggregation bookkeeping, analytics-friendly
indexes and analytics/reports permissions.

Non-destructive by design (Phase 10 spec §36, §39):
- only ADDS tables, indexes and permission rows
- never DROPs, TRUNCATEs, RESETs or rewrites operational data
- aggregate tables are derived data, rebuildable at any time
- downgrade removes ONLY what this revision created

Revision ID: 0009_analytics_reporting
Revises: 0008_automation_workflows
Create Date: 2026-09-07
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_analytics_reporting"
down_revision = "0008_automation_workflows"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("analytics.view", "View the global analytics dashboard"),
    ("analytics.view_leads", "View lead analytics"),
    ("analytics.view_scraping", "View scraper analytics"),
    ("analytics.view_marketing", "View marketing analytics"),
    ("analytics.view_whatsapp", "View WhatsApp analytics"),
    ("analytics.view_email", "View email analytics"),
    ("analytics.view_inbox", "View inbox/conversation analytics"),
    ("analytics.view_team", "View team performance analytics"),
    ("analytics.view_automation", "View automation analytics"),
    ("analytics.manage", "Rebuild aggregates and run analytics diagnostics"),
    ("reports.view", "View saved reports"),
    ("reports.create", "Create saved reports"),
    ("reports.edit", "Edit reports they own or manage"),
    ("reports.delete", "Delete reports they own or manage"),
    ("reports.run", "Execute saved reports"),
    ("reports.export", "Export report results"),
    ("reports.manage", "Manage all reports regardless of ownership"),
)

PERMISSION_MATRIX = {
    "analytics.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "analytics.view_leads": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "analytics.view_scraping": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "analytics.view_marketing": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "analytics.view_whatsapp": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "analytics.view_email": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "analytics.view_inbox": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "analytics.view_team": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "analytics.view_automation": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "analytics.manage": ("SUPER_ADMIN", "ADMIN"),
    "reports.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "reports.create": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "reports.edit": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "reports.delete": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "reports.run": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "reports.export": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "reports.manage": ("SUPER_ADMIN", "ADMIN"),
}

NEW_INDEXES = (
    # (index name, table, columns) — analytics-friendly indexes that do NOT
    # already exist from migrations 0001-0008 (ix_conversations_status comes
    # from 0007 and ix_opt_out_created covers opt_out_records.created_at, so
    # both are deliberately absent here to keep downgrade ownership clean).
    # Existence is still verified before create/drop for idempotency.
    ("ix_conversations_created_at", "conversations", ["created_at"]),
    ("ix_messages_created_at", "messages", ["created_at"]),
    ("ix_messages_direction_created", "messages", ["direction", "created_at"]),
    ("ix_campaign_recipients_created_at", "campaign_recipients", ["created_at"]),
    ("ix_lead_activities_created_at", "lead_activities", ["created_at"]),
    ("ix_scrape_jobs_created_at", "scrape_jobs", ["created_at"]),
)


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


def _index_exists(bind, name: str, table: str) -> bool:
    if bind.dialect.name == "postgresql":
        row = bind.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :n"), {"n": name}
        ).first()
        return row is not None
    # SQLite: indexes live in sqlite_master
    row = bind.execute(
        sa.text("SELECT 1 FROM sqlite_master WHERE type='index' AND name = :n"), {"n": name}
    ).first()
    return row is not None


def _create_analytics_indexes() -> None:
    bind = op.get_bind()
    for name, table, columns in NEW_INDEXES:
        if _index_exists(bind, name, table):
            continue
        op.create_index(name, table, columns)


def _drop_analytics_indexes() -> None:
    bind = op.get_bind()
    for name, table, _columns in NEW_INDEXES:
        if _index_exists(bind, name, table):
            op.drop_index(name, table_name=table)


def upgrade() -> None:
    bind = op.get_bind()
    _insert_permissions_if_missing(bind)

    # --- reports (§17–§18): persisted, reproducible report configurations ------
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(20), nullable=False),
        sa.Column("config", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("visibility", sa.String(10), nullable=False, server_default="PRIVATE"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_reports_owner_status", "reports", ["owner_id", "status"])
    op.create_index("ix_reports_status_created", "reports", ["status", "created_at"])

    # --- report_runs (§19): background execution bookkeeping --------------------
    op.create_table(
        "report_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("config_snapshot", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("format", sa.String(10), nullable=False, server_default="json"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_report_runs_report_created", "report_runs", ["report_id", "created_at"])
    op.create_index("ix_report_runs_status", "report_runs", ["status"])
    op.create_index("ix_report_runs_requested_by", "report_runs", ["requested_by"])

    # --- report_snapshots (§19): immutable run results ---------------------------
    op.create_table(
        "report_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("export_file_id", sa.Uuid(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["report_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["export_file_id"], ["files.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_report_snapshots_run", "report_snapshots", ["run_id"])
    op.create_index("ix_report_snapshots_report_created", "report_snapshots",
                    ["report_id", "created_at"])

    # --- daily aggregate tables (§20): derived, rebuildable, idempotent ---------
    def _daily_table(table_name: str, dim_columns: list[tuple[str, sa.types.TypeEngine]],
                     uq_name: str) -> None:
        dim_names = [cname for cname, _ctype in dim_columns]
        cols = [
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("day", sa.Date(), nullable=False),
        ]
        cols += [sa.Column(cname, ctype, nullable=False, server_default="")
                 for cname, ctype in dim_columns]
        cols += [
            sa.Column("metrics", JSONVariant, nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        ]
        op.create_table(
            table_name, *cols,
            sa.UniqueConstraint("day", *dim_names, name=uq_name),
        )
        op.create_index(f"ix_{table_name}_day", table_name, ["day"])

    _daily_table("analytics_daily_leads", [("source", sa.String(100))],
                 "uq_analytics_daily_leads_day_source")
    _daily_table("analytics_daily_campaigns", [("channel", sa.String(20))],
                 "uq_analytics_daily_campaigns_day_channel")
    _daily_table("analytics_daily_messages",
                 [("channel", sa.String(20)), ("direction", sa.String(10))],
                 "uq_analytics_daily_messages_key")
    _daily_table("analytics_daily_conversations", [("channel", sa.String(20))],
                 "uq_analytics_daily_conversations_key")
    _daily_table("analytics_daily_scraping",
                 [("actor_id", sa.String(100)), ("actor_version", sa.String(20))],
                 "uq_analytics_daily_scraping_key")
    _daily_table("analytics_daily_automation", [("workflow_id", sa.String(64))],
                 "uq_analytics_daily_automation_key")

    # --- aggregation bookkeeping (§21) --------------------------------------------
    op.create_table(
        "analytics_aggregation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("triggered_by", sa.String(20), nullable=False, server_default="WORKER"),
        sa.Column("status", sa.String(20), nullable=False, server_default="RUNNING"),
        sa.Column("day_start", sa.Date(), nullable=True),
        sa.Column("day_end", sa.Date(), nullable=True),
        sa.Column("rows_upserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_analytics_agg_runs_table_created", "analytics_aggregation_runs",
                    ["table_name", "created_at"])

    # --- analytics-friendly operational indexes (additive) ------------------------
    _create_analytics_indexes()


def downgrade() -> None:
    """Reverse only what this revision created (portable: SQLite + PostgreSQL)."""
    bind = op.get_bind()
    _delete_permissions(bind)

    _drop_analytics_indexes()

    op.drop_index("ix_analytics_agg_runs_table_created", table_name="analytics_aggregation_runs")
    op.drop_table("analytics_aggregation_runs")
    op.drop_index("ix_analytics_daily_automation_day", table_name="analytics_daily_automation")
    op.drop_table("analytics_daily_automation")
    op.drop_index("ix_analytics_daily_scraping_day", table_name="analytics_daily_scraping")
    op.drop_table("analytics_daily_scraping")
    op.drop_index("ix_analytics_daily_conversations_day", table_name="analytics_daily_conversations")
    op.drop_table("analytics_daily_conversations")
    op.drop_index("ix_analytics_daily_messages_day", table_name="analytics_daily_messages")
    op.drop_table("analytics_daily_messages")
    op.drop_index("ix_analytics_daily_campaigns_day", table_name="analytics_daily_campaigns")
    op.drop_table("analytics_daily_campaigns")
    op.drop_index("ix_analytics_daily_leads_day", table_name="analytics_daily_leads")
    op.drop_table("analytics_daily_leads")
    op.drop_index("ix_report_snapshots_report_created", table_name="report_snapshots")
    op.drop_index("ix_report_snapshots_run", table_name="report_snapshots")
    op.drop_table("report_snapshots")
    op.drop_index("ix_report_runs_requested_by", table_name="report_runs")
    op.drop_index("ix_report_runs_status", table_name="report_runs")
    op.drop_index("ix_report_runs_report_created", table_name="report_runs")
    op.drop_table("report_runs")
    op.drop_index("ix_reports_status_created", table_name="reports")
    op.drop_index("ix_reports_owner_status", table_name="reports")
    op.drop_table("reports")
