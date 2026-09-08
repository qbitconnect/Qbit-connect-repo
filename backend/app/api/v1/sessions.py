"""Session management (Phase 11 §17): view + revoke active sessions.

Users may always view/revoke their own sessions; `sessions.view`/`sessions.revoke`
permissions unlock other users' sessions. Token values (jti) are never exposed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.api.deps import (
    CurrentUser,
    DbSession,
    get_client_ip,
    require_context,
)
from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.enterprise import UserSession
from app.schemas.common import PageMeta
from app.schemas.enterprise import RevokeIn, SessionListOut, SessionOut
from app.services.authorization import MemberContext

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _to_out(row: UserSession) -> SessionOut:
    return SessionOut(
        id=str(row.id),
        user_id=str(row.user_id),
        ip_address=row.ip_address,
        user_agent=(row.user_agent or "")[:200],
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at,
        revoked_reason=row.revoked_reason,
        is_live=row.is_live,
    )


@router.get("/me", response_model=SessionListOut)
async def list_own_sessions(
    session: DbSession,
    actor: CurrentUser,
    include_revoked: bool = Query(default=False),
):
    query = select(UserSession).where(UserSession.user_id == actor.id)
    if not include_revoked:
        query = query.where(UserSession.revoked_at.is_(None))
    rows = (
        (await session.execute(query.order_by(UserSession.created_at.desc())))
        .scalars().all()
    )
    return SessionListOut(
        data=[_to_out(r) for r in rows],
        meta=PageMeta(page=1, page_size=len(rows), total=len(rows)),
    )


@router.post("/revoke-all", response_model=SessionListOut)
async def revoke_all_sessions(
    session: DbSession,
    actor: CurrentUser,
    request: Request,
    ctx: MemberContext = Depends(require_context("sessions.revoke")),
    user_id: uuid.UUID | None = Query(default=None),
):
    """Revoke every live session of a user (defaults to the caller)."""
    target = user_id or actor.id
    if target != actor.id and not ctx.has("sessions.revoke"):
        raise PermissionDeniedError("You cannot revoke these sessions")
    rows = (
        (await session.execute(
            select(UserSession).where(
                UserSession.user_id == target, UserSession.revoked_at.is_(None)
            )
        )).scalars().all()
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = "REVOKED_ALL"
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="session.revoked_all",
        actor_user_id=actor.id,
        resource_type="user",
        resource_id=str(target),
        ip_address=get_client_ip(request),
        metadata={"count": len(rows)},
    )
    return SessionListOut(
        data=[_to_out(r) for r in rows],
        meta=PageMeta(page=1, page_size=len(rows), total=len(rows)),
    )


@router.get("", response_model=SessionListOut)
async def list_sessions(
    session: DbSession,
    actor: CurrentUser,
    ctx: MemberContext = Depends(require_context("sessions.view")),
    user_id: uuid.UUID | None = Query(default=None),
    include_revoked: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    query = select(UserSession)
    count_q = select(func.count()).select_from(UserSession)
    if user_id is not None:
        query = query.where(UserSession.user_id == user_id)
        count_q = count_q.where(UserSession.user_id == user_id)
    if not include_revoked:
        query = query.where(UserSession.revoked_at.is_(None))
    total = int(await session.scalar(count_q) or 0)
    rows = (
        (await session.execute(query.order_by(UserSession.created_at.desc())
                               .offset((page - 1) * page_size).limit(page_size)))
        .scalars().all()
    )
    return SessionListOut(
        data=[_to_out(r) for r in rows],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


@router.post("/{session_id}/revoke", response_model=SessionListOut)
async def revoke_session(
    session_id: uuid.UUID,
    session: DbSession,
    actor: CurrentUser,
    request: Request,
    ctx: MemberContext = Depends(require_context("sessions.revoke")),
    payload: RevokeIn | None = None,
):
    row = await session.get(UserSession, session_id)
    if row is None:
        raise NotFoundError("Session not found")
    if row.user_id != actor.id and not ctx.has("sessions.revoke"):
        raise PermissionDeniedError("You cannot revoke this session")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        row.revoked_reason = (payload.reason if payload else None) or "REVOKED_BY_ADMIN"
        # Phase 11 §25: notify the session owner about the security event
        if row.user_id != actor.id:
            from app.services import notifications as notification_service

            await notification_service.emit(
                session,
                user_id=row.user_id,
                organization_id=ctx.organization_id,
                type="SECURITY",
                title="A session of yours was revoked",
                body="An administrator revoked one of your active sessions.",
                resource_type="session",
                resource_id=str(row.id),
            )
        await session.commit()
        await request.app.state.audit.log(
            session,
            action="session.revoked",
            actor_user_id=actor.id,
            resource_type="session",
            resource_id=str(row.id),
            ip_address=get_client_ip(request),
            metadata={"target_user_id": str(row.user_id)},
        )
    return SessionListOut(data=[_to_out(row)], meta=PageMeta(page=1, page_size=1, total=1))
