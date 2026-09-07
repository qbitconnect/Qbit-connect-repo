"""RBAC: permission catalog, role→permission matrix, resolution (Brief §10).

Enforcement happens in backend dependencies (api/deps.py) — never only in the UI.
The matrix below is the seed source of truth (architecture doc 18); changes are
audited when applied via the seed command.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import Permission, Role, role_permissions, user_roles
from app.models.user import User

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_ADMIN = "ADMIN"
ROLE_MANAGER = "MANAGER"
ROLE_OPERATOR = "OPERATOR"
ROLE_VIEWER = "VIEWER"

ROLES: list[dict] = [
    {"code": ROLE_SUPER_ADMIN, "name": "Super Admin", "description": "Full platform control"},
    {"code": ROLE_ADMIN, "name": "Admin", "description": "Platform administration"},
    {"code": ROLE_MANAGER, "name": "Manager", "description": "Campaigns, templates, exports"},
    {"code": ROLE_OPERATOR, "name": "Operator", "description": "Runs scrapers, manages leads/files"},
    {"code": ROLE_VIEWER, "name": "Viewer", "description": "Read-only dashboards and analytics"},
]

PERMISSIONS: list[tuple[str, str]] = [
    ("dashboard.view", "View admin dashboard"),
    ("scraping.view", "View scraping module and jobs"),
    ("scraping.run", "Create and control scrape jobs"),
    ("scraping.pause", "Pause running scrape jobs"),
    ("scraping.cancel", "Cancel scrape jobs"),
    ("scraping.export", "Export scrape job results"),
    ("scraping.manage", "Enable/disable scrapers and manage scraper settings"),
    ("leads.view", "View lead database"),
    ("leads.create", "Create leads manually or via API"),
    ("leads.edit", "Modify leads, tags on leads, notes"),
    ("leads.archive", "Archive and restore leads"),
    ("leads.delete", "Hard-delete leads (explicit administrative action)"),
    ("leads.import", "Import leads from CSV/XLSX/JSON/JSONL"),
    ("leads.export", "Export leads"),
    ("leads.merge", "Review and merge duplicate leads"),
    ("leads.manage_tags", "Create/rename/delete tags"),
    ("leads.manage_views", "Create and share saved views"),
    ("leads.manage_quality", "Run quality/dedup scans and recompute scores"),
    ("marketing.view", "View marketing module"),
    ("campaign.view", "View campaigns (legacy alias of campaigns.view)"),
    ("campaign.create", "Create campaigns (legacy alias of campaigns.create)"),
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
    ("connections.view", "View connection center"),
    ("connections.manage", "Connect/disconnect accounts (legacy alias)"),
    ("connections.create", "Create messaging connections (Phase 6)"),
    ("connections.edit", "Edit messaging connections (Phase 6)"),
    ("connections.delete", "Remove messaging connections (Phase 6)"),
    ("connections.validate", "Validate connection credentials with the provider (Phase 6)"),
    ("connections.health", "Run connection health checks (Phase 6)"),
    ("connections.sync_templates", "Synchronize provider templates (Phase 6)"),
    ("campaigns.whatsapp.launch", "Launch WhatsApp campaigns (Phase 6)"),
    ("templates.whatsapp.view", "View WhatsApp provider templates (Phase 6)"),
    ("templates.whatsapp.manage", "Manage WhatsApp provider templates (Phase 6)"),
    ("webhooks.whatsapp.receive", "Receive WhatsApp webhook events internally (Phase 6)"),
    # --- Phase 7: email marketing provider (§48) ------------------------------
    ("email.connections.view", "View email sender accounts (Phase 7)"),
    ("email.connections.create", "Create email sender accounts (Phase 7)"),
    ("email.connections.edit", "Edit email sender accounts (Phase 7)"),
    ("email.connections.delete", "Remove email sender accounts (Phase 7)"),
    ("email.connections.validate", "Validate email sender accounts with the provider (Phase 7)"),
    ("email.connections.health", "Run email sender health checks (Phase 7)"),
    ("email.templates.view", "View email templates (Phase 7)"),
    ("email.templates.manage", "Manage email templates (Phase 7)"),
    ("campaigns.email.launch", "Launch email campaigns (Phase 7)"),
    ("campaigns.email.analytics", "View email campaign analytics (Phase 7)"),
    ("suppression.email.view", "View EMAIL-channel suppressions (Phase 7)"),
    ("suppression.email.manage", "Manage EMAIL-channel suppressions (Phase 7)"),
    # --- Phase 8: unified inbox (§49) -----------------------------------------
    ("inbox.view", "View the unified inbox (Phase 8)"),
    ("inbox.reply", "Send replies from the inbox (Phase 8)"),
    ("inbox.assign", "Assign/unassign conversations (Phase 8)"),
    ("inbox.manage", "Manage inbox visibility configuration (Phase 8)"),
    ("inbox.add_notes", "Add internal conversation notes (Phase 8)"),
    ("inbox.change_status", "Change conversation status (Phase 8)"),
    ("inbox.change_priority", "Change conversation priority (Phase 8)"),
    ("inbox.link_lead", "Link/unlink conversations to leads (Phase 8)"),
    ("inbox.create_lead", "Create leads from inbox contacts (Phase 8)"),
    ("inbox.whatsapp.reply", "Send WhatsApp replies (Phase 8)"),
    ("inbox.email.reply", "Send email replies (Phase 8)"),
    ("automation.view", "View workflows and templates (Phase 9)"),
    ("automation.create", "Create workflows (Phase 9)"),
    ("automation.edit", "Edit draft workflows (Phase 9)"),
    ("automation.publish", "Validate and publish workflow versions (Phase 9)"),
    ("automation.pause", "Pause active workflows (Phase 9)"),
    ("automation.resume", "Resume paused workflows (Phase 9)"),
    ("automation.execute", "Trigger and cancel workflow executions (Phase 9)"),
    ("automation.delete", "Delete draft workflows (Phase 9)"),
    ("automation.view_executions", "View workflow execution history (Phase 9)"),
    ("exports.view", "View exports and files"),
    ("exports.download", "Download exported files"),
    ("files.view", "List and view file metadata"),
    ("files.create", "Upload files"),
    ("files.delete", "Delete files"),
    ("settings.view", "View system settings"),
    ("settings.manage", "Modify system settings"),
    ("users.view", "View users"),
    ("users.manage", "Create/modify users and roles"),
    ("roles.view", "View roles and permission matrix"),
    ("audit.view", "View audit log"),
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    ROLE_SUPER_ADMIN: [code for code, _ in PERMISSIONS],
    ROLE_ADMIN: [
        "dashboard.view", "scraping.view", "scraping.run",
        "scraping.pause", "scraping.cancel", "scraping.export", "scraping.manage",
        "leads.view", "leads.create", "leads.edit", "leads.archive", "leads.delete",
        "leads.import", "leads.export", "leads.merge", "leads.manage_tags",
        "leads.manage_views", "leads.manage_quality",
        "marketing.view", "campaign.view", "campaign.create",
        "campaigns.view", "campaigns.create", "campaigns.edit",
        "campaigns.validate", "campaigns.launch", "campaigns.pause",
        "campaigns.resume", "campaigns.cancel", "campaigns.export",
        "campaigns.analytics",
        "templates.view", "templates.create", "templates.edit", "templates.delete",
        "sending_accounts.view", "sending_accounts.manage",
        "suppression.view", "suppression.manage",
        "connections.view", "connections.manage",
        "connections.create", "connections.edit", "connections.delete",
        "connections.validate", "connections.health", "connections.sync_templates",
        "campaigns.whatsapp.launch", "templates.whatsapp.view", "templates.whatsapp.manage",
        "webhooks.whatsapp.receive",
        "email.connections.view", "email.connections.create", "email.connections.edit",
        "email.connections.delete", "email.connections.validate", "email.connections.health",
        "email.templates.view", "email.templates.manage",
        "campaigns.email.launch", "campaigns.email.analytics",
        "suppression.email.view", "suppression.email.manage",
        "inbox.view", "inbox.reply", "inbox.assign", "inbox.manage",
        "inbox.add_notes", "inbox.change_status", "inbox.change_priority",
        "inbox.link_lead", "inbox.create_lead",
        "inbox.whatsapp.reply", "inbox.email.reply",
        "automation.view", "automation.create", "automation.edit",
        "automation.publish", "automation.pause", "automation.resume",
        "automation.execute", "automation.delete", "automation.view_executions",
        "exports.view", "exports.download",
        "files.view", "files.create", "files.delete",
        "settings.view", "users.view", "users.manage", "roles.view", "audit.view",
    ],
    ROLE_MANAGER: [
        "dashboard.view", "scraping.view",
        "leads.view", "leads.create", "leads.edit", "leads.archive",
        "leads.import", "leads.export", "leads.merge", "leads.manage_tags",
        "leads.manage_views",
        "marketing.view", "campaign.view", "campaign.create",
        "campaigns.view", "campaigns.create", "campaigns.edit",
        "campaigns.validate", "campaigns.launch", "campaigns.pause",
        "campaigns.resume", "campaigns.cancel", "campaigns.export",
        "campaigns.analytics",
        "templates.view", "templates.create", "templates.edit", "templates.delete",
        "sending_accounts.view",
        "suppression.view", "suppression.manage",
        "connections.view",
        "connections.validate", "connections.health", "connections.sync_templates",
        "campaigns.whatsapp.launch", "templates.whatsapp.view", "templates.whatsapp.manage",
        "email.connections.view", "email.connections.validate", "email.connections.health",
        "email.templates.view", "email.templates.manage",
        "campaigns.email.launch", "campaigns.email.analytics",
        "suppression.email.view", "suppression.email.manage",
        "inbox.view", "inbox.reply", "inbox.assign",
        "inbox.add_notes", "inbox.change_status", "inbox.change_priority",
        "inbox.link_lead", "inbox.create_lead",
        "inbox.whatsapp.reply", "inbox.email.reply",
        "automation.view", "automation.create", "automation.edit",
        "automation.publish", "automation.pause", "automation.resume",
        "automation.execute", "automation.view_executions",
        "exports.view", "exports.download",
        "files.view", "files.create", "files.delete",
    ],
    ROLE_OPERATOR: [
        "dashboard.view", "scraping.view", "scraping.run",
        "scraping.pause", "scraping.cancel", "scraping.export",
        "leads.view", "leads.create", "leads.edit", "leads.archive",
        "leads.import", "leads.export", "leads.merge", "leads.manage_tags",
        "marketing.view", "campaigns.view", "campaigns.export",
        "templates.view", "sending_accounts.view", "suppression.view",
        "connections.view", "connections.health", "templates.whatsapp.view",
        "email.connections.view", "email.connections.health", "email.templates.view",
        "campaigns.email.analytics", "suppression.email.view",
        "inbox.view", "inbox.reply", "inbox.assign",
        "inbox.add_notes", "inbox.change_status", "inbox.change_priority",
        "inbox.link_lead", "inbox.create_lead",
        "inbox.whatsapp.reply", "inbox.email.reply",
        "automation.view", "automation.execute", "automation.view_executions",
        "exports.view", "exports.download",
        "files.view", "files.create", "files.delete",
    ],
    ROLE_VIEWER: [
        "dashboard.view", "scraping.view", "leads.view",
        "marketing.view", "campaign.view", "campaigns.view",
        "campaigns.analytics", "templates.view", "sending_accounts.view",
        "suppression.view",
        "connections.view", "templates.whatsapp.view",
        "email.connections.view", "email.templates.view",
        "campaigns.email.analytics", "suppression.email.view",
        "inbox.view",
        "automation.view", "automation.view_executions",
        "exports.view", "files.view",
        "roles.view",
    ],
}


async def seed_rbac(session: AsyncSession) -> dict:
    """Idempotent seed of roles, permissions and the matrix. Never destructive:
    existing rows are updated in place (description text only)."""
    perm_rows: dict[str, Permission] = {}
    for code, description in PERMISSIONS:
        row = await session.scalar(select(Permission).where(Permission.code == code))
        if row is None:
            row = Permission(code=code, description=description)
            session.add(row)
            await session.flush()
        else:
            row.description = description
        perm_rows[code] = row

    for spec in ROLES:
        role = await session.scalar(select(Role).where(Role.code == spec["code"]))
        if role is None:
            role = Role(code=spec["code"], name=spec["name"], description=spec["description"])
            session.add(role)
            await session.flush()
        else:
            role.name = spec["name"]
            role.description = spec["description"]

        desired = {perm_rows[c].id for c in ROLE_PERMISSIONS.get(spec["code"], [])}
        existing = set(
            await session.scalars(
                select(role_permissions.c.permission_id).where(role_permissions.c.role_id == role.id)
            )
        )
        for pid in desired - existing:
            await session.execute(
                role_permissions.insert().values(role_id=role.id, permission_id=pid)
            )

    await session.commit()
    return {"roles": len(ROLES), "permissions": len(PERMISSIONS)}


async def seed_admin(
    session: AsyncSession, *, email: str, password: str, full_name: str | None
) -> tuple[uuid.UUID | None, bool]:
    """Idempotently ensure one SUPER_ADMIN user. Returns (user_id, created)."""
    from app.core.security import hash_password
    from app.models.user import User

    email_norm = email.strip().lower()
    existing = await session.scalar(select(User).where(User.email == email_norm))
    if existing is not None:
        return existing.id, False

    user = User(email=email_norm, password_hash=hash_password(password), full_name=full_name)
    super_role = await session.scalar(select(Role).where(Role.code == ROLE_SUPER_ADMIN))
    if super_role is not None:
        user.roles.append(super_role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.id, True


async def load_user_permissions(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """Effective permission set for a user (role → permission join)."""
    rows = await session.execute(
        select(Permission.code)
        .join(role_permissions, role_permissions.c.permission_id == Permission.id)
        .join(user_roles, user_roles.c.role_id == role_permissions.c.role_id)
        .where(user_roles.c.user_id == user_id)
    )
    return {r for r in rows.scalars().all()}


async def set_user_roles(session: AsyncSession, user_id: uuid.UUID, role_codes: list[str]) -> list[str]:
    """Replace a user's role assignments atomically (admin action).

    Uses direct association-table operations — no implicit lazy loads, which are
    forbidden in async SQLAlchemy sessions.
    """
    from app.core.errors import NotFoundError

    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")

    role_rows = (
        (await session.execute(select(Role).where(Role.code.in_(role_codes)))).scalars().all()
    )
    found_codes = {r.code for r in role_rows}
    missing = set(role_codes) - found_codes
    if missing:
        raise NotFoundError(f"Unknown role(s): {sorted(missing)}")

    # Replace assignments deterministically (delete + insert).
    await session.execute(user_roles.delete().where(user_roles.c.user_id == user_id))
    for role in role_rows:
        await session.execute(
            user_roles.insert().values(user_id=user_id, role_id=role.id)
        )
    await session.commit()
    return sorted(r.code for r in role_rows)
