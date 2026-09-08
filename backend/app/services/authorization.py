"""Phase 11 — centralized authorization / tenancy service.

Layered ON TOP of the existing RBAC architecture (app/services/rbac.py +
api/deps.require_permission) — it does NOT replace them:

    require_permission(code)        capability check (existing, per-route)
            │
    AuthorizationService            org membership + org status + visibility
            │                       scope + resource-level access + safety guards
            ▼
    visible query / 404

Visibility scopes (ALL / TEAM / ASSIGNED_ONLY / OWNED_ONLY) are resolved in
this order (first match wins):
    1. organization_members.visibility_scope  (per-member override)
    2. organizations.settings_json["visibility_defaults"][ROLE]
    3. shipped role default (ALL — preserves pre-Phase-11 behavior; admins may
       tighten per member or per role at runtime without a migration)
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.enterprise import (
    MemberStatus,
    Organization,
    OrganizationMember,
    Team,
    TeamMember,
    VisibilityScope,
)
from app.models.user import User
from app.services import rbac as rbac_service

DEFAULT_ORG_SLUG = "default"

#: Shipped per-role defaults — deliberately ALL to keep pre-Phase-11 behavior
#: (backward compatibility, Phase 11 §42). Tighten via org settings or member
#: overrides; enforcement is centralized and tested.
ROLE_DEFAULT_VISIBILITY: dict[str, str] = {
    rbac_service.ROLE_SUPER_ADMIN: VisibilityScope.ALL.value,
    rbac_service.ROLE_ADMIN: VisibilityScope.ALL.value,
    rbac_service.ROLE_MANAGER: VisibilityScope.ALL.value,
    rbac_service.ROLE_OPERATOR: VisibilityScope.ALL.value,
    rbac_service.ROLE_VIEWER: VisibilityScope.ALL.value,
}


class MemberContext:
    """Resolved tenancy context for the current request."""

    __slots__ = (
        "user", "membership", "organization", "team_ids", "led_team_ids",
        "permissions", "is_super_admin", "member_role_codes",
    )

    def __init__(
        self,
        user: User,
        membership: OrganizationMember,
        organization: Organization,
        team_ids: set[uuid.UUID],
        led_team_ids: set[uuid.UUID],
        permissions: set[str],
        is_super_admin: bool = False,
        member_role_codes: list[str] | None = None,
    ) -> None:
        self.user = user
        self.membership = membership
        self.organization = organization
        self.team_ids = team_ids
        self.led_team_ids = led_team_ids
        self.permissions = permissions
        self.is_super_admin = is_super_admin
        self.member_role_codes = member_role_codes or []

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def visibility_scope(self) -> str:
        override = self.membership.visibility_scope
        if override:
            return override
        defaults = (self.organization.settings_json or {}).get("visibility_defaults") or {}
        for role in self.member_role_codes:
            if role in defaults:
                return defaults[role]
        for role in self.member_role_codes:
            if role in ROLE_DEFAULT_VISIBILITY:
                return ROLE_DEFAULT_VISIBILITY[role]
        return VisibilityScope.ALL.value


# --- membership resolution -------------------------------------------------------


async def user_role_codes(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Explicit role-code lookup (never touches expired ORM instances)."""
    from app.models.rbac import Role, user_roles

    rows = await session.scalars(
        select(Role.code)
        .join(user_roles, user_roles.c.role_id == Role.id)
        .where(user_roles.c.user_id == user_id)
    )
    return list(rows)


