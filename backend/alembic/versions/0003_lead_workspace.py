"""lead workspace: extended lead columns, tags/notes/activity/merge/duplicates,
saved views, import batches, lead exports + granular leads.* permissions.

Non-destructive by design (Phase 4 §41):
- only ADDS columns, tables, indexes and permission rows
- never DROPs or RESETs existing data
- legacy lead status values are mapped information-preservingly:
  'active' -> 'NEW', 'archived' -> 'ARCHIVED' (same meaning, new vocabulary)
- legacy `tags` JSON values are back-filled into lead_tags/lead_tag_assignments
- quality_score is back-filled with the deterministic completeness formula

Revision ID: 0003_lead_workspace
Revises: 0002_scraping_engine
Create Date: 2026-09-03
"""

from __future__ import annotations

import json
import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_lead_workspace"
down_revision = "0002_scraping_engine"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

NEW_PERMISSIONS = (
    ("leads.create", "Create leads manually or via API"),
    ("leads.archive", "Archive and restore leads"),
    ("leads.delete", "Hard-delete leads (explicit administrative action)"),
    ("leads.import", "Import leads from CSV/XLSX/JSON/JSONL"),
    ("leads.export", "Export leads"),
    ("leads.merge", "Review and merge duplicate leads"),
    ("leads.manage_tags", "Create/rename/delete tags and assign them"),
    ("leads.manage_views", "Create and share saved views"),
    ("leads.manage_quality", "Run quality/dedup scans and recompute scores"),
)

STATUS_MAP = {"active": "NEW", "archived": "ARCHIVED"}


