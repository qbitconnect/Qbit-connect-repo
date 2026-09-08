"""Admin UI — Team / Admin / Enterprise console (Phase 11 §31–§34).

Server-rendered pages reusing the existing dark QBIT visual language and the
same services as the /api/v1 layer. The UI is never the security boundary:
every POST re-checks permissions server-side and the last-admin guard runs in
AuthorizationService (single source of truth).

Pages:
    GET  /admin                        overview (real counts, §20)
    GET  /admin/users                  user list + filters
    GET  /admin/users/{id}             user detail (roles/status/activity)
    POST /admin/users/{id}/roles       set roles (guard rails §35)
    POST /admin/users/{id}/status      activate/suspend/deactivate
    GET  /admin/teams                  team list
    POST /admin/teams                  create team
    GET  /admin/teams/{id}             team detail + members
    POST /admin/teams/{id}/update      rename/activate/deactivate
    POST /admin/teams/{id}/members     add member
    POST /admin/teams/{id}/members/remove
    GET  /admin/roles                  role → permission matrix (read-only)
    GET  /admin/invitations            invitation list
    POST /admin/invitations            create (link shown ONCE)
    POST /admin/invitations/{id}/revoke
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.enterprise import (
    Invitation,
    MemberStatus,
    Organization,
    OrganizationMember,
    Team,
    TeamMember,
    UserSession,
)
from app.models.rbac import Permission, Role, role_permissions, user_roles
from app.models.scrape import Lead
from app.models.user import User
from app.services import authorization as authz
from app.services import notifications as notification_service
from app.services import rbac as rbac_service
from app.ui import (
    UiRedirect,
    _ctx,
    require_ui_permission,
    templates,
)

router = APIRouter(prefix="/admin", tags=["ui-admin"])

INVITE_ACCEPT_PATH = "/invite"


def _perms(request: Request) -> set[str]:
    return getattr(request.state, "ui_permissions", None) or set()


async def _member_ctx(session, request, user):
    """Resolve the Phase 11 member context for UI handlers."""
    perms = _perms(request)
    if not perms:
        perms = await rbac_service.load_user_permissions(session, user.id)
    return await authz.resolve_context(session, user, perms)


def _fmt_dt(value) -> str:
    if not value:
        return "—"
    return str(value)[:16].replace("T", " ")


# --------------------------------------------------------------------- overview
@router.get("", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("users.view"))],
):
    ctx = await _member_ctx(session, request, user)
    org_id = ctx.organization_id

    async def _count(model, *where):
        q = select(func.count()).select_from(model)
        if where:
            q = q.where(*where)
        return int(await session.scalar(q) or 0)

    org = await session.get(Organization, org_id)
    active_users = await _count(User, User.is_active.is_(True))
    members = await _count(
        OrganizationMember,
        OrganizationMember.organization_id == org_id,
        OrganizationMember.status == MemberStatus.ACTIVE.value,
    )
    teams = await _count(Team, Team.organization_id == org_id, Team.is_active.is_(True))
    from app.models.marketing import Campaign
    from app.models.messaging import Conversation

    active_campaigns = await _count(
        Campaign,
        Campaign.organization_id == org_id,
        Campaign.status.in_(["SCHEDULED", "RUNNING", "SENDING", "PROCESSING"]),
    )
    open_conversations = await _count(
        Conversation,
        Conversation.organization_id == org_id,
        Conversation.status.in_(["PENDING", "OPEN", "ACTIVE"]),
    )
    from app.models.scrape import ScrapeJob

    active_jobs = await _count(
        ScrapeJob,
        ScrapeJob.organization_id == org_id,
        ScrapeJob.status.in_(["QUEUED", "RUNNING", "PAUSED"]),
    )
    leads_count = await _count(
        Lead, Lead.organization_id == org_id, Lead.merged_into_id.is_(None)
    )
    from app.models.connection import Connection

    active_connections = await _count(
        Connection,
        Connection.organization_id == org_id,
        Connection.status.in_(["CONNECTED", "PENDING"]),
    )
    pending_invitations = await _count(
        Invitation,
        Invitation.organization_id == org_id,
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
    )
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    from app.models.audit import AuditLog

    recent = (
        (await session.execute(
            select(AuditLog)
            .where(
                or_(AuditLog.organization_id == org_id, AuditLog.organization_id.is_(None)),
                AuditLog.created_at >= week_ago,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(12)
        )).scalars().all()
    )
    # workflow engine (Phase 9) is not part of this repository — honest None
    return templates.TemplateResponse(
        request,
        "admin/overview.html",
        _ctx(
            request, user,
            org=org,
            stats={
                "members": members,
                "active_users": active_users,
                "teams": teams,
                "connections": active_connections,
                "campaigns": active_campaigns,
                "conversations": open_conversations,
                "jobs": active_jobs,
                "leads": leads_count,
                "invitations": pending_invitations,
                "workflows": None,
            },
            recent=recent,
            fmt=_fmt_dt,
            can_manage_users="users.manage" in _perms(request),
        ),
    )


# ------------------------------------------------------------------------ users
@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("users.view"))],
    search: str = Query(default="", max_length=200),
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
):
    query = select(User)
    count_q = select(func.count()).select_from(User)
    if search:
        pattern = f"%{search.lower()}%"
        flt = or_(func.lower(User.email).like(pattern), func.lower(User.full_name).like(pattern))
        query = query.where(flt)
        count_q = count_q.where(flt)
    if status:
        query = query.where(User.status == status.upper())
        count_q = count_q.where(User.status == status.upper())
    total = int(await session.scalar(count_q) or 0)
    rows = (
        (await session.execute(
            query.order_by(User.created_at.asc()).offset((page - 1) * 25).limit(25)
        )).scalars().all()
    )
    # bulk-load role codes per user (no N+1)
    user_ids = [u.id for u in rows]
    role_map: dict = {}
    if user_ids:
        for uid, code in (
            await session.execute(
                select(user_roles.c.user_id, Role.code)
                .join(Role, Role.id == user_roles.c.role_id)
                .where(user_roles.c.user_id.in_(user_ids))
            )
        ):
            role_map.setdefault(uid, []).append(code)
    total_super = await authz.count_super_admins(session)
    can_manage = "users.manage" in _perms(request)
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _ctx(
            request, user,
            users=[
                {
                    "id": u.id,
                    "email": u.email,
                    "full_name": u.full_name,
                    "status": u.effective_status,
                    "is_active": u.is_active,
                    "roles": role_map.get(u.id, []),
                    "is_super": "SUPER_ADMIN" in role_map.get(u.id, []),
                    "created": _fmt_dt(u.created_at),
                }
                for u in rows
            ],
            search=search,
            status=status,
            page=page,
            total=total,
            page_size=25,
            can_manage=can_manage,
            is_super_admin=await authz.user_is_super_admin(session, user.id),
            total_super_admins=total_super,
            ok=request.query_params.get("ok"),
            err=request.query_params.get("err"),
        ),
    )


# ------------------------------------------------------------------ user detail
@router.get("/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail(
    user_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("users.view"))],
):
    target = await session.get(User, user_id)
    if target is None:
        raise UiRedirect("/admin/users")
    role_codes = await authz.user_role_codes(session, target.id)
    all_roles = (await session.execute(select(Role).order_by(Role.code))).scalars().all()
    teams = (
        (await session.execute(
            select(Team).join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == target.id)
        )).scalars().all()
    )
    active_sessions = int(
        await session.scalar(
            select(func.count()).select_from(UserSession).where(
                UserSession.user_id == target.id, UserSession.revoked_at.is_(None)
            )
        )
        or 0
    )
    # scoped leads/campaign counts (org-wide numbers the target can act on)
    ctx = await _member_ctx(session, request, user)
    assigned_leads = int(
        await session.scalar(
            select(func.count()).select_from(Lead).where(Lead.assigned_user_id == target.id)
        )
        or 0
    )
    is_last_super = (
        "SUPER_ADMIN" in role_codes and await authz.count_super_admins(session) <= 1
    )
    return templates.TemplateResponse(
        request,
        "admin/user_detail.html",
        _ctx(
            request, user,
            target=target,
            target_status=target.effective_status,
            role_codes=role_codes,
            all_roles=all_roles,
            teams=teams,
            active_sessions=active_sessions,
            assigned_leads=assigned_leads,
            org=ctx.organization,
            is_self=target.id == user.id,
            is_last_super=is_last_super,
            can_manage="users.manage" in _perms(request),
            total_super_admins=await authz.count_super_admins(session),
            ok=request.query_params.get("ok"),
            err=request.query_params.get("err"),
            fmt=_fmt_dt,
        ),
    )


@router.post("/users/{user_id}/roles")
async def admin_user_set_roles(
    user_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("users.manage"))],
    roles: Annotated[list[str], Form()] = [],
):
    target = await session.get(User, user_id)
    if target is None:
        raise UiRedirect("/admin/users")
    if target.id == user.id:
        return RedirectResponse(url=f"/admin/users/{user_id}?err=You+cannot+change+your+own+roles", status_code=303)
    wanted = [r for r in roles if r in {r2["code"] for r2 in rbac_service.ROLES}]
    if "SUPER_ADMIN" in wanted and "SUPER_ADMIN" not in await authz.user_role_codes(session, target.id):
        return RedirectResponse(url=f"/admin/users/{user_id}?err=SUPER_ADMIN+cannot+be+granted+here", status_code=303)
    if "SUPER_ADMIN" in await authz.user_role_codes(session, target.id) and "SUPER_ADMIN" not in wanted:
        try:
            await authz.assert_not_last_super_admin(session, target.id)
        except Exception:
            return RedirectResponse(url=f"/admin/users/{user_id}?err=Cannot+remove+the+last+SUPER_ADMIN", status_code=303)
    applied = await rbac_service.set_user_roles(session, target.id, wanted)
    await session.commit()
    await request.app.state.audit.log(
        session, action="user.roles_changed", actor_user_id=user.id,
        resource_type="user", resource_id=str(target.id),
        metadata={"roles": applied},
    )
    return RedirectResponse(url=f"/admin/users/{user_id}?ok=Roles+updated", status_code=303)


@router.post("/users/{user_id}/status")
async def admin_user_set_status(
    user_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("users.manage"))],
    status: Annotated[str, Form()],
):
    target = await session.get(User, user_id)
    if target is None:
        raise UiRedirect("/admin/users")
    status = status.upper()
    if target.id == user.id and status in ("SUSPENDED", "DEACTIVATED"):
        return RedirectResponse(url=f"/admin/users/{user_id}?err=You+cannot+disable+your+own+account", status_code=303)
    if status in ("SUSPENDED", "DEACTIVATED"):
        try:
            await authz.assert_not_last_super_admin(session, target.id)
        except Exception:
            return RedirectResponse(url=f"/admin/users/{user_id}?err=Cannot+disable+the+last+SUPER_ADMIN", status_code=303)
    now = datetime.now(timezone.utc)
    if status == "ACTIVE":
        target.is_active = True
        target.status = "ACTIVE"
    elif status in ("SUSPENDED", "DEACTIVATED"):
        target.is_active = False
        target.status = status
        target.tokens_revoked_before = now
        await session.execute(
            UserSession.__table__.update()
            .where(UserSession.user_id == target.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=f"ACCOUNT_{status}")
        )
        await notification_service.emit(
            session, user_id=target.id, organization_id=None,
            type="SECURITY",
            title="Your account status changed",
            body=f"An administrator set your account status to {status}. Sessions were revoked.",
        )
    await session.commit()
    await request.app.state.audit.log(
        session, action=f"user.{status.lower()}ed", actor_user_id=user.id,
        resource_type="user", resource_id=str(target.id), metadata={"status": status},
    )
    return RedirectResponse(url=f"/admin/users/{user_id}?ok=Status+updated", status_code=303)


# ------------------------------------------------------------------------ teams
@router.get("/teams", response_class=HTMLResponse)
async def admin_teams(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("teams.view"))],
):
    ctx = await _member_ctx(session, request, user)
    rows = (
        (await session.execute(
            select(Team).where(Team.organization_id == ctx.organization_id)
            .order_by(Team.name)
        )).scalars().all()
    )
    team_ids = [t.id for t in rows]
    counts: dict = {}
    if team_ids:
        count_rows = await session.execute(
            select(TeamMember.team_id, func.count())
            .where(TeamMember.team_id.in_(team_ids))
            .group_by(TeamMember.team_id)
        )
        counts = {team_id: n for team_id, n in count_rows}
    return templates.TemplateResponse(
        request,
        "admin/teams.html",
        _ctx(
            request, user,
            teams=[
                {
                    "id": t.id,
                    "name": t.name,
                    "slug": t.slug,
                    "description": t.description,
                    "is_active": t.is_active,
                    "members": counts.get(t.id, 0),
                }
                for t in rows
            ],
            can_create="teams.create" in _perms(request),
            can_manage="teams.manage_members" in _perms(request),
            can_edit="teams.edit" in _perms(request),
            ok=request.query_params.get("ok"),
            err=request.query_params.get("err"),
        ),
    )


def _slugify(name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:120] or f"team-{uuid.uuid4().hex[:6]}"


@router.post("/teams")
async def admin_team_create(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("teams.create"))],
    name: Annotated[str, Form(max_length=200)],
    description: Annotated[str, Form()] = "",
):
    ctx = await _member_ctx(session, request, user)
    name = (name or "").strip()
    if not name:
        return RedirectResponse(url="/admin/teams?err=Name+is+required", status_code=303)
    slug = _slugify(name)
    existing = await session.scalar(
        select(Team).where(Team.organization_id == ctx.organization_id, Team.slug == slug)
    )
    if existing is not None:
        return RedirectResponse(url="/admin/teams?err=A+team+with+that+name+already+exists", status_code=303)
    team = Team(
        organization_id=ctx.organization_id,
        name=name,
        slug=slug,
        description=(description or "").strip() or None,
        created_by=user.id,
    )
    session.add(team)
    await session.commit()
    await request.app.state.audit.log(
        session, action="team.created", actor_user_id=user.id,
        resource_type="team", resource_id=str(team.id), metadata={"name": name},
    )
    return RedirectResponse(url="/admin/teams?ok=Team+created", status_code=303)


@router.get("/teams/{team_id}", response_class=HTMLResponse)
async def admin_team_detail(
    team_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("teams.view"))],
):
    ctx = await _member_ctx(session, request, user)
    team = await session.get(Team, team_id)
    if team is None or team.organization_id != ctx.organization_id:
        raise UiRedirect("/admin/teams")
    members = (
        (await session.execute(
            select(User, TeamMember.is_lead, TeamMember.created_at)
            .join(TeamMember, TeamMember.user_id == User.id)
            .where(TeamMember.team_id == team.id)
            .order_by(User.full_name, User.email)
        )).all()
    )
    # candidates: active org members not yet in the team
    member_rows = (
        await session.scalars(
            select(OrganizationMember.user_id).where(
                OrganizationMember.organization_id == ctx.organization_id,
                OrganizationMember.status == MemberStatus.ACTIVE.value,
            )
        )
    ).all()
    existing_ids = {m_id for (m_id,) in (
        await session.execute(select(TeamMember.user_id).where(TeamMember.team_id == team.id))
    )}
    candidates = (
        (await session.execute(
            select(User).where(User.id.in_([uid for uid in member_rows if uid not in existing_ids]) or [uuid.uuid4()])
            .order_by(User.email)
        )).scalars().all()
        if member_rows else []
    )
    # team lead counts for visibility
    team_leads = int(
        await session.scalar(
            select(func.count()).select_from(Lead).where(
                Lead.assigned_team_id == team.id, Lead.merged_into_id.is_(None)
            )
        )
        or 0
    )
    return templates.TemplateResponse(
        request,
        "admin/team_detail.html",
        _ctx(
            request, user,
            team=team,
            members=[
                {
                    "id": u.id,
                    "email": u.email,
                    "full_name": u.full_name,
                    "is_lead": bool(is_lead),
                    "since": _fmt_dt(since),
                }
                for (u, is_lead, since) in members
            ],
            candidates=candidates,
            team_leads=team_leads,
            can_manage="teams.manage_members" in _perms(request),
            can_edit="teams.edit" in _perms(request),
            ok=request.query_params.get("ok"),
            err=request.query_params.get("err"),
            fmt=_fmt_dt,
        ),
    )


@router.post("/teams/{team_id}/update")
async def admin_team_update(
    team_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("teams.edit"))],
    name: Annotated[str, Form(max_length=200)] = "",
    description: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
):
    ctx = await _member_ctx(session, request, user)
    team = await session.get(Team, team_id)
    if team is None or team.organization_id != ctx.organization_id:
        raise UiRedirect("/admin/teams")
    if name.strip():
        team.name = name.strip()
    if description is not None:
        team.description = description.strip() or None
    team.is_active = is_active == "on" or is_active == "true"
    await session.commit()
    await request.app.state.audit.log(
        session, action="team.updated", actor_user_id=user.id,
        resource_type="team", resource_id=str(team.id), metadata={"is_active": team.is_active},
    )
    return RedirectResponse(url=f"/admin/teams/{team_id}?ok=Team+updated", status_code=303)


@router.post("/teams/{team_id}/members")
async def admin_team_add_member(
    team_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("teams.manage_members"))],
    user_id: Annotated[str, Form()],
    is_lead: Annotated[str, Form()] = "",
):
    ctx = await _member_ctx(session, request, user)
    team = await session.get(Team, team_id)
    if team is None or team.organization_id != ctx.organization_id:
        raise UiRedirect("/admin/teams")
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        return RedirectResponse(url=f"/admin/teams/{team_id}?err=Invalid+user", status_code=303)
    target = await session.get(User, target_id)
    if target is None or await authz.get_membership(session, target_id, ctx.organization_id) is None:
        return RedirectResponse(url=f"/admin/teams/{team_id}?err=User+is+not+an+organization+member", status_code=303)
    existing = await session.scalar(
        select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == target_id)
    )
    if existing is None:
        session.add(TeamMember(team_id=team.id, user_id=target_id, is_lead=is_lead == "on"))
        await session.commit()
        await request.app.state.audit.log(
            session, action="team.member_added", actor_user_id=user.id,
            resource_type="team", resource_id=str(team.id),
            metadata={"user_id": str(target_id)},
        )
    return RedirectResponse(url=f"/admin/teams/{team_id}?ok=Member+added", status_code=303)


@router.post("/teams/{team_id}/members/remove")
async def admin_team_remove_member(
    team_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("teams.manage_members"))],
    user_id: Annotated[str, Form()],
):
    ctx = await _member_ctx(session, request, user)
    team = await session.get(Team, team_id)
    if team is None or team.organization_id != ctx.organization_id:
        raise UiRedirect("/admin/teams")
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        raise UiRedirect(f"/admin/teams/{team_id}")
    row = await session.scalar(
        select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == target_id)
    )
    if row is not None:
        await session.delete(row)
        await session.commit()
        await request.app.state.audit.log(
            session, action="team.member_removed", actor_user_id=user.id,
            resource_type="team", resource_id=str(team.id),
            metadata={"user_id": str(target_id)},
        )
    return RedirectResponse(url=f"/admin/teams/{team_id}?ok=Member+removed", status_code=303)


# ------------------------------------------------------------------------ roles
@router.get("/roles", response_class=HTMLResponse)
async def admin_roles(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("roles.view"))],
):
    """Read-only role → permission matrix (§32). Changes ship via the API +
    migrations so the matrix can never drift from the enforced catalog."""
    perms_by_role: dict[str, set[str]] = {}
    rows = (
        await session.execute(
            select(Role.code, Permission.code)
            .join(role_permissions, role_permissions.c.permission_id == Permission.id)
            .join(Role, Role.id == role_permissions.c.role_id)
        )
    )
    for role_code, perm_code in rows:
        perms_by_role.setdefault(role_code, set()).add(perm_code)
    all_permissions = [code for code, _ in rbac_service.PERMISSIONS]
    role_list = [r["code"] for r in rbac_service.ROLES]
    user_counts = {
        code: int(n or 0)
        for code, n in (
            await session.execute(
                select(Role.code, func.count())
                .join(user_roles, user_roles.c.role_id == Role.id)
                .group_by(Role.code)
            )
        )
    }
    return templates.TemplateResponse(
        request,
        "admin/roles.html",
        _ctx(
            request, user,
            role_list=role_list,
            all_permissions=all_permissions,
            perms_by_role=perms_by_role,
            user_counts=user_counts,
        ),
    )


# ------------------------------------------------------------------ invitations
@router.get("/invitations", response_class=HTMLResponse)
async def admin_invitations(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("invitations.view"))],
):
    ctx = await _member_ctx(session, request, user)
    rows = (
        (await session.execute(
            select(Invitation)
            .where(Invitation.organization_id == ctx.organization_id)
            .order_by(Invitation.created_at.desc())
            .limit(200)
        )).scalars().all()
    )
    now = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        request,
        "admin/invitations.html",
        _ctx(
            request, user,
            invitations=[
                {
                    "id": inv.id,
                    "email": inv.email,
                    "roles": inv.role_codes or [],
                    "status": (
                        "ACCEPTED" if inv.accepted_at is not None
                        else "REVOKED" if inv.revoked_at is not None
                        else "EXPIRED" if inv.expires_at is not None and inv.expires_at <= now
                        else "PENDING"
                    ),
                    "expires": _fmt_dt(inv.expires_at),
                    "created": _fmt_dt(inv.created_at),
                    "accepted": _fmt_dt(inv.accepted_at),
                }
                for inv in rows
            ],
            all_roles=[r["code"] for r in rbac_service.ROLES if r["code"] != "SUPER_ADMIN"],
            teams=(
                (await session.execute(
                    select(Team).where(
                        Team.organization_id == ctx.organization_id, Team.is_active.is_(True)
                    ).order_by(Team.name)
                )).scalars().all()
            ),
            can_create="invitations.create" in _perms(request),
            can_revoke="invitations.revoke" in _perms(request),
            ok=request.query_params.get("ok"),
            err=request.query_params.get("err"),
            invite_url=request.query_params.get("invite_url"),
        ),
    )


@router.post("/invitations")
async def admin_invitation_create(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("invitations.create"))],
    email: Annotated[str, Form(max_length=320)],
    roles: Annotated[list[str], Form()] = [],
    team_id: Annotated[str, Form()] = "",
):
    import secrets as _secrets

    from app.core.security import hash_token_secret

    ctx = await _member_ctx(session, request, user)
    email = (email or "").strip().lower()
    if "@" not in email:
        return RedirectResponse(url="/admin/invitations?err=A+valid+email+is+required", status_code=303)
    existing_user = await session.scalar(select(User).where(User.email == email))
    if existing_user is not None and existing_user.effective_status == "ACTIVE":
        return RedirectResponse(url="/admin/invitations?err=An+active+user+with+that+email+exists", status_code=303)
    limiter = request.app.state.invite_limiter
    if not limiter.check(str(user.id)):
        return RedirectResponse(url="/admin/invitations?err=Too+many+invitations,+try+again+later", status_code=303)
    _valid_codes = {r["code"] for r in rbac_service.ROLES}
    role_codes = [r for r in roles if r in _valid_codes and r != "SUPER_ADMIN"]
    team_uuid = None
    if team_id:
        try:
            t = await session.get(Team, uuid.UUID(team_id))
        except ValueError:
            t = None
        if t is not None and t.organization_id == ctx.organization_id:
            team_uuid = t.id
    settings = request.app.state.settings
    expiry_hours = getattr(settings, "QBIT_INVITATION_EXPIRY_HOURS", 168)
    plaintext = _secrets.token_urlsafe(32)
    inv = Invitation(
        organization_id=ctx.organization_id,
        email=email,
        role_codes=role_codes,
        team_id=team_uuid,
        token_hash=hash_token_secret(plaintext),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
        invited_by=user.id,
    )
    session.add(inv)
    await session.commit()
    await request.app.state.audit.log(
        session, action="invitation.created", actor_user_id=user.id,
        resource_type="invitation", resource_id=str(inv.id),
        metadata={"email": email, "roles": role_codes},  # token NEVER logged
    )
    # §5: the plaintext link is shown to the admin exactly ONCE — rendered in
    # this POST response only (no redirect, never stored server-side)
    invite_url = f"{str(request.base_url).rstrip('/')}{INVITE_ACCEPT_PATH}?token={plaintext}"
    return templates.TemplateResponse(
        request,
        "admin/invitation_created.html",
        _ctx(request, user, email=email, invite_url=invite_url, roles=role_codes),
    )


@router.post("/invitations/{invitation_id}/revoke")
async def admin_invitation_revoke(
    invitation_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("invitations.revoke"))],
):
    ctx = await _member_ctx(session, request, user)
    inv = await session.get(Invitation, invitation_id)
    if inv is None or inv.organization_id != ctx.organization_id:
        raise UiRedirect("/admin/invitations")
    if inv.revoked_at is None and inv.accepted_at is None:
        inv.revoked_at = datetime.now(timezone.utc)
        await session.commit()
        await request.app.state.audit.log(
            session, action="invitation.revoked", actor_user_id=user.id,
            resource_type="invitation", resource_id=str(inv.id),
        )
    return RedirectResponse(url="/admin/invitations?ok=Invitation+revoked", status_code=303)
