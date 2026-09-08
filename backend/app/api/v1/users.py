"""User management endpoints (Brief §9, §10; Phase 11 §4, §32, §35).

RBAC is enforced server-side via `require_permission` dependencies — never only
by hiding UI elements. Phase 11 adds: status lifecycle, search/filter, teams,
last-admin protection, self-role-change protection, deactivation → session
revocation, and a per-user activity summary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select

from app.api.deps import CurrentUser, DbSession, get_client_ip, get_user_agent
from app.api.deps import require_permission
from app.core.errors import ConflictError, NotFoundError
from app.core.security import hash_password, verify_password
from app.models.enterprise import TeamMember, UserSession
from app.models.user import User
from app.schemas.enterprise import PreferencesIn
from app.schemas.user import (
    UserActionOut,
    UserCreate,
    UserListOut,
    UserOut,
    UserPasswordChange,
    UserUpdate,
)
from app.services import authorization as authz
from app.services import rbac as rbac_service

router = APIRouter(prefix="/users", tags=["users"])


def _to_out(user: User) -> UserOut:
    return UserOut(**user.to_public_dict())


@router.post("", response_model=UserActionOut, status_code=201)
async def create_user(
    payload: UserCreate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.manage"))],
):
    email = payload.email.lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ConflictError("A user with this email already exists")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        status="ACTIVE",
    )
    session.add(user)
    await session.flush()

    if payload.roles:
        if "SUPER_ADMIN" in payload.roles:
            raise ConflictError("SUPER_ADMIN cannot be granted by user creation")
        await rbac_service.set_user_roles(session, user.id, payload.roles)
    else:
        await session.commit()

    # Phase 11: attach to the actor's organization
    ctx = getattr(request.state, "member_context", None)
    if ctx is None:
        from app.services.authorization import resolve_context

        perms = await rbac_service.load_user_permissions(session, actor.id)
        ctx = await resolve_context(session, actor, perms)
    await authz.ensure_membership(session, user, ctx.organization)
    await session.commit()
    await session.refresh(user)

    await request.app.state.audit.log(
        session,
        action="user.created",
        actor_user_id=actor.id,
        resource_type="user",
        resource_id=str(user.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"roles": user.role_codes},
    )
    return UserActionOut(data=_to_out(user))


@router.get("", response_model=UserListOut)
async def list_users(
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.view"))],
    search: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None, max_length=20),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    query = select(User)
    count_q = select(func.count()).select_from(User)
    if search:
        pattern = f"%{search.lower()}%"
        flt = or_(func.lower(User.email).like(pattern), func.lower(User.full_name).like(pattern))
        query = query.where(flt)
        count_q = count_q.where(flt)
    if status:
        flt = User.status == status.upper()
        query = query.where(flt)
        count_q = count_q.where(flt)
    total = int(await session.scalar(count_q) or 0)
    rows = await session.execute(
        query.order_by(User.created_at.asc()).offset((page - 1) * page_size).limit(page_size)
    )
    users = [_to_out(u) for u in rows.scalars().all()]
    return UserListOut(
        data=users,
        meta={"page": page, "page_size": page_size, "total": total},
    )


# --- Phase 11 §26: user preferences (own profile; never auth material) -----------


@router.get("/me/preferences")
async def get_own_preferences(
    session: DbSession,
    actor: CurrentUser,
):
    """Return the caller's preferences (timezone / locale / dashboard / notify)."""
    from app.models.enterprise import UserPreference

    row = await session.get(UserPreference, actor.id)
    if row is None:
        row = UserPreference(user_id=actor.id)
        session.add(row)
        await session.commit()
    return {
        "success": True,
        "data": {
            "timezone": row.timezone,
            "locale": row.locale,
            "preferences": row.preferences_json or {},
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        },
    }


@router.patch("/me/preferences")
async def update_own_preferences(
    payload: PreferencesIn,
    session: DbSession,
    actor: CurrentUser,
):
    """Update the caller's preferences. Sensitive authentication data is
    intentionally NOT accepted here (Phase 11 §26)."""
    from app.models.enterprise import UserPreference

    row = await session.get(UserPreference, actor.id)
    if row is None:
        row = UserPreference(user_id=actor.id)
        session.add(row)
    changed = {}
    if payload.timezone is not None:
        row.timezone = payload.timezone.strip()[:64]
        changed["timezone"] = row.timezone
    if payload.locale is not None:
        row.locale = payload.locale.strip()[:20]
        changed["locale"] = row.locale
    if payload.preferences is not None:
        merged = dict(row.preferences_json or {})
        for key, value in payload.preferences.items():
            if len(key) > 60:
                raise ConflictError("Preference keys are limited to 60 characters")
            merged[key] = value
        row.preferences_json = merged
        changed["preferences"] = sorted(merged.keys())
    await session.commit()
    return {
        "success": True,
        "data": {
            "timezone": row.timezone,
            "locale": row.locale,
            "preferences": row.preferences_json or {},
            "updated": sorted(changed.keys()),
        },
    }


