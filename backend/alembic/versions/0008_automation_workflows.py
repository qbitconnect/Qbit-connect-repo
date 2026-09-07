"""Workflow automation engine (Phase 9): workflows, immutable versions,
executions (+ steps), intake events, indexes and automation permissions.

Non-destructive by design (Phase 9 §78):
- only ADDS tables, indexes and permission rows
- never DROPs, TRUNCATEs or RESETs anything; existing data untouched
- downgrade removes ONLY what this revision created

Revision ID: 0008_automation_workflows
Revises: 0007_inbox_conversations
Create Date: 2026-09-07
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_automation_workflows"
down_revision = "0007_inbox_conversations"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("automation.view", "View workflows and templates"),
    ("automation.create", "Create workflows"),
    ("automation.edit", "Edit draft workflows"),
    ("automation.publish", "Validate and publish workflow versions"),
    ("automation.pause", "Pause active workflows"),
    ("automation.resume", "Resume paused workflows"),
    ("automation.execute", "Trigger and cancel workflow executions"),
    ("automation.delete", "Delete draft workflows"),
    ("automation.view_executions", "View workflow execution history"),
)

PERMISSION_MATRIX = {
    "automation.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
    "automation.create": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "automation.edit": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "automation.publish": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "automation.pause": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "automation.resume": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "automation.execute": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
    "automation.delete": ("SUPER_ADMIN", "ADMIN"),
    "automation.view_executions": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
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
    _insert_permissions_if_missing(bind)

    # --- workflows (§2) ---------------------------------------------------------
    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("trigger_type", sa.String(50), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_workflows_status", "workflows", ["status"])
    op.create_index("ix_workflows_trigger_type", "workflows", ["trigger_type"])

    # --- workflow_versions (§3, §60): immutable published definitions -----------
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", JSONVariant, nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_version"),
    )
    op.create_index("ix_workflow_versions_workflow", "workflow_versions", ["workflow_id"])

    # --- workflow_events (§12, §41): intake log with causation ------------------
    op.create_table(
        "workflow_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.String(160), nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("entity_type", sa.String(40), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("payload", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("causation_id", sa.String(160), nullable=True),
        sa.Column("correlation_id", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("event_id", name="uq_workflow_events_event_id"),
    )
    op.create_index("ix_workflow_events_type", "workflow_events", ["event_type"])
    op.create_index("ix_workflow_events_created", "workflow_events", ["created_at"])

    # --- workflow_executions (§32, §59): the execution queue --------------------
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_version_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_event_id", sa.String(160), nullable=False),
        sa.Column("entity_type", sa.String(40), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("current_node_id", sa.String(60), nullable=True),
        sa.Column("context", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_execution_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_class", sa.String(20), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(80), nullable=True),
        sa.Column("causation_id", sa.String(160), nullable=True),
        sa.Column("correlation_id", sa.String(160), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_version_id"], ["workflow_versions.id"],
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("workflow_id", "trigger_event_id",
                            name="uq_workflow_executions_event"),
    )
    op.create_index("ix_workflow_executions_status", "workflow_executions", ["status"])
    op.create_index("ix_workflow_executions_entity", "workflow_executions",
                    ["entity_type", "entity_id"])
    op.create_index("ix_workflow_executions_next_run", "workflow_executions",
                    ["next_execution_at"])
    op.create_index("ix_workflow_executions_created", "workflow_executions", ["created_at"])

    # --- workflow_execution_steps (§33) ------------------------------------------
    op.create_table(
        "workflow_execution_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.String(60), nullable=False),
        sa.Column("node_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_snapshot", JSONVariant, nullable=True),
        sa.Column("output_snapshot", JSONVariant, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_executions.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_workflow_execution_steps_execution", "workflow_execution_steps",
                    ["execution_id", "status"])


def downgrade() -> None:
    """Reverse only what this revision created (portable: SQLite + PostgreSQL)."""
    bind = op.get_bind()
    _delete_permissions(bind)

    op.drop_index("ix_workflow_execution_steps_execution", table_name="workflow_execution_steps")
    op.drop_table("workflow_execution_steps")
    op.drop_index("ix_workflow_executions_created", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_next_run", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_entity", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_status", table_name="workflow_executions")
    op.drop_table("workflow_executions")
    op.drop_index("ix_workflow_events_created", table_name="workflow_events")
    op.drop_index("ix_workflow_events_type", table_name="workflow_events")
    op.drop_table("workflow_events")
    op.drop_index("ix_workflow_versions_workflow", table_name="workflow_versions")
    op.drop_table("workflow_versions")
    op.drop_index("ix_workflows_trigger_type", table_name="workflows")
    op.drop_index("ix_workflows_status", table_name="workflows")
    op.drop_table("workflows")