def _datetime_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _add_lead_columns() -> None:
    columns = [
        sa.Column("first_name", sa.String(150), nullable=True),
        sa.Column("last_name", sa.String(150), nullable=True),
        sa.Column("postal_code", sa.String(20), nullable=True),
        sa.Column("industry", sa.String(150), nullable=True),
        sa.Column("source_type", sa.String(50), nullable=True),
        sa.Column("source_id", sa.String(300), nullable=True),
        sa.Column("imported_file_id", sa.Uuid(), nullable=True),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column("quality_score", sa.Integer(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    ]
    for column in columns:
        op.add_column("leads", column)
    # Phase 4 §26: scrape jobs report updated leads (merged into existing)
    op.add_column(
        "scrape_jobs",
        sa.Column("records_updated", sa.Integer(), nullable=False, server_default="0"),
    )


def _map_status_values(bind) -> None:
    """Information-preserving vocabulary migration (never deletes rows)."""
    for old, new in STATUS_MAP.items():
        bind.execute(
            sa.text("UPDATE leads SET status = :new WHERE status = :old"),
            {"old": old, "new": new},
        )
    # Remaining unknown values (if any operator customization existed) are kept
    # as-is; the service layer treats unknown statuses as custom values.


def _widen_status_default() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.alter_column(
            "status",
            existing_type=sa.String(20),
            type_=sa.String(30),
            existing_nullable=False,
            server_default="NEW",
        )


def _backfill_quality_score(bind) -> None:
    """Deterministic completeness score, computed in SQL (§15):
    business 20 / phone 20 / email 20 / website 15 / address 10 /
    city 5 / state 5 / source provenance 5 = 100."""
    bind.execute(
        sa.text(
            """
            UPDATE leads SET quality_score =
                (CASE WHEN business_name IS NOT NULL AND business_name != '' THEN 20 ELSE 0 END +
                 CASE WHEN phone IS NOT NULL AND phone != '' THEN 20 ELSE 0 END +
                 CASE WHEN email IS NOT NULL AND email != '' THEN 20 ELSE 0 END +
                 CASE WHEN website IS NOT NULL AND website != '' THEN 15 ELSE 0 END +
                 CASE WHEN address IS NOT NULL AND address != '' THEN 10 ELSE 0 END +
                 CASE WHEN city IS NOT NULL AND city != '' THEN 5 ELSE 0 END +
                 CASE WHEN state IS NOT NULL AND state != '' THEN 5 ELSE 0 END +
                 CASE WHEN (source IS NOT NULL AND source != '')
                        OR source_url IS NOT NULL THEN 5 ELSE 0 END)
            """
        )
    )


def _backfill_tags(bind) -> None:
    """Move legacy JSON tag names into the relational tag tables (idempotent)."""
    now = datetime.now(timezone.utc)
    rows = bind.execute(sa.text("SELECT id, tags FROM leads")).fetchall()
    tag_ids: dict[str, str] = {}
    for row in rows:
        lead_id, raw_tags = row[0], row[1]
        if raw_tags is None:
            continue
        if isinstance(raw_tags, str):
            try:
                tags = json.loads(raw_tags)
            except (ValueError, TypeError):
                continue
        else:
            tags = raw_tags
        if not isinstance(tags, list):
            continue
        for name in tags:
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()[:100]
            if name not in tag_ids:
                existing = bind.execute(
                    sa.text("SELECT id FROM lead_tags WHERE name = :name"), {"name": name}
                ).first()
                if existing:
                    tag_ids[name] = existing[0]
                else:
                    # hex32: matches SQLAlchemy Uuid storage on SQLite and is a
                    # valid UUID literal on PostgreSQL (see migration 0002 note).
                    tag_id = uuid_module.uuid4().hex
                    bind.execute(
                        sa.text(
                            "INSERT INTO lead_tags (id, name, is_system, created_at, updated_at) "
                            "VALUES (:id, :name, 1, :ts, :ts)"
                        ),
                        {"id": tag_id, "name": name, "ts": now},
                    )
                    tag_ids[name] = tag_id
            bind.execute(
                sa.text(
                    "INSERT INTO lead_tag_assignments (id, lead_id, tag_id, created_at) "
                    "SELECT :id, :lead_id, :tag_id, :ts WHERE NOT EXISTS ("
                    "  SELECT 1 FROM lead_tag_assignments a "
                    "  WHERE a.lead_id = :lead_id AND a.tag_id = :tag_id)"
                ),
                {"id": uuid_module.uuid4().hex, "lead_id": lead_id, "tag_id": tag_ids[name], "ts": now},
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

    matrix = {
        "leads.create": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
        "leads.archive": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
        "leads.delete": ("SUPER_ADMIN", "ADMIN"),
        "leads.import": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
        "leads.export": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
        "leads.merge": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
        "leads.manage_tags": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
        "leads.manage_views": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR"),
        "leads.manage_quality": ("SUPER_ADMIN", "ADMIN"),
    }
    for code, role_codes in matrix.items():
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
        "lead_tags",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("color", sa.String(20), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_lead_tags_name", "lead_tags", ["name"])

    op.create_table(
        "lead_tag_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "lead_id", sa.Uuid(),
            sa.ForeignKey("leads.id", ondelete="CASCADE", name="fk_lead_tag_assignments_lead_id_leads"),
            nullable=False,
        ),
        sa.Column(
            "tag_id", sa.Uuid(),
            sa.ForeignKey("lead_tags.id", ondelete="CASCADE", name="fk_lead_tag_assignments_tag_id_lead_tags"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("lead_id", "tag_id", name="uq_lead_tag_once"),
    )
    op.create_index("ix_lead_tag_tag", "lead_tag_assignments", ["tag_id"])

    op.create_table(
        "lead_notes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "lead_id", sa.Uuid(),
            sa.ForeignKey("leads.id", ondelete="CASCADE", name="fk_lead_notes_lead_id_leads"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        *_datetime_columns(),
    )
    op.create_index("ix_lead_notes_lead_created", "lead_notes", ["lead_id", "created_at"])

    op.create_table(
        "lead_activities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "lead_id", sa.Uuid(),
            sa.ForeignKey("leads.id", ondelete="CASCADE", name="fk_lead_activities_lead_id_leads"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("metadata_json", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_lead_activities_lead_created", "lead_activities", ["lead_id", "created_at"])

    op.create_table(
        "lead_merge_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("primary_lead_id", sa.Uuid(), nullable=False),
        sa.Column("merged_lead_id", sa.Uuid(), nullable=False),
        sa.Column("before_data", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("after_data", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("conflicts", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_lead_merge_primary", "lead_merge_history", ["primary_lead_id"])

    op.create_table(
        "lead_duplicate_candidates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("lead_a_id", sa.Uuid(), nullable=False),
        sa.Column("lead_b_id", sa.Uuid(), nullable=False),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("matched_on", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("origin", sa.String(20), nullable=True),
        sa.Column("detected_by_job_id", sa.Uuid(), nullable=True),
        sa.Column("resolution_note", sa.String(500), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("lead_a_id", "lead_b_id", name="uq_duplicate_pair"),
    )
    op.create_index("ix_dup_candidates_status_created", "lead_duplicate_candidates", ["status", "created_at"])
    op.create_index("ix_dup_candidates_lead_a", "lead_duplicate_candidates", ["lead_a_id"])
    op.create_index("ix_dup_candidates_lead_b", "lead_duplicate_candidates", ["lead_b_id"])

    op.create_table(
        "saved_views",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("entity", sa.String(50), nullable=False, server_default="leads"),
        sa.Column("filters", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("visibility", sa.String(10), nullable=False, server_default="PRIVATE"),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_saved_views_owner", "saved_views", ["owner_id"])

    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("filename", sa.String(260), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("mapping", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("options", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("imported_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary", JSONVariant, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("error_file_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_import_batches_status_created", "import_batches", ["status", "created_at"])
    op.create_index("ix_import_batches_created_by", "import_batches", ["created_by"])

    op.create_table(
        "lead_exports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False, server_default="filtered"),
        sa.Column("filters", JSONVariant, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("fields", JSONVariant, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        *_datetime_columns(),
    )
    op.create_index("ix_lead_exports_status_created", "lead_exports", ["status", "created_at"])
    op.create_index("ix_lead_exports_created_by", "lead_exports", ["created_by"])


def _create_lead_indexes() -> None:
    for name, columns in (
        ("ix_leads_status_created", ["status", "created_at"]),
        ("ix_leads_source", ["source"]),
        ("ix_leads_source_type", ["source_type"]),
        ("ix_leads_city", ["city"]),
        ("ix_leads_state", ["state"]),
        ("ix_leads_country", ["country"]),
        ("ix_leads_quality_score", ["quality_score"]),
        ("ix_leads_created_at", ["created_at"]),
        ("ix_leads_updated_at", ["updated_at"]),
        ("ix_leads_import_batch", ["import_batch_id"]),
        ("ix_leads_merged_into", ["merged_into_id"]),
    ):
        op.create_index(name, "leads", columns)


def upgrade() -> None:
    bind = op.get_bind()
    _add_lead_columns()
    _map_status_values(bind)
    _widen_status_default()
    _create_tables()
    _create_lead_indexes()
    _backfill_quality_score(bind)
    _backfill_tags(bind)
    _insert_permissions_if_missing(bind)


def downgrade() -> None:
    """Best-effort reverse: drop Phase 4 tables/columns; data columns are
    nullable so removal does not cascade into other data."""
    for name in (
        "ix_leads_status_created", "ix_leads_source", "ix_leads_source_type",
        "ix_leads_city", "ix_leads_state", "ix_leads_country",
        "ix_leads_quality_score", "ix_leads_created_at", "ix_leads_updated_at",
        "ix_leads_import_batch", "ix_leads_merged_into",
    ):
        op.drop_index(name, table_name="leads")
    op.drop_table("lead_exports")
    op.drop_table("import_batches")
    op.drop_index("ix_saved_views_owner", table_name="saved_views")
    op.drop_table("saved_views")
    op.drop_index("ix_dup_candidates_lead_b", table_name="lead_duplicate_candidates")
    op.drop_index("ix_dup_candidates_lead_a", table_name="lead_duplicate_candidates")
    op.drop_index("ix_dup_candidates_status_created", table_name="lead_duplicate_candidates")
    op.drop_table("lead_duplicate_candidates")
    op.drop_index("ix_lead_merge_primary", table_name="lead_merge_history")
    op.drop_table("lead_merge_history")
    op.drop_index("ix_lead_activities_lead_created", table_name="lead_activities")
    op.drop_table("lead_activities")
    op.drop_index("ix_lead_notes_lead_created", table_name="lead_notes")
    op.drop_table("lead_notes")
    op.drop_index("ix_lead_tag_tag", table_name="lead_tag_assignments")
    op.drop_table("lead_tag_assignments")
    op.drop_index("ix_lead_tags_name", table_name="lead_tags")
    op.drop_table("lead_tags")
    with op.batch_alter_table("leads") as batch:
        for column in (
            "last_verified_at", "merged_into_id", "archived_at", "quality_score",
            "import_batch_id", "imported_file_id", "source_id", "source_type",
            "industry", "postal_code", "last_name", "first_name",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("scrape_jobs") as batch:
        batch.drop_column("records_updated")
