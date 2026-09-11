"""Phase 11 — Team / Admin / Enterprise engine.

NON-DESTRUCTIVE by design (Phase 11 §30, §45):
- only ADDS tables, columns, indexes and permission rows
- backfills a single DEFAULT ORGANIZATION and links every existing user/record
  to it, preserving all current ownership and behavior
- never DROPs, TRUNCATEs, RESETs or deletes anything
- downgrade removes ONLY what this revision created (columns + tables)

Revision ID: 0010_team_admin_enterprise
Revises: 0006_email_provider
Create Date: 2026-09-08
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_team_admin_enterprise"
down_revision = "0009_analytics_reporting"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

#: Deterministic id of the default organization created by the backfill.
DEFAULT_ORG_ID = uuid_module.UUID("00000000-0000-0000-0000-000000000001")

#: Single migration-run timestamp (UTC) used for backfilled rows.
NOW = datetime.now(timezone.utc)

# --- Phase 11 permission catalog additions (additive only) ----------------------

NEW_PERMISSIONS = (
    # NOTE: inbox.view / inbox.reply / inbox.assign were already inserted by
    # 0007_inbox_conversations (with the same role matrix) — not redefined here.
    ("teams.view", "View teams"),
    ("teams.create", "Create teams"),
    ("teams.edit", "Rename/deactivate teams"),
    ("teams.manage_members", "Add/remove team members and leads"),
    ("invitations.view", "View invitations"),
    ("invitations.create", "Invite users"),
    ("invitations.revoke", "Revoke pending invitations"),
    ("leads.assign", "Assign/reassign leads (single and bulk)"),
    ("apikeys.view", "View API keys"),
    ("apikeys.create", "Create API keys"),
    ("apikeys.revoke", "Revoke API keys"),
    ("sessions.view", "View active sessions"),
    ("sessions.revoke", "Revoke sessions"),
    ("security.view", "View security settings"),
    ("security.manage", "Modify security settings"),
    ("notifications.view", "View own in-app notifications"),
)

PERMISSION_MATRIX = {
    "teams.view": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "teams.create": ("SUPER_ADMIN", "ADMIN"),
    "teams.edit": ("SUPER_ADMIN", "ADMIN"),
    "teams.manage_members": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "invitations.view": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "invitations.create": ("SUPER_ADMIN", "ADMIN"),
    "invitations.revoke": ("SUPER_ADMIN", "ADMIN"),
    # inbox.* role mappings already inserted by 0007_inbox_conversations
    "leads.assign": ("SUPER_ADMIN", "ADMIN", "MANAGER"),
    "apikeys.view": ("SUPER_ADMIN", "ADMIN"),
    "apikeys.create": ("SUPER_ADMIN", "ADMIN"),
    "apikeys.revoke": ("SUPER_ADMIN", "ADMIN"),
    "sessions.view": ("SUPER_ADMIN", "ADMIN"),
    "sessions.revoke": ("SUPER_ADMIN", "ADMIN"),
    "security.view": ("SUPER_ADMIN", "ADMIN"),
    "security.manage": ("SUPER_ADMIN", "ADMIN"),
    "notifications.view": ("SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"),
}


def _build_metadata() -> tuple[sa.MetaData, list[sa.Table]]:
    md = sa.MetaData()
    organizations = sa.Table(
        "organizations", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("locale", sa.String(20), nullable=False, server_default="en"),
        sa.Column("settings_json", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    organization_members = sa.Table(
        "organization_members", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("visibility_scope", sa.String(20), nullable=True),
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    teams = sa.Table(
        "teams", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    team_members = sa.Table(
        "team_members", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_lead", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    invitations = sa.Table(
        "invitations", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role_codes", JSONVariant, nullable=False, server_default="[]"),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invited_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    sessions = sa.Table(
        "sessions", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("jti", sa.String(64), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(100), nullable=True),
    )
    api_keys = sa.Table(
        "api_keys", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("prefix", sa.String(40), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("scopes", JSONVariant, nullable=False, server_default="[]"),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    notifications = sa.Table(
        "notifications", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("resource_type", sa.String(100), nullable=True),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    user_preferences = sa.Table(
        "user_preferences", md,
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("locale", sa.String(20), nullable=True),
        sa.Column("preferences_json", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    lead_assignment_history = sa.Table(
        "lead_assignment_history", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("lead_id", sa.Uuid(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("previous_user_id", sa.Uuid(), nullable=True),
        sa.Column("previous_team_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_user_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_team_id", sa.Uuid(), nullable=True),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    conversation_assignment_history = sa.Table(
        "conversation_assignment_history", md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("previous_user_id", sa.Uuid(), nullable=True),
        sa.Column("previous_team_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_user_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_team_id", sa.Uuid(), nullable=True),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    tables = [
        organizations, organization_members, teams, team_members, invitations,
        sessions, api_keys, notifications, user_preferences,
        lead_assignment_history, conversation_assignment_history,
    ]
    return md, tables


_MD, _TABLES = _build_metadata()


def _new_indexes() -> list[tuple[str, str, list[str], bool]]:
    """(index_name, table, columns, unique)"""
    return [
        ("ix_organizations_slug", "organizations", ["slug"], True),
        ("ix_organizations_status", "organizations", ["status"], False),
        ("ix_org_members_org_user", "organization_members", ["organization_id", "user_id"], True),
        ("ix_org_members_user", "organization_members", ["user_id"], False),
        ("ix_org_members_status", "organization_members", ["status"], False),
        ("ix_teams_org", "teams", ["organization_id"], False),
        ("ix_teams_org_name", "teams", ["organization_id", "name"], False),
        ("ix_teams_slug", "teams", ["slug"], False),
        ("ix_team_members_team_user", "team_members", ["team_id", "user_id"], True),
        ("ix_team_members_user", "team_members", ["user_id"], False),
        ("ix_invitations_org", "invitations", ["organization_id"], False),
        ("ix_invitations_org_created", "invitations", ["organization_id", "created_at"], False),
        ("ix_invitations_email", "invitations", ["email"], False),
        ("ix_invitations_token_hash", "invitations", ["token_hash"], True),
        ("ix_sessions_user_active", "sessions", ["user_id", "revoked_at"], False),
        ("ix_sessions_jti", "sessions", ["jti"], True),
        ("ix_sessions_last_seen", "sessions", ["last_seen_at"], False),
        ("ix_api_keys_org", "api_keys", ["organization_id"], False),
        ("ix_api_keys_prefix", "api_keys", ["prefix"], True),
        ("ix_api_keys_key_hash", "api_keys", ["key_hash"], False),
        ("ix_notifications_user_created", "notifications", ["user_id", "created_at"], False),
        ("ix_notifications_user_unread", "notifications", ["user_id", "read_at"], False),
        ("ix_notifications_type", "notifications", ["type"], False),
        ("ix_lead_assign_history_lead", "lead_assignment_history", ["lead_id", "created_at"], False),
        ("ix_lead_assign_history_user", "lead_assignment_history", ["assigned_user_id"], False),
        ("ix_conv_assign_history_conv", "conversation_assignment_history",
         ["conversation_id", "created_at"], False),
    ]


def _create_table(table: sa.Table) -> None:
    """Create one table via alembic op (FKs by name — targets may live outside
    this revision's metadata)."""
    cols = []
    for c in table.columns:
        col = sa.Column(c.name, c.type, nullable=c.nullable, server_default=c.server_default)
        if c.primary_key:
            col.primary_key = True
        for fk in c.foreign_keys:
            col.append_foreign_key(sa.ForeignKey(fk.target_fullname, ondelete=fk.ondelete))
        cols.append(col)
    op.create_table(table.name, *cols)


def upgrade() -> None:
    bind = op.get_bind()

    # --- 1. new tables (FKs by name; safe on PostgreSQL and SQLite) ------------
    for table in _TABLES:
        _create_table(table)

    for name, table, columns, unique in _new_indexes():
        op.create_index(name, table, columns, unique=unique)

    # --- 2. new columns on existing tables (batch mode: SQLite-compatible) ------
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("status", sa.String(20), nullable=True))
        batch.add_column(sa.Column("tokens_revoked_before", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_status", "users", ["status"])

    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("assigned_user_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("assigned_team_id", sa.Uuid(), nullable=True))
    op.create_index("ix_leads_organization", "leads", ["organization_id"])
    op.create_index("ix_leads_assigned_user", "leads", ["assigned_user_id"])
    op.create_index("ix_leads_assigned_team", "leads", ["assigned_team_id"])

    with op.batch_alter_table("scrape_jobs") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index("ix_scrape_jobs_organization", "scrape_jobs", ["organization_id"])

    with op.batch_alter_table("conversations") as batch:
        # NOTE: assigned_user_id / assigned_team_id were already added by
        # 0007_inbox_conversations — only the tenancy column is new here.
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index("ix_conversations_organization", "conversations", ["organization_id"])
    op.create_index("ix_conversations_assigned_team", "conversations", ["assigned_team_id"])

    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("owner_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("team_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("updated_by", sa.Uuid(), nullable=True))
    op.create_index("ix_campaigns_organization", "campaigns", ["organization_id"])
    op.create_index("ix_campaigns_owner", "campaigns", ["owner_id"])
    op.create_index("ix_campaigns_team", "campaigns", ["team_id"])

    with op.batch_alter_table("campaign_templates") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index("ix_campaign_templates_organization", "campaign_templates", ["organization_id"])

    with op.batch_alter_table("sending_accounts") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("created_by", sa.Uuid(), nullable=True))
        # Phase 11 §16 — sender accounts hold provider credentials, so they get
        # the same access-scope model as connections (ORGANIZATION | TEAM | RESTRICTED)
        batch.add_column(sa.Column("owner_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("team_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("access_scope", sa.String(20), nullable=True))
    op.create_index("ix_sending_accounts_organization", "sending_accounts", ["organization_id"])

    with op.batch_alter_table("suppression_entries") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))

    with op.batch_alter_table("connections") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("owner_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("team_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("access_scope", sa.String(20), nullable=True))
        batch.add_column(sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_connections_organization", "connections", ["organization_id"])

    with op.batch_alter_table("files") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index("ix_files_organization", "files", ["organization_id"])

    with op.batch_alter_table("import_batches") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index("ix_import_batches_organization", "import_batches", ["organization_id"])

    with op.batch_alter_table("lead_exports") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index("ix_lead_exports_organization", "lead_exports", ["organization_id"])

    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index("ix_audit_logs_organization", "audit_logs", ["organization_id"])
    op.create_index("ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"])

    # --- 3. SAFE BACKFILL (preserves all existing data/ownership) ---------------
    bind.execute(
        _MD.tables["organizations"].insert().values(
            id=DEFAULT_ORG_ID,
            name="Default Organization",
            slug="default",
            status="ACTIVE",
            timezone="UTC",
            locale="en",
            settings_json={},
            created_at=NOW,
            updated_at=NOW,
        )
    )

    # every existing user joins the default organization; SUPER_ADMINs are owners
    bind.execute(
        sa.text(
            """
            INSERT INTO organization_members
                (id, organization_id, user_id, status, is_owner, created_at, updated_at)
            SELECT :mid, :org_id, u.id, 'ACTIVE',
                   CASE WHEN EXISTS (
                       SELECT 1 FROM user_roles ur
                       JOIN roles r ON r.id = ur.role_id
                       WHERE ur.user_id = u.id AND r.code = 'SUPER_ADMIN'
                   ) THEN TRUE ELSE FALSE END,
                   :ts, :ts
            FROM users u
            WHERE NOT EXISTS (
                SELECT 1 FROM organization_members m WHERE m.user_id = u.id
            )
            """
        ).bindparams(
            sa.bindparam("org_id", DEFAULT_ORG_ID, type_=sa.Uuid()),
            sa.bindparam("mid", uuid_module.uuid4(), type_=sa.Uuid()),
        ).bindparams(ts=NOW)
    )

    # link every existing tenant-scoped record to the default organization
    for table in (
        "leads", "scrape_jobs", "conversations", "campaigns", "campaign_templates",
        "sending_accounts", "suppression_entries", "connections", "files", "audit_logs",
        "import_batches", "lead_exports",
    ):
        bind.execute(
            sa.text(
                f"UPDATE {table} SET organization_id = :org_id WHERE organization_id IS NULL"
            ).bindparams(sa.bindparam("org_id", DEFAULT_ORG_ID, type_=sa.Uuid()))
        )
    # preserve current ownership semantics on campaigns
    bind.execute(sa.text("UPDATE campaigns SET owner_id = created_by WHERE owner_id IS NULL"))
    # existing connections + sender accounts remain usable org-wide
    # (pre-Phase-11 behavior)
    bind.execute(
        sa.text("UPDATE connections SET access_scope = 'ORGANIZATION' WHERE access_scope IS NULL")
    )
    bind.execute(
        sa.text(
            "UPDATE sending_accounts SET access_scope = 'ORGANIZATION' "
            "WHERE access_scope IS NULL"
        )
    )

    # --- 4. new permission rows + matrix entries (additive) ----------------------
    perm_insert = sa.text(
        "INSERT INTO permissions (id, code, description, created_at, updated_at) "
        "SELECT :id, :code, :description, :ts, :ts "
        "WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code = :code)"
    ).bindparams(sa.bindparam("id", type_=sa.Uuid()))
    for code, description in NEW_PERMISSIONS:
        bind.execute(
            perm_insert.bindparams(
                id=uuid_module.uuid4(), code=code, description=description, ts=NOW
            )
        )
    matrix_insert = sa.text(
        "INSERT INTO role_permissions (role_id, permission_id, created_at) "
        "SELECT r.id, p.id, :ts FROM roles r, permissions p "
        "WHERE r.code = :role_code AND p.code = :code "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM role_permissions rp"
        "  WHERE rp.role_id = r.id AND rp.permission_id = p.id)"
    )
    for code, role_codes in PERMISSION_MATRIX.items():
        for role_code in role_codes:
            bind.execute(matrix_insert.bindparams(ts=NOW, role_code=role_code, code=code))


def downgrade() -> None:
    bind = op.get_bind()
    # remove only the permission rows this revision added
    for code, _desc in NEW_PERMISSIONS:
        bind.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_id = "
                "(SELECT id FROM permissions WHERE code = :code)"
            ).bindparams(code=code)
        )
        bind.execute(sa.text("DELETE FROM permissions WHERE code = :code").bindparams(code=code))

    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_organization", table_name="audit_logs")
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_column("organization_id")

    op.drop_index("ix_files_organization", table_name="files")
    with op.batch_alter_table("files") as batch:
        batch.drop_column("organization_id")

    op.drop_index("ix_lead_exports_organization", table_name="lead_exports")
    with op.batch_alter_table("lead_exports") as batch:
        batch.drop_column("organization_id")

    op.drop_index("ix_import_batches_organization", table_name="import_batches")
    with op.batch_alter_table("import_batches") as batch:
        batch.drop_column("organization_id")

    op.drop_index("ix_connections_organization", table_name="connections")
    with op.batch_alter_table("connections") as batch:
        batch.drop_column("last_checked_at")
        batch.drop_column("access_scope")
        batch.drop_column("team_id")
        batch.drop_column("owner_id")
        batch.drop_column("organization_id")

    with op.batch_alter_table("suppression_entries") as batch:
        batch.drop_column("organization_id")

    op.drop_index("ix_sending_accounts_organization", table_name="sending_accounts")
    with op.batch_alter_table("sending_accounts") as batch:
        batch.drop_column("access_scope")
        batch.drop_column("team_id")
        batch.drop_column("owner_id")
        batch.drop_column("created_by")
        batch.drop_column("organization_id")

    op.drop_index("ix_campaign_templates_organization", table_name="campaign_templates")
    with op.batch_alter_table("campaign_templates") as batch:
        batch.drop_column("organization_id")

    op.drop_index("ix_campaigns_team", table_name="campaigns")
    op.drop_index("ix_campaigns_owner", table_name="campaigns")
    op.drop_index("ix_campaigns_organization", table_name="campaigns")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("updated_by")
        batch.drop_column("team_id")
        batch.drop_column("owner_id")
        batch.drop_column("organization_id")

    op.drop_index("ix_conversations_assigned_team", table_name="conversations")
    op.drop_index("ix_conversations_organization", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        # NOTE: assigned_user_id / assigned_team_id belong to 0007_inbox_conversations
        # and must SURVIVE this downgrade — only the tenancy column is removed.
        batch.drop_column("organization_id")

    op.drop_index("ix_scrape_jobs_organization", table_name="scrape_jobs")
    with op.batch_alter_table("scrape_jobs") as batch:
        batch.drop_column("organization_id")

    op.drop_index("ix_leads_assigned_team", table_name="leads")
    op.drop_index("ix_leads_assigned_user", table_name="leads")
    op.drop_index("ix_leads_organization", table_name="leads")
    with op.batch_alter_table("leads") as batch:
        batch.drop_column("assigned_team_id")
        batch.drop_column("assigned_user_id")
        batch.drop_column("organization_id")

    op.drop_index("ix_users_status", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("tokens_revoked_before")
        batch.drop_column("status")

    for name, table, _columns, _unique in reversed(_new_indexes()):
        op.drop_index(name, table_name=table)

    for table in reversed([t.name for t in _TABLES]):
        op.drop_table(table)
