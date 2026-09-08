"""Admin area APIs (Phase 11 §19–§21): overview, audit search, org settings.

All values come from real queries — no fake statistics (§20).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select

from app.api.deps import DbSession, get_client_ip, require_context
from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.audit import AuditLog
from app.models.connection import Connection
from app.models.enterprise import Invitation, Organization
from app.models.marketing import Campaign
from app.models.messaging import Conversation
from app.models.scrape import Lead, ScrapeJob
from app.models.user import User
from app.schemas.common import PageMeta
from app.schemas.enterprise import (
    AdminOverviewOut,
    AuditSearchOut,
    OrgSettingsUpdate,
    SecuritySettingsOut,
    SecuritySettingsUpdate,
)
from app.services.authorization import MemberContext

router = APIRouter(prefix="/admin", tags=["admin"])


# --- 20. overview ---------------------------------------------------------------


@router.get("/overview", response_model=AdminOverviewOut)
async def admin_overview(
    session: DbSession,
    ctx: MemberContext = Depends(require_context("users.view")),
):
    org_id = ctx.organization_id
    active_users = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.is_active.is_(True))
        )
        or 0
    )
    from app.models.enterprise import OrganizationMember, Team

    members = int(
        await session.scalar(
            select(func.count()).select_from(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.status == "ACTIVE",
            )
        )
        or 0
    )
    teams = int(
        await session.scalar(
            select(func.count()).select_from(Team).where(
                Team.organization_id == org_id, Team.is_active.is_(True)
            )
        )
        or 0
    )
    active_connections = int(
        await session.scalar(
            select(func.count()).select_from(Connection).where(
                Connection.organization_id == org_id,
                Connection.status.in_(["CONNECTED", "PENDING"]),
            )
        )
        or 0
    )
    active_campaigns = int(
        await session.scalar(
            select(func.count()).select_from(Campaign).where(
                Campaign.organization_id == org_id,
                Campaign.status.in_(["SCHEDULED", "RUNNING", "SENDING", "PROCESSING"]),
            )
        )
        or 0
    )
    open_conversations = int(
        await session.scalar(
            select(func.count()).select_from(Conversation).where(
                Conversation.organization_id == org_id,
                Conversation.status.in_(["PENDING", "OPEN", "ACTIVE"]),
            )
        )
        or 0
    )
    active_jobs = int(
        await session.scalar(
            select(func.count()).select_from(ScrapeJob).where(
                ScrapeJob.organization_id == org_id,
                ScrapeJob.status.in_(["QUEUED", "RUNNING", "PAUSED"]),
            )
        )
        or 0
    )
    leads_count = int(
        await session.scalar(
            select(func.count()).select_from(Lead).where(
                Lead.organization_id == org_id, Lead.merged_into_id.is_(None)
            )
        )
        or 0
    )
    pending_invitations = int(
        await session.scalar(
            select(func.count()).select_from(Invitation).where(
                Invitation.organization_id == org_id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
        )
        or 0
    )

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent_events = (
        (await session.execute(
            select(AuditLog)
            .where(
                or_(
                    AuditLog.organization_id == org_id,
                    AuditLog.organization_id.is_(None),
                ),
                AuditLog.created_at >= week_ago,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(10)
        )).scalars().all()
    )
    security_actions = (
        "user.login_failed", "invitation.revoked", "session.revoked",
        "session.revoked_all", "apikey.created", "apikey.revoked",
        "user.password_changed", "user.deactivated",
    )
    security_alerts = (
        (await session.execute(
            select(AuditLog)
            .where(
                AuditLog.action.in_(security_actions),
                AuditLog.created_at >= week_ago,
                or_(
                    AuditLog.organization_id == org_id,
                    AuditLog.organization_id.is_(None),
                ),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(10)
        )).scalars().all()
    )

    return AdminOverviewOut(data={
        "active_users": active_users,
        "organization_members": members,
        "teams": teams,
        "active_connections": active_connections,
        "active_campaigns": active_campaigns,
        "open_conversations": open_conversations,
        "active_scrape_jobs": active_jobs,
        "leads": leads_count,
        "pending_invitations": pending_invitations,
        "recent_activity": [e.to_public_dict() for e in recent_events],
        "security_alerts": [e.to_public_dict() for e in security_alerts],
        # workflow engine (Phase 9) is not part of this repository yet —
        # honest "unavailable" instead of a fake zero (§20 no fake stats)
        "active_workflows": None,
    })


# --- 19. audit center ------------------------------------------------------------


@router.get("/audit", response_model=AuditSearchOut)
async def search_audit(
    session: DbSession,
    ctx: MemberContext = Depends(require_context("audit.view")),
    actor_user_id: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
    success: bool | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    query = select(AuditLog).where(
        or_(
            AuditLog.organization_id == ctx.organization_id,
            AuditLog.organization_id.is_(None),
        )
    )
    count_q = select(func.count()).select_from(AuditLog).where(
        or_(
            AuditLog.organization_id == ctx.organization_id,
            AuditLog.organization_id.is_(None),
        )
    )
    if actor_user_id is not None:
        query = query.where(AuditLog.actor_user_id == actor_user_id)
        count_q = count_q.where(AuditLog.actor_user_id == actor_user_id)
    if action:
        query = query.where(AuditLog.action.ilike(f"%{action}%"))
        count_q = count_q.where(AuditLog.action.ilike(f"%{action}%"))
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
        count_q = count_q.where(AuditLog.resource_type == resource_type)
    if resource_id:
        query = query.where(AuditLog.resource_id == resource_id)
        count_q = count_q.where(AuditLog.resource_id == resource_id)
    if date_from is not None:
        query = query.where(AuditLog.created_at >= date_from)
        count_q = count_q.where(AuditLog.created_at >= date_from)
    if date_to is not None:
        query = query.where(AuditLog.created_at <= date_to)
        count_q = count_q.where(AuditLog.created_at <= date_to)
    # success flag: login_failed-style actions are failures
    if success is False:
        query = query.where(AuditLog.action.contains("failed"))
        count_q = count_q.where(AuditLog.action.contains("failed"))
    elif success is True:
        query = query.where(~AuditLog.action.contains("failed"))
        count_q = count_q.where(~AuditLog.action.contains("failed"))

    total = int(await session.scalar(count_q) or 0)
    rows = (
        (await session.execute(query.order_by(AuditLog.created_at.desc())
                               .offset((page - 1) * page_size).limit(page_size)))
        .scalars().all()
    )
    return AuditSearchOut(
        data=[e.to_public_dict() for e in rows],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


# --- 21. organization settings ----------------------------------------------------


@router.get("/settings")
async def org_settings(
    session: DbSession,
    ctx: MemberContext = Depends(require_context("settings.view")),
):
    org = await session.get(Organization, ctx.organization_id)
    if org is None:
        raise NotFoundError("Organization not found")
    return {"success": True, "data": org.to_public_dict()}


@router.patch("/settings")
async def update_org_settings(
    payload: OrgSettingsUpdate,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("settings.manage")),
):
    org = await session.get(Organization, ctx.organization_id)
    if org is None:
        raise NotFoundError("Organization not found")
    changed = {}
    if payload.name is not None:
        org.name = payload.name.strip()
        changed["name"] = org.name
    if payload.timezone is not None:
        org.timezone = payload.timezone
        changed["timezone"] = org.timezone
    if payload.locale is not None:
        org.locale = payload.locale
        changed["locale"] = org.locale
    if payload.visibility_defaults is not None:
        allowed = {"ALL", "TEAM", "ASSIGNED_ONLY", "OWNED_ONLY"}
        clean = {
            role: scope
            for role, scope in payload.visibility_defaults.items()
            if scope in allowed
        }
        settings = dict(org.settings_json or {})
        settings["visibility_defaults"] = clean
        org.settings_json = settings
        changed["visibility_defaults"] = clean
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="organization.settings_updated",
        actor_user_id=ctx.user.id,
        resource_type="organization",
        resource_id=str(org.id),
        ip_address=get_client_ip(request),
        metadata={"changes": changed},
    )
    return {"success": True, "data": org.to_public_dict()}


# --- 18. security settings (DB-backed where safe, env fallback) -------------------

_SECURITY_SETTING_KEYS = {
    "security.invitation_expiry_hours": "invitation_expiry_hours",
    "security.session_ttl_minutes": "session_ttl_minutes",
}


def _security_defaults(request: Request) -> dict:
    s = request.app.state.settings
    return {
        "invitation_expiry_hours": getattr(s, "QBIT_INVITATION_EXPIRY_HOURS", 168),
        "session_ttl_minutes": s.QBIT_SESSION_TTL_MINUTES,
        "password_min_length": s.QBIT_PASSWORD_MIN_LENGTH,
        "login_rate_limit_per_min": s.QBIT_RATE_LIMIT_LOGIN_PER_MIN,
    }


@router.get("/security", response_model=SecuritySettingsOut)
async def get_security_settings(
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("security.view")),
):
    from app.services.settings import SystemSettingsService

    service = SystemSettingsService(request.app.state.audit)
    values = _security_defaults(request)
    for key, field in _SECURITY_SETTING_KEYS.items():
        try:
            stored = await service.get(session, key)
        except NotFoundError:
            continue
        if stored is not None:
            try:
                values[field] = int(stored)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    return SecuritySettingsOut(**values)


@router.patch("/security", response_model=SecuritySettingsOut)
async def update_security_settings(
    payload: SecuritySettingsUpdate,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("security.manage")),
):
    from app.services.settings import SystemSettingsService

    service = SystemSettingsService(request.app.state.audit)
    changed = {}
    if payload.invitation_expiry_hours is not None:
        await service.set(
            session,
            "security.invitation_expiry_hours",
            payload.invitation_expiry_hours,
            updated_by=ctx.user.id,
        )
        changed["invitation_expiry_hours"] = payload.invitation_expiry_hours
    if payload.session_ttl_minutes is not None:
        await service.set(
            session,
            "security.session_ttl_minutes",
            payload.session_ttl_minutes,
            updated_by=ctx.user.id,
        )
        changed["session_ttl_minutes"] = payload.session_ttl_minutes
    await request.app.state.audit.log(
        session,
        action="security.settings_updated",
        actor_user_id=ctx.user.id,
        resource_type="security_settings",
        resource_id=None,
        ip_address=get_client_ip(request),
        metadata={"changes": changed},
    )
    return await get_security_settings(request, session, ctx)