@router.get("/{user_id}", response_model=UserActionOut)
async def get_user(
    user_id: uuid.UUID,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.view"))],
):
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return UserActionOut(data=_to_out(user))


@router.patch("/{user_id}", response_model=UserActionOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.manage"))],
):
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")

    changed = {}
    if payload.full_name is not None:
        user.full_name = payload.full_name
        changed["full_name"] = payload.full_name
    if payload.is_active is not None:
        if user.id == actor.id and payload.is_active is False:
            raise ConflictError("You cannot deactivate your own account")
        if not payload.is_active:
            await authz.assert_not_last_super_admin(session, user.id)
        user.is_active = payload.is_active
        user.status = "ACTIVE" if payload.is_active else "DEACTIVATED"
        changed["is_active"] = payload.is_active
        if not payload.is_active:
            # Phase 11 §17: deactivation revokes sessions + outstanding tokens
            now = datetime.now(timezone.utc)
            user.tokens_revoked_before = now
            await session.execute(
                UserSession.__table__.update()
                .where(
                    UserSession.user_id == user.id,
                    UserSession.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_reason="ACCOUNT_DEACTIVATED")
            )
            # Phase 11 §25: notify the affected user (security event)
            from app.services import notifications as notification_service

            _ctx_deact = getattr(request.state, "member_context", None)
            await notification_service.emit(
                session,
                user_id=user.id,
                organization_id=getattr(_ctx_deact, "organization_id", None),
                type="SECURITY",
                title="Your account has been deactivated",
                body="All active sessions were revoked. Contact an administrator if you believe this is a mistake.",
            )
    await session.commit()
    await session.refresh(user)  # reload roles before role-code checks below

    if payload.roles is not None:
        if user.id == actor.id:
            raise ConflictError("You cannot change your own roles (self-approval protection)")
        if "SUPER_ADMIN" in payload.roles:
            existing_codes = set(user.role_codes)
            if "SUPER_ADMIN" not in existing_codes:
                raise ConflictError("SUPER_ADMIN cannot be granted by role edit")
        if "SUPER_ADMIN" in user.role_codes and "SUPER_ADMIN" not in payload.roles:
            await authz.assert_not_last_super_admin(session, user.id)
        applied = await rbac_service.set_user_roles(session, user.id, payload.roles)
        changed["roles"] = applied
        await session.refresh(user)

    await request.app.state.audit.log(
        session,
        action="user.updated",
        actor_user_id=actor.id,
        resource_type="user",
        resource_id=str(user.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"changes": changed},
    )
    return UserActionOut(data=_to_out(user))


@router.post("/{user_id}/password", response_model=UserActionOut)
async def change_password(
    user_id: uuid.UUID,
    payload: UserPasswordChange,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.manage"))],
):
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    if verify_password(payload.password, user.password_hash):
        raise ConflictError("New password must differ from the current password")
    user.password_hash = hash_password(payload.password)
    # Phase 11 §17: security reset invalidates outstanding tokens + sessions
    now = datetime.now(timezone.utc)
    user.tokens_revoked_before = now
    await session.execute(
        UserSession.__table__.update()
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason="PASSWORD_RESET")
    )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="user.password_changed",
        actor_user_id=actor.id,
        resource_type="user",
        resource_id=str(user.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return UserActionOut(data=_to_out(user))


@router.get("/{user_id}/activity", response_model=UserActionOut)
async def user_activity(
    user_id: uuid.UUID,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.view"))],
):
    """Phase 11 §32: per-user operational summary (no credentials, ever)."""
    from app.models.marketing import Campaign
    from app.models.scrape import Lead, ScrapeJob

    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    assigned_leads = int(
        await session.scalar(
            select(func.count()).select_from(Lead).where(Lead.assigned_user_id == user.id)
        )
        or 0
    )
    campaigns_created = int(
        await session.scalar(
            select(func.count()).select_from(Campaign).where(Campaign.created_by == user.id)
        )
        or 0
    )
    jobs_created = int(
        await session.scalar(
            select(func.count()).select_from(ScrapeJob).where(ScrapeJob.created_by == user.id)
        )
        or 0
    )
    live_sessions = int(
        await session.scalar(
            select(func.count()).select_from(UserSession).where(
                UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
            )
        )
        or 0
    )
    team_rows = (await session.execute(select(TeamMember).where(TeamMember.user_id == user.id))).scalars().all()
    data = user.to_public_dict()
    data.update(
        {
            "assigned_leads": assigned_leads,
            "campaigns_created": campaigns_created,
            "scrape_jobs_created": jobs_created,
            "live_sessions": live_sessions,
            "teams": [
                {"team_id": str(t.team_id), "is_lead": t.is_lead} for t in team_rows
            ],
        }
    )
    return UserActionOut(data=data)
