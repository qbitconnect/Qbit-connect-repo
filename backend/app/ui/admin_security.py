"""Admin UI — security console (Phase 11 §15–§19, §21–§24, §31–§34).

Connection access scopes, API keys (one-time plaintext), security settings,
session management, the immutable audit center and organization settings.
Same conventions as app/ui/admin.py — the API remains the enforcement layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.security import hash_token_secret, new_api_key_secret
from app.models.audit import AuditLog
from app.models.connection import Connection
from app.models.enterprise import (
    API_KEY_SCOPES,
    ApiKey,
    Organization,
    Team,
    UserSession,
    utc_aware,
)
from app.models.marketing import SendingAccount
from app.models.user import User
from app.services import authorization as authz
from app.services import rbac as rbac_service
from app.ui import UiRedirect, _ctx, require_ui_permission
from app.ui.admin import _fmt_dt, _member_ctx, _perms

router = APIRouter(prefix="/admin", tags=["ui-admin-security"])


# ------------------------------------------------------------- connections scope
@router.get("/connections", response_class=HTMLResponse)
async def admin_connections(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("connections.view"))],
):
    ctx = await _member_ctx(session, request, user)
    conns = (
        (await session.execute(
            select(Connection).where(
                or_(
                    Connection.organization_id == ctx.organization_id,
                    Connection.organization_id.is_(None),
                )
            ).order_by(Connection.created_at.desc())
        )).scalars().all()
    )
    accounts = (
        (await session.execute(
            select(SendingAccount).where(
                or_(
                    SendingAccount.organization_id == ctx.organization_id,
                    SendingAccount.organization_id.is_(None),
                )
            ).order_by(SendingAccount.created_at.desc())
        )).scalars().all()
    )
    can_edit = "connections.edit" in _perms(request) or ctx.is_super_admin
    teams = (
        (await session.execute(
            select(Team)
            .where(Team.organization_id == ctx.organization_id)
            .order_by(Team.name)
        )).scalars().all()
    )
    return _render(request, user, "admin/connections.html", dict(
        conns=conns,
        accounts=accounts,
        can_edit=can_edit,
        scopes=["ORGANIZATION", "TEAM", "RESTRICTED"],
        teams=teams,
        ok=request.query_params.get("ok"),
        err=request.query_params.get("err"),
        fmt=_fmt_dt,
    ))


@router.post("/connections/{resource_type}/{resource_id}/scope")
async def admin_connection_scope(
    resource_type: str,
    resource_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("connections.edit"))],
    access_scope: Annotated[str, Form()],
    team_id: Annotated[str, Form()] = "",
):
    """Set ORGANIZATION | TEAM | RESTRICTED on a connection or sending account."""
    ctx = await _member_ctx(session, request, user)
    scope = (access_scope or "").upper()
    if scope not in ("ORGANIZATION", "TEAM", "RESTRICTED"):
        return RedirectResponse(url="/admin/connections?err=Invalid+scope", status_code=303)
    model = (
        Connection if resource_type == "connection"
        else SendingAccount if resource_type == "account"
        else None
    )
    if model is None:
        raise UiRedirect("/admin/connections")
    row = await session.get(model, resource_id)
    if row is None:
        raise UiRedirect("/admin/connections")
    row_org = getattr(row, "organization_id", None)
    if row_org is not None and row_org != ctx.organization_id:
        raise UiRedirect("/admin/connections")
    row.access_scope = scope
    team_uuid = None
    if scope == "TEAM" and team_id:
        from app.models.enterprise import Team

        try:
            team = await session.get(Team, uuid.UUID(team_id))
        except ValueError:
            team = None
        if team is not None and team.organization_id == ctx.organization_id:
            team_uuid = team.id
    row.team_id = team_uuid
    await session.commit()
    await request.app.state.audit.log(
        session, action="connection.scope_updated", actor_user_id=user.id,
        resource_type="connection", resource_id=str(resource_id),
        metadata={"scope": scope, "team_id": str(team_uuid) if team_uuid else None},
    )
    return RedirectResponse(url="/admin/connections?ok=Access+scope+updated", status_code=303)


# ---------------------------------------------------------------------- API keys
@router.get("/api-keys", response_class=HTMLResponse)
async def admin_api_keys(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("apikeys.view"))],
):
    ctx = await _member_ctx(session, request, user)
    rows = (
        (await session.execute(
            select(ApiKey)
            .where(ApiKey.organization_id == ctx.organization_id)
            .order_by(ApiKey.created_at.desc())
        )).scalars().all()
    )
    owner_uids = [k.created_by for k in rows if k.created_by] or [uuid.uuid4()]
    owner_names = {
        uid: name
        for uid, name in (
            await session.execute(select(User.id, User.full_name).where(User.id.in_(owner_uids)))
        )
    }
    return _render(request, user, "admin/api_keys.html", dict(
        keys=[
            {
                "id": k.id,
                "name": k.name,
                "prefix": k.prefix,
                "scopes": k.scopes or [],
                "status": "REVOKED" if k.revoked_at is not None else "ACTIVE",
                "last_used": _fmt_dt(k.last_used_at),
                "created": _fmt_dt(k.created_at),
                "owner": owner_names.get(k.created_by, "—"),
            }
            for k in rows
        ],
        all_scopes=sorted(API_KEY_SCOPES),
        can_create="apikeys.create" in _perms(request),
        can_revoke="apikeys.revoke" in _perms(request),
        ok=request.query_params.get("ok"),
        err=request.query_params.get("err"),
    ))


@router.post("/api-keys")
async def admin_api_key_create(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("apikeys.create"))],
    name: Annotated[str, Form(max_length=120)],
    scopes: Annotated[list[str], Form()] = [],
):
    ctx = await _member_ctx(session, request, user)
    limiter = request.app.state.apikey_limiter
    if not limiter.check(str(user.id)):
        return RedirectResponse(url="/admin/api-keys?err=Rate+limit+hit,+try+again+later", status_code=303)
    clean_scopes = sorted({s for s in scopes if s in API_KEY_SCOPES})
    if not clean_scopes:
        return RedirectResponse(url="/admin/api-keys?err=Select+at+least+one+scope", status_code=303)
    name = (name or "").strip() or "API key"
    # canonical generator: same format the API bearer resolver expects
    full_key, prefix, secret = new_api_key_secret()
    key = ApiKey(
        organization_id=ctx.organization_id,
        name=name[:120],
        prefix=prefix,
        key_hash=hash_token_secret(secret),
        scopes=clean_scopes,
        created_by=user.id,
    )
    session.add(key)
    await session.commit()
    await request.app.state.audit.log(
        session, action="apikey.created", actor_user_id=user.id,
        resource_type="api_key", resource_id=str(key.id),
        metadata={"name": name, "scopes": clean_scopes},  # plaintext NEVER logged
    )
    # §23: plaintext shown exactly ONCE in this POST response — never stored
    return _render(request, user, "admin/api_key_created.html", dict(
        key_name=name, key_plain=full_key, key_scopes=clean_scopes,
    ))


@router.post("/api-keys/{key_id}/revoke")
async def admin_api_key_revoke(
    key_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("apikeys.revoke"))],
):
    ctx = await _member_ctx(session, request, user)
    key = await session.get(ApiKey, key_id)
    if key is None or key.organization_id != ctx.organization_id:
        raise UiRedirect("/admin/api-keys")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        await session.commit()
        await request.app.state.audit.log(
            session, action="apikey.revoked", actor_user_id=user.id,
            resource_type="api_key", resource_id=str(key.id),
        )
    return RedirectResponse(url="/admin/api-keys?ok=API+key+revoked", status_code=303)


# ---------------------------------------------------------------------- sessions
@router.get("/security", response_class=HTMLResponse)
async def admin_security(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("security.view"))],
):
    ctx = await _member_ctx(session, request, user)
    rows = (
        (await session.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .order_by(UserSession.last_seen_at.desc().nullslast())
            .limit(200)
        )).all()
    )
    now = datetime.now(timezone.utc)
    s = request.app.state.settings
    from app.services.settings import SystemSettingsService

    service = SystemSettingsService(request.app.state.audit)
    stored = {}
    for key in ("security.invitation_expiry_hours", "security.session_ttl_minutes"):
        try:
            stored[key] = await service.get(session, key)
        except Exception:
            stored[key] = None
    return _render(request, user, "admin/security.html", dict(
        sessions=[
            {
                "id": row.id,
                "user": target.email,
                "user_id": target.id,
                "ip": row.ip_address or "—",
                "agent": (row.user_agent or "")[:60],
                "created": _fmt_dt(row.created_at),
                "last_seen": _fmt_dt(row.last_seen_at),
                "expires": _fmt_dt(row.expires_at),
                "status": (
                    "REVOKED" if row.revoked_at is not None
                    else "EXPIRED" if row.expires_at is not None and utc_aware(row.expires_at) <= now
                    else "ACTIVE"
                ),
                "is_mine": row.user_id == user.id,
            }
            for (row, target) in rows
        ],
        settings={
            "invitation_expiry_hours": stored.get("security.invitation_expiry_hours")
            or getattr(s, "QBIT_INVITATION_EXPIRY_HOURS", 168),
            "session_ttl_minutes": stored.get("security.session_ttl_minutes")
            or s.QBIT_SESSION_TTL_MINUTES,
            "password_min_length": s.QBIT_PASSWORD_MIN_LENGTH,
            "login_rate_limit_per_min": s.QBIT_RATE_LIMIT_LOGIN_PER_MIN,
        },
        can_manage="security.manage" in _perms(request),
        can_revoke="sessions.revoke" in _perms(request),
        ok=request.query_params.get("ok"),
        err=request.query_params.get("err"),
    ))


@router.post("/security")
async def admin_security_update(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("security.manage"))],
    invitation_expiry_hours: Annotated[int, Form()],
    session_ttl_minutes: Annotated[int, Form()],
):
    from app.services.settings import SystemSettingsService

    ctx = await _member_ctx(session, request, user)
    service = SystemSettingsService(request.app.state.audit)
    changed = {}
    if invitation_expiry_hours >= 1:
        await service.set(session, "security.invitation_expiry_hours",
                          invitation_expiry_hours, updated_by=user.id)
        changed["invitation_expiry_hours"] = invitation_expiry_hours
    if session_ttl_minutes >= 5:
        await service.set(session, "security.session_ttl_minutes",
                          session_ttl_minutes, updated_by=user.id)
        changed["session_ttl_minutes"] = session_ttl_minutes
    await session.commit()
    await request.app.state.audit.log(
        session, action="security.settings_updated", actor_user_id=user.id,
        resource_type="security_settings", resource_id=None,
        metadata={"changes": changed},
    )
    return RedirectResponse(url="/admin/security?ok=Security+settings+updated", status_code=303)


@router.post("/sessions/{session_id}/revoke")
async def admin_session_revoke(
    session_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("sessions.revoke"))],
):
    ctx = await _member_ctx(session, request, user)
    row = await session.get(UserSession, session_id)
    if row is None:
        raise UiRedirect("/admin/security")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        row.revoked_reason = "REVOKED_BY_ADMIN"
        await session.commit()
        await request.app.state.audit.log(
            session, action="session.revoked", actor_user_id=user.id,
            resource_type="session", resource_id=str(row.id),
            metadata={"target_user_id": str(row.user_id)},
        )
    return RedirectResponse(url="/admin/security?ok=Session+revoked", status_code=303)


# ------------------------------------------------------------------------- audit
@router.get("/audit", response_class=HTMLResponse)
async def admin_audit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("audit.view"))],
    action: str = Query(default="", max_length=200),
    resource_type: str = Query(default="", max_length=60),
    actor: str = Query(default="", max_length=320),
    success: str = Query(default=""),
    page: int = Query(default=1, ge=1),
):
    ctx = await _member_ctx(session, request, user)
    query = select(AuditLog).where(
        or_(AuditLog.organization_id == ctx.organization_id, AuditLog.organization_id.is_(None))
    )
    count_q = select(func.count()).select_from(AuditLog).where(
        or_(AuditLog.organization_id == ctx.organization_id, AuditLog.organization_id.is_(None))
    )
    if action:
        query = query.where(AuditLog.action.ilike(f"%{action}%"))
        count_q = count_q.where(AuditLog.action.ilike(f"%{action}%"))
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
        count_q = count_q.where(AuditLog.resource_type == resource_type)
    if actor:
        like = f"%{actor.lower()}%"
        query = query.where(AuditLog.actor_user_id.in_(
            select(User.id).where(or_(func.lower(User.email).like(like), func.lower(User.full_name).like(like)))
        ))
        count_q = count_q.where(AuditLog.actor_user_id.in_(
            select(User.id).where(or_(func.lower(User.email).like(like), func.lower(User.full_name).like(like)))
        ))
    if success == "failed":
        query = query.where(AuditLog.action.contains("failed"))
        count_q = count_q.where(AuditLog.action.contains("failed"))
    elif success == "ok":
        query = query.where(~AuditLog.action.contains("failed"))
        count_q = count_q.where(~AuditLog.action.contains("failed"))
    total = int(await session.scalar(count_q) or 0)
    rows = (
        (await session.execute(
            query.order_by(AuditLog.created_at.desc())
            .offset((page - 1) * 50).limit(50)
        )).scalars().all()
    )
    return _render(request, user, "admin/audit.html", dict(
        logs=[
            {
                "id": e.id,
                "action": e.action,
                "resource_type": e.resource_type,
                "resource_id": (e.resource_id or "")[:8] or "—",
                "actor": e.actor_user_id,
                "ip": e.ip_address or "—",
                "created": _fmt_dt(e.created_at),
                "failed": "failed" in (e.action or ""),
            }
            for e in rows
        ],
        action=action,
        resource_type=resource_type,
        actor=actor,
        success=success,
        page=page,
        total=total,
        page_size=50,
    ))


# ---------------------------------------------------------------------- settings
@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("settings.view"))],
):
    ctx = await _member_ctx(session, request, user)
    org = await session.get(Organization, ctx.organization_id)
    defaults = (org.settings_json or {}).get("visibility_defaults") or {}
    return _render(request, user, "admin/settings.html", dict(
        org=org,
        visibility_defaults=defaults,
        role_list=[r["code"] for r in rbac_service.ROLES],
        scope_list=["ALL", "TEAM", "ASSIGNED_ONLY", "OWNED_ONLY"],
        can_manage="settings.manage" in _perms(request),
        ok=request.query_params.get("ok"),
        err=request.query_params.get("err"),
    ))


@router.post("/settings")
async def admin_settings_update(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("settings.manage"))],
    name: Annotated[str, Form(max_length=200)] = "",
    timezone: Annotated[str, Form(max_length=64)] = "UTC",
    locale: Annotated[str, Form(max_length=20)] = "en",
):
    ctx = await _member_ctx(session, request, user)
    org = await session.get(Organization, ctx.organization_id)
    if org is None:
        raise UiRedirect("/admin/settings")
    if name.strip():
        org.name = name.strip()
    org.timezone = timezone.strip() or "UTC"
    org.locale = locale.strip() or "en"
    await session.commit()
    await request.app.state.audit.log(
        session, action="organization.settings_updated", actor_user_id=user.id,
        resource_type="organization", resource_id=str(org.id),
        metadata={"name": org.name, "timezone": org.timezone, "locale": org.locale},
    )
    return RedirectResponse(url="/admin/settings?ok=Settings+updated", status_code=303)


@router.post("/settings/visibility")
async def admin_settings_visibility(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("settings.manage"))],
):
    """Update per-role default visibility scopes (form fields named
    `scope_<ROLE>`). Invalid values are dropped, never error the whole batch."""
    ctx = await _member_ctx(session, request, user)
    org = await session.get(Organization, ctx.organization_id)
    if org is None:
        raise UiRedirect("/admin/settings")
    form = await request.form()
    allowed = {"ALL", "TEAM", "ASSIGNED_ONLY", "OWNED_ONLY"}
    clean = {}
    for key, value in form.items():
        if key.startswith("scope_"):
            role = key[len("scope_"):]
            if value in allowed and role in {r["code"] for r in rbac_service.ROLES}:
                clean[role] = value
    settings = dict(org.settings_json or {})
    if clean:
        settings["visibility_defaults"] = clean
    else:
        settings.pop("visibility_defaults", None)
    org.settings_json = settings
    await session.commit()
    await request.app.state.audit.log(
        session, action="organization.visibility_updated", actor_user_id=user.id,
        resource_type="organization", resource_id=str(org.id),
        metadata={"visibility_defaults": clean},
    )
    return RedirectResponse(url="/admin/settings?ok=Visibility+defaults+updated", status_code=303)


def _render(request: Request, user, template: str, extra: dict):
    from app.ui import templates as _t

    return _t.TemplateResponse(request, template, _ctx(request, user, **extra))