async def user_is_super_admin(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return rbac_service.ROLE_SUPER_ADMIN in await user_role_codes(session, user_id)


async def get_default_organization(session: AsyncSession) -> Organization | None:
    return await session.scalar(
        select(Organization).where(Organization.slug == DEFAULT_ORG_SLUG)
    )


async def ensure_default_organization(session: AsyncSession) -> Organization:
    org = await get_default_organization(session)
    if org is not None:
        return org
    org = Organization(name="Default Organization", slug=DEFAULT_ORG_SLUG)
    session.add(org)
    await session.flush()
    return org


async def get_membership(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> OrganizationMember | None:
    return await session.scalar(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == organization_id,
        )
    )


async def ensure_membership(
    session: AsyncSession, user: User, organization: Organization
) -> OrganizationMember:
    """Idempotently attach a user to an organization (self-heal for legacy users)."""
    membership = await get_membership(session, user.id, organization.id)
    if membership is not None:
        return membership
    is_owner = await user_is_super_admin(session, user.id)
    membership = OrganizationMember(
        organization_id=organization.id, user_id=user.id,
        status=MemberStatus.ACTIVE, is_owner=is_owner,
    )
    session.add(membership)
    await session.flush()
    return membership


async def resolve_context(session: AsyncSession, user: User, permissions: set[str]) -> MemberContext:
    """Resolve the caller's active organization context.

    Requests may target a specific organization via the X-Organization-Id header;
    users without a membership there are rejected (cross-tenant protection).
    Legacy users without any membership are attached to the default organization
    (safe migration strategy, Phase 11 §30).
    """
    org_id = _requested_org_id()
    if org_id is not None:
        organization = await session.get(Organization, org_id)
        if organization is None:
            raise PermissionDeniedError("Unknown organization")
        # §28 cross-tenant protection: a user may target an organization they
        # already belong to — NEVER self-join one via the header
        membership = await get_membership(session, user.id, org_id)
        if membership is None:
            raise PermissionDeniedError("You do not belong to this organization")
    else:
        membership_row = await session.scalar(
            select(OrganizationMember)
            .where(OrganizationMember.user_id == user.id)
            .order_by(OrganizationMember.created_at.asc())
            .limit(1)
        )
        if membership_row is None:
            organization = await ensure_default_organization(session)
        else:
            organization = await session.get(Organization, membership_row.organization_id)
            if organization is None:  # pragma: no cover — broken FK
                organization = await ensure_default_organization(session)

    # legacy users without any membership are attached to their resolved
    # DEFAULT org only (self-heal; never applies to an explicitly requested org)
    membership = await ensure_membership(session, user, organization)
    if membership is None:  # pragma: no cover — ensure_membership never returns None
        raise PermissionDeniedError("You do not belong to this organization")
    if membership.status != MemberStatus.ACTIVE:
        raise PermissionDeniedError("Your organization membership is not active")
    if organization.status != "ACTIVE":
        raise PermissionDeniedError("This organization is not active")

    team_ids = set(
        await session.scalars(
            select(TeamMember.team_id).where(TeamMember.user_id == user.id)
        )
    )
    led_team_ids = set(
        await session.scalars(
            select(TeamMember.team_id).where(
                TeamMember.user_id == user.id, TeamMember.is_lead.is_(True)
            )
        )
    )
    role_codes = await user_role_codes(session, user.id)
    is_super = rbac_service.ROLE_SUPER_ADMIN in role_codes
    return MemberContext(
        user, membership, organization, team_ids, led_team_ids, permissions,
        is_super_admin=is_super, member_role_codes=role_codes,
    )


async def resolve_context_for_organization(
    session: AsyncSession, user: User, organization_id: uuid.UUID, permissions: set[str]
) -> MemberContext:
    """Resolve context for a FIXED organization (API-key principals)."""
    organization = await session.get(Organization, organization_id)
    if organization is None:
        raise PermissionDeniedError("Unknown organization")
    membership = await get_membership(session, user.id, organization.id)
    if membership is None:
        raise PermissionDeniedError("You do not belong to this organization")
    if membership.status != MemberStatus.ACTIVE:
        raise PermissionDeniedError("Your organization membership is not active")
    if organization.status != "ACTIVE":
        raise PermissionDeniedError("This organization is not active")
    team_ids = await team_ids_for_member(session, user.id, organization.id)
    led_rows = await session.scalars(
        select(TeamMember.team_id).where(
            TeamMember.user_id == user.id, TeamMember.is_lead.is_(True)
        )
    )
    led_team_ids = {r for r in led_rows}
    role_codes = await user_role_codes(session, user.id)
    is_super = rbac_service.ROLE_SUPER_ADMIN in role_codes
    return MemberContext(
        user, membership, organization, team_ids, led_team_ids, permissions,
        is_super_admin=is_super, member_role_codes=role_codes,
    )


def _requested_org_id() -> uuid.UUID | None:
    """Read X-Organization-Id from the active request context (import-lazy to
    avoid a hard dependency on FastAPI in this service)."""
    try:
        from fastapi import Request  # noqa: F401

        from app.api.deps import current_request
    except Exception:  # pragma: no cover — non-request callers (worker/CLI/tests)
        return None
    request = current_request.get()
    if request is None:
        return None
    raw = request.headers.get("x-organization-id")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


# --- centralized permission + visibility engine ----------------------------------


async def can(
    session: AsyncSession,
    ctx: MemberContext,
    permission: str,
    resource: object | None = None,
) -> bool:
    """Central check: capability + membership + resource-level access.

    Resource rules (when a resource object is provided):
      - organization_id must match ctx (cross-tenant → False)
      - TEAM scope: resource.team_id ∈ ctx.team_ids OR owner is the caller
      - ASSIGNED_ONLY: resource.assigned_user_id == caller
      - OWNED_ONLY: resource.owner_id/created_by == caller
    """
    if not ctx.has(permission):
        return False
    if resource is None:
        return True
    resource_org = getattr(resource, "organization_id", None)
    if resource_org is not None and resource_org != ctx.organization_id:
        return False
    scope = ctx.visibility_scope()
    if scope == VisibilityScope.ALL.value:
        return True
    if scope == VisibilityScope.OWNED_ONLY.value:
        return _is_owner(resource, ctx.user.id)
    if scope == VisibilityScope.ASSIGNED_ONLY.value:
        return (
            getattr(resource, "assigned_user_id", None) == ctx.user.id
            or _is_owner(resource, ctx.user.id)
        )
    if scope == VisibilityScope.TEAM.value:
        team_id = getattr(resource, "team_id", None) or getattr(resource, "assigned_team_id", None)
        if _is_owner(resource, ctx.user.id):
            return True
        if team_id is not None and team_id in ctx.team_ids:
            return True
        # unassigned/unteam'd resources stay visible to TEAM scope to preserve
        # queue-style workflows; team-scoped private resources are filtered
        return team_id is None
    return False


def _is_owner(resource: object, user_id: uuid.UUID) -> bool:
    for attr in ("owner_id", "created_by"):
        if getattr(resource, attr, None) == user_id:
            return True
    return False


def apply_visibility(query: Select, model: type, ctx: MemberContext) -> Select:
    """Add the visibility WHERE clauses for the caller's scope to a query.

    Pre-Phase-11 rows (organization_id NULL) always remain visible so an
    un-migrated row can never silently disappear from operators' views.
    """
    from sqlalchemy import or_ as _or

    org_col = getattr(model, "organization_id", None)
    if org_col is not None:
        query = query.where(_or(org_col == ctx.organization_id, org_col.is_(None)))

    scope = ctx.visibility_scope()
    if scope == VisibilityScope.ALL.value:
        return query

    owner_col = getattr(model, "owner_id", None)
    if owner_col is None:
        owner_col = getattr(model, "created_by", None)
    assigned_col = getattr(model, "assigned_user_id", None)
    team_col = getattr(model, "team_id", None)
    if team_col is None:
        team_col = getattr(model, "assigned_team_id", None)

    clauses = []
    if owner_col is not None:
        clauses.append(owner_col == ctx.user.id)
    if scope == VisibilityScope.OWNED_ONLY.value:
        return query.where(or_(*clauses)) if clauses else query
    if assigned_col is not None:
        clauses.append(assigned_col == ctx.user.id)
    if scope == VisibilityScope.ASSIGNED_ONLY.value:
        return query.where(or_(*clauses)) if clauses else query
    if scope == VisibilityScope.TEAM.value and team_col is not None and ctx.team_ids:
        clauses.append(team_col.in_(ctx.team_ids))
        # unassigned/unteam'd resources remain visible (queue-style workflow)
        clauses.append(team_col.is_(None))
    return query.where(or_(*clauses)) if clauses else query


def visibility_clause(model: type, ctx: MemberContext):
    """Standalone WHERE clause (org AND scope) for services that build their own
    queries (e.g. lead workspace search). None = no clause needed.

    Pre-Phase-11 rows (organization_id NULL) always remain visible so an
    un-migrated row can never silently disappear from operators' views.
    """
    from sqlalchemy import and_, or_

    org_col = getattr(model, "organization_id", None)
    org_ok = or_(org_col == ctx.organization_id, org_col.is_(None)) if org_col is not None else None

    scope = ctx.visibility_scope()
    if scope == VisibilityScope.ALL.value:
        return org_ok

    owner_col = getattr(model, "owner_id", None)
    if owner_col is None:
        owner_col = getattr(model, "created_by", None)
    assigned_col = getattr(model, "assigned_user_id", None)
    team_col = getattr(model, "team_id", None)
    if team_col is None:
        team_col = getattr(model, "assigned_team_id", None)

    scope_clauses = []
    if owner_col is not None:
        scope_clauses.append(owner_col == ctx.user.id)
    if scope == VisibilityScope.OWNED_ONLY.value:
        if not scope_clauses:
            return org_ok
        scope_ok = or_(*scope_clauses)
        return and_(org_ok, scope_ok) if org_ok is not None else scope_ok
    if assigned_col is not None:
        scope_clauses.append(assigned_col == ctx.user.id)
    if scope == VisibilityScope.ASSIGNED_ONLY.value:
        if not scope_clauses:
            return org_ok
        scope_ok = or_(*scope_clauses)
        return and_(org_ok, scope_ok) if org_ok is not None else scope_ok
    if scope == VisibilityScope.TEAM.value and team_col is not None:
        # unassigned/unteam'd resources remain visible (queue-style workflow)
        team_opts = [team_col.is_(None)]
        if ctx.team_ids:
            team_opts.append(team_col.in_(ctx.team_ids))
        if owner_col is not None:
            team_opts.append(owner_col == ctx.user.id)
        scope_ok = or_(*team_opts)
        return and_(org_ok, scope_ok) if org_ok is not None else scope_ok
    return org_ok


# --- resource accessors (IDOR protection; 404, never leak) -----------------------


def _passes_scope(resource: object, ctx: MemberContext) -> bool:
    """Resource-level visibility under the caller's scope (no permission check)."""
    scope = ctx.visibility_scope()
    if scope == VisibilityScope.ALL.value:
        return True
    if scope == VisibilityScope.OWNED_ONLY.value:
        return _is_owner(resource, ctx.user.id)
    if scope == VisibilityScope.ASSIGNED_ONLY.value:
        return (
            getattr(resource, "assigned_user_id", None) == ctx.user.id
            or _is_owner(resource, ctx.user.id)
        )
    if scope == VisibilityScope.TEAM.value:
        team_id = getattr(resource, "team_id", None) or getattr(
            resource, "assigned_team_id", None
        )
        return team_id is None or team_id in ctx.team_ids or _is_owner(
            resource, ctx.user.id
        )
    return True


async def get_visible_or_404(
    session: AsyncSession,
    model: type,
    resource_id: uuid.UUID,
    ctx: MemberContext,
    *,
    permission: str | None = None,
):
    """Fetch a resource enforcing organization + visibility (raises NotFoundError).

    Uses 404 (not 403) for foreign resources so IDs are never confirmed to
    unauthorized callers — matching the platform's existing security convention.
    """
    resource = await session.get(model, resource_id)
    if resource is None:
        raise NotFoundError("Resource not found")
    resource_org = getattr(resource, "organization_id", None)
    if resource_org is not None and resource_org != ctx.organization_id:
        raise NotFoundError("Resource not found")
    if permission is not None and not ctx.has(permission):
        raise NotFoundError("Resource not found")
    if not _passes_scope(resource, ctx):
        raise NotFoundError("Resource not found")
    return resource


# --- connection access (Phase 11 §16) ---------------------------------------------


async def can_use_connection(
    session: AsyncSession, ctx: MemberContext, connection
) -> bool:
    """Whether the caller may SEND THROUGH a connection.

    ORGANIZATION → any active member; TEAM → same team only;
    RESTRICTED → owner (or SUPER_ADMIN/ADMIN). Pre-Phase-11 rows with a NULL
    organization stay usable (legacy compatibility, like other resources).
    """
    if connection.organization_id is not None and connection.organization_id != ctx.organization_id:
        return False
    scope = (connection.access_scope or "ORGANIZATION").upper()
    if scope == "ORGANIZATION":
        return True
    if ctx.is_super_admin or rbac_service.ROLE_ADMIN in ctx.member_role_codes:
        return True
    if scope == "TEAM":
        return (
            connection.team_id in ctx.team_ids
            or connection.owner_id == ctx.user.id
        )
    if scope == "RESTRICTED":
        return connection.owner_id == ctx.user.id
    return False


# --- enterprise safety guards (Phase 11 §35) ---------------------------------------


async def count_super_admins(session: AsyncSession) -> int:
    from app.models.rbac import Role, user_roles
    from sqlalchemy import func

    return int(
        await session.scalar(
            select(func.count())
            .select_from(user_roles)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(Role.code == rbac_service.ROLE_SUPER_ADMIN)
        )
        or 0
    )


async def assert_not_last_super_admin(
    session: AsyncSession, target_user_id: uuid.UUID
) -> None:
    """Block demoting/deactivating the last remaining SUPER_ADMIN."""
    from app.models.rbac import Role, user_roles

    target_roles = {
        row for row in await session.scalars(
            select(Role.code)
            .join(user_roles, user_roles.c.role_id == Role.id)
            .where(user_roles.c.user_id == target_user_id)
        )
    }
    if rbac_service.ROLE_SUPER_ADMIN not in target_roles:
        return
    total = await count_super_admins(session)
    if total <= 1:
        raise PermissionDeniedError(
            "Cannot remove or demote the last remaining SUPER_ADMIN"
        )


async def team_ids_for_member(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> set[uuid.UUID]:
    rows = await session.execute(
        select(TeamMember.team_id)
        .join(Team, Team.id == TeamMember.team_id)
        .where(TeamMember.user_id == user_id, Team.organization_id == organization_id)
    )
    return {r for r in rows.scalars().all()}
