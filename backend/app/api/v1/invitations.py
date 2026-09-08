"""Invitation system (Phase 11 §5).

Security properties:
- token: 32 random bytes (urlsafe) — plaintext shown ONCE to the inviting admin
- storage: SHA-256(token_hash) only; DB logs never contain the plaintext token
- one-time use (accepted_at set atomically), expiry, revocation, replay-proof
- rate limited per actor (SlidingWindowRateLimiter via app.state.invite_limiter)
- acceptance: blocked for already-active users with the same email, sets the
  user status to ACTIVE and grants the invited role codes / team membership
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.api.deps import DbSession, get_client_ip, require_context
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, RateLimitedError
from app.core.security import hash_password, hash_token_secret
from app.models.enterprise import Invitation, Organization, Team, TeamMember, utc_aware
from app.models.rbac import Role, user_roles
from app.models.user import User
from app.schemas.common import PageMeta
from app.schemas.enterprise import (
    InvitationAcceptIn,
    InvitationCreatedOut,
    InvitationCreate,
    InvitationListOut,
    InvitationOut,
)
from app.services.authorization import MemberContext
from app.services import authorization as authz

router = APIRouter(prefix="/invitations", tags=["invitations"])

#: public signup page route used to build invite_url (UI phase may override)
INVITE_ACCEPT_PATH = "/invite/accept"


def _to_out(inv: Invitation) -> InvitationOut:
    return InvitationOut(
        id=str(inv.id),
        organization_id=str(inv.organization_id),
        email=inv.email,
        role_codes=inv.role_codes or [],
        team_id=str(inv.team_id) if inv.team_id else None,
        status=inv.status,
        expires_at=inv.expires_at,
        invited_by=str(inv.invited_by) if inv.invited_by else None,
        accepted_at=inv.accepted_at,
        revoked_at=inv.revoked_at,
        created_at=inv.created_at,
    )


@router.post("/accept", status_code=200)
async def accept_invitation(
    payload: InvitationAcceptIn,
    request: Request,
    session: DbSession,
):
    """Public endpoint: exchange a one-time token for account activation."""
    token_hash = hash_token_secret(payload.token.strip())
    inv = await session.scalar(select(Invitation).where(Invitation.token_hash == token_hash))
    if inv is None:
        raise NotFoundError("Invitation not found")
    if inv.revoked_at is not None:
        raise PermissionDeniedError("This invitation has been revoked")
    if inv.accepted_at is not None:
        raise PermissionDeniedError("This invitation has already been used")
    if inv.expires_at is not None and utc_aware(inv.expires_at) <= datetime.now(timezone.utc):
        raise PermissionDeniedError("This invitation has expired")

    organization = await session.get(Organization, inv.organization_id)
    if organization is None or organization.status != "ACTIVE":
        raise PermissionDeniedError("This organization is not active")

    email = inv.email.strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            status="ACTIVE",
        )
        session.add(user)
        await session.flush()
    else:
        if user.effective_status not in ("INVITED",):
            raise ConflictError("An active user with this email already exists")
        user.password_hash = hash_password(payload.password)
        if payload.full_name:
            user.full_name = payload.full_name
        user.status = "ACTIVE"
        user.is_active = True

    # grant invited roles (validated against the real catalog)
    role_codes = [c for c in (inv.role_codes or []) if c]
    if role_codes:
        roles = (await session.execute(select(Role).where(Role.code.in_(role_codes)))).scalars().all()
        for role in roles:
            await session.execute(
                user_roles.insert().values(user_id=user.id, role_id=role.id)
            )

    # attach to the organization + optional team
    await authz.ensure_membership(session, user, organization)
    if inv.team_id:
        team = await session.get(Team, inv.team_id)
        if team is not None and team.organization_id == inv.organization_id:
            existing = await session.scalar(
                select(TeamMember).where(
                    TeamMember.team_id == team.id, TeamMember.user_id == user.id
                )
            )
            if existing is None:
                session.add(TeamMember(team_id=team.id, user_id=user.id))

    inv.accepted_at = datetime.now(timezone.utc)
    inv.accepted_by_user_id = user.id
    # Phase 11 §25: notify the inviter that their invitation was accepted
    if inv.invited_by:
        from app.services import notifications as notification_service

        await notification_service.emit(
            session,
            user_id=inv.invited_by,
            organization_id=organization.id,
            type="INVITATION",
            title=f"{email} joined {organization.name}",
            resource_type="invitation",
            resource_id=str(inv.id),
        )
    await session.commit()

    await request.app.state.audit.log(
        session,
        action="invitation.accepted",
        actor_user_id=user.id,
        resource_type="invitation",
        resource_id=str(inv.id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500],
        metadata={"email": email, "organization_id": str(organization.id)},
    )
    return {"success": True, "data": {"email": email, "organization": organization.name}}


@router.get("", response_model=InvitationListOut)
async def list_invitations(
    session: DbSession,
    ctx: MemberContext = Depends(require_context("invitations.view")),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    query = select(Invitation).where(Invitation.organization_id == ctx.organization_id)
    total = int(
        await session.scalar(select(func.count()).select_from(Invitation)
                             .where(Invitation.organization_id == ctx.organization_id))
        or 0
    )
    rows = (
        (await session.execute(query.order_by(Invitation.created_at.desc())
                               .offset((page - 1) * page_size).limit(page_size)))
        .scalars().all()
    )
    items = [_to_out(i) for i in rows]
    if status:
        items = [i for i in items if i.status == status.upper()]
    return InvitationListOut(
        data=items, meta=PageMeta(page=page, page_size=page_size, total=total)
    )


@router.post("", response_model=InvitationCreatedOut, status_code=201)
async def create_invitation(
    payload: InvitationCreate,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("invitations.create")),
):
    limiter = request.app.state.invite_limiter
    if not limiter.check(str(ctx.user.id)):
        raise RateLimitedError("Too many invitations created. Try again shortly.")

    email = payload.email.strip().lower()
    existing_user = await session.scalar(select(User).where(User.email == email))
    if existing_user is not None and existing_user.effective_status == "ACTIVE":
        raise ConflictError("An active user with this email already exists")

    role_codes: list[str] = []
    if payload.role_codes:
        wanted = list(dict.fromkeys(payload.role_codes))
        found = {
            r.code
            for r in (
                await session.execute(select(Role).where(Role.code.in_(wanted)))
            ).scalars().all()
        }
        missing = set(wanted) - found
        if missing:
            raise NotFoundError(f"Unknown role(s): {sorted(missing)}")
        # cannot grant SUPER_ADMIN via invitation
        if "SUPER_ADMIN" in wanted:
            raise PermissionDeniedError("SUPER_ADMIN cannot be granted via invitation")
        role_codes = wanted

    team_id: uuid.UUID | None = None
    if payload.team_id:
        team = await session.get(Team, uuid.UUID(payload.team_id))
        if team is None or team.organization_id != ctx.organization_id:
            raise NotFoundError("Team not found")
        team_id = team.id

    settings = request.app.state.settings
    expiry_hours = getattr(settings, "QBIT_INVITATION_EXPIRY_HOURS", 168)
    plaintext = secrets.token_urlsafe(32)
    inv = Invitation(
        organization_id=ctx.organization_id,
        email=email,
        role_codes=role_codes,
        team_id=team_id,
        token_hash=hash_token_secret(plaintext),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
        invited_by=ctx.user.id,
    )
    session.add(inv)
    # Phase 11 §25: if the invitee already has an (inactive) account, notify
    # them in-app that an invitation is waiting
    if existing_user is not None:
        from app.services import notifications as notification_service

        await notification_service.emit(
            session,
            user_id=existing_user.id,
            organization_id=ctx.organization_id,
            type="INVITATION",
            title=f"You have been invited to join {ctx.organization.name}",
            body="Accept the invitation from the link provided by your administrator.",
            resource_type="invitation",
            resource_id=str(inv.id),
        )
    await session.commit()

    await request.app.state.audit.log(
        session,
        action="invitation.created",
        actor_user_id=ctx.user.id,
        resource_type="invitation",
        resource_id=str(inv.id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500],
        metadata={"email": email, "roles": role_codes},  # token NEVER logged
    )
    return InvitationCreatedOut(
        data=_to_out(inv),
        invite_token=plaintext,
        invite_url=f"{INVITE_ACCEPT_PATH}?token={plaintext}",
    )


@router.post("/{invitation_id}/revoke", response_model=InvitationListOut)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("invitations.revoke")),
):
    inv = await session.get(Invitation, invitation_id)
    if inv is None or inv.organization_id != ctx.organization_id:
        raise NotFoundError("Invitation not found")
    if inv.accepted_at is None and inv.revoked_at is None:
        inv.revoked_at = datetime.now(timezone.utc)
        await session.commit()
        await request.app.state.audit.log(
            session,
            action="invitation.revoked",
            actor_user_id=ctx.user.id,
            resource_type="invitation",
            resource_id=str(inv.id),
            ip_address=get_client_ip(request),
        )
    return InvitationListOut(
        data=[_to_out(inv)], meta=PageMeta(page=1, page_size=1, total=1)
    )
