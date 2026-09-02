"""Auth endpoints — foundation (Brief §9, §10).

Login issues short-lived HS256 bearer tokens signed with QBIT_SECRET_KEY.
Server-side sessions/revocation arrive in the dedicated auth phase (documented
in docs/25 — no fake functionality).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import (
    CurrentUser,
    DbSession,
    get_client_ip,
    get_user_agent,
)
from app.core.errors import AuthFailedError, PermissionDeniedError, RateLimitedError
from app.core.logging import get_logger, log_with
from app.core.security import (
    create_access_token,
    password_needs_rehash,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import LoginRequest, LogoutResponse, TokenResponse
from app.services import rbac as rbac_service

logger = get_logger("qbit.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: DbSession,
):
    settings = request.app.state.settings  # DI via app.state (test-isolated)
    ip = get_client_ip(request)
    limiter = request.app.state.login_limiter
    if not limiter.check(ip):
        raise RateLimitedError("Too many login attempts. Try again shortly.")

    email = payload.email.lower()
    user = await session.scalar(select(User).where(User.email == email))

    # Constant-shape response: verify password even when user is missing.
    if user is None:
        verify_password(payload.password, "$argon2id$invalidplaceholderhashvalue")
        await request.app.state.audit.log(
            session,
            action="user.login_failed",
            resource_type="user",
            resource_id=email,
            ip_address=ip,
            user_agent=get_user_agent(request),
            metadata={"reason": "unknown_email"},
        )
        raise AuthFailedError()

    if not verify_password(payload.password, user.password_hash):
        await request.app.state.audit.log(
            session,
            action="user.login_failed",
            resource_type="user",
            resource_id=str(user.id),
            ip_address=ip,
            user_agent=get_user_agent(request),
            metadata={"reason": "bad_password"},
        )
        raise AuthFailedError()

    if not user.is_active:
        raise PermissionDeniedError("Account is deactivated")

    token, expires_at = create_access_token(
        subject=str(user.id),
        secret_key=settings.QBIT_SECRET_KEY,
        ttl_minutes=settings.QBIT_SESSION_TTL_MINUTES,
    )
    user.last_login_at = datetime.now(timezone.utc)
    if password_needs_rehash(user.password_hash):
        from app.core.security import hash_password

        user.password_hash = hash_password(payload.password)
    await session.commit()

    await request.app.state.audit.log(
        session,
        action="user.login",
        actor_user_id=user.id,
        resource_type="user",
        resource_id=str(user.id),
        ip_address=ip,
        user_agent=get_user_agent(request),
    )
    log_with(logger, 20, "User logged in", user_id=str(user.id))

    return TokenResponse(
        access_token=token,
        expires_at=expires_at.isoformat(),
    )


@router.get("/me")
async def me(request: Request, user: CurrentUser, session: DbSession):
    from app.core import request_context

    request_context.set_user_id(str(user.id))
    permissions = await rbac_service.load_user_permissions(session, user.id)
    return {
        "success": True,
        "data": {**user.to_public_dict(), "permissions": sorted(permissions)},
    }


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    user: CurrentUser,
    session: DbSession,
):
    """Audit-only logout: stateless tokens are discarded client-side.
    Token revocation arrives with server-side sessions (docs/25 — honest scope)."""
    await request.app.state.audit.log(
        session,
        action="user.logout",
        actor_user_id=user.id,
        resource_type="user",
        resource_id=str(user.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return LogoutResponse(message="Token discarded client-side; logout audited")
