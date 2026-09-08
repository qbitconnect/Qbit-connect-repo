"""API dependencies: DB session, storage, auth principal, RBAC enforcement.

Phase 11 additions (all layered, nothing removed):
- server-side session validation (JWT `jti` ↔ sessions table; legacy tokens
  issued before Phase 11 remain valid unless `tokens_revoked_before` excludes them)
- organization membership context (AuthorizationService.resolve_context) —
  every protected endpoint inherits org isolation automatically
- API-key bearer support (`qbit_...` keys resolved by prefix, hash-verified,
  scope-limited to the allowlist ∩ owner permissions)

Every protected backend endpoint verifies permissions here — the UI is never the
security boundary.
"""

from __future__ import annotations

import contextvars
import uuid
from datetime import datetime, timezone
from typing import Annotated, AsyncIterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AuthFailedError, PermissionDeniedError, UnauthorizedError
from app.core.security import decode_access_token
from app.db.session import DatabaseManager
from app.models.enterprise import ApiKey, API_KEY_SCOPES, API_KEY_PREFIX, UserSession, utc_aware

#: Phase 11 §23: API-key scopes are PRODUCT-level intents mapped onto the
#: platform's capability codes. A key's effective permissions are this mapping
#: ∩ the OWNER's permissions — keys can never grant beyond their owner and
#: never auto-grant admin capabilities.
API_KEY_SCOPE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "leads.read": ("leads.view",),
    "leads.write": ("leads.create", "leads.edit", "leads.import", "leads.export"),
    "campaigns.read": ("campaigns.view",),
    "campaigns.write": ("campaigns.create", "campaigns.edit", "campaigns.validate"),
    "analytics.read": ("dashboard.view", "exports.view"),
    "scraping.run": ("scraping.view", "scraping.run"),
    "files.read": ("files.view", "exports.download"),
    "connections.read": ("connections.view",),
}
from app.models.user import User
from app.redis_client import RedisManager
from app.services.audit import AuditService
from app.services.files import FileService
from app.services.storage import StorageService

bearer_scheme = HTTPBearer(auto_error=False)

#: Active request (read by AuthorizationService for X-Organization-Id routing).
current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
    "qbit_current_request", default=None
)


def get_db_manager(request: Request) -> DatabaseManager:
    return request.app.state.db


def get_redis_manager(request: Request) -> RedisManager:
    return request.app.state.redis


def get_storage(request: Request) -> StorageService:
    return request.app.state.storage


def get_audit_service(request: Request) -> AuditService:
    return request.app.state.audit


def get_file_service(request: Request) -> FileService:
    return request.app.state.files


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    db: DatabaseManager = request.app.state.db
    async with db.session() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]
StorageDep = Annotated[StorageService, Depends(get_storage)]
AuditDep = Annotated[AuditService, Depends(get_audit_service)]
FileServiceDep = Annotated[FileService, Depends(get_file_service)]


def get_client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting + audit trails.

    Phase 12 hardening: header values are only trusted from the LAST XFF
    entry (the one appended by OUR reverse proxy) or X-Real-IP (set by the
    proxy, overriding client-supplied values). Earlier XFF entries are
    client-controlled — trusting them let an attacker rotate spoofed IPs
    and bypass the per-IP login limiter. When the app is reached directly
    (dev mode), the socket peer address is used.
    """
    real = request.headers.get("x-real-ip")
    if real and real.strip():
        return real.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"


def get_user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:500]


async def _resolve_api_key(
    request: Request, session: AsyncSession, token: str, settings: Settings
) -> tuple[User, ApiKey]:
    """Resolve a `qbit_...` API key to (owner_user, key). Scopes are attached
    to request.state and enforced by require_permission; keys never grant
    anything their owner could not do, and never grant admin privileges."""
    from app.core.security import hash_token_secret

    raw = token.strip()
    if len(raw) < len(API_KEY_PREFIX) + 2 or not raw.startswith(API_KEY_PREFIX + "_"):
        raise UnauthorizedError("Invalid API key format")
    # format: qbit_<prefix8>_<secret>
    parts = raw.split("_", 2)
    if len(parts) != 3 or len(parts[1]) != 8:
        raise UnauthorizedError("Invalid API key format")
    prefix = f"{parts[0]}_{parts[1]}"
    key_row = await session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if key_row is None:
        raise AuthFailedError("Invalid API key")
    if not key_row.is_live:
        raise PermissionDeniedError("API key is revoked or expired")
    if key_row.key_hash != hash_token_secret(parts[2]):
        raise AuthFailedError("Invalid API key")
    owner = await session.get(User, key_row.created_by) if key_row.created_by else None
    if owner is None:
        raise AuthFailedError("API key owner no longer exists")
    if not owner.is_active:
        raise PermissionDeniedError("Account is deactivated")
    request.state.api_key = key_row
    # effective permissions = mapped scopes ∩ owner permissions
    from app.services import rbac as rbac_service

    owner_permissions = await rbac_service.load_user_permissions(session, owner.id)
    effective: set[str] = set()
    for scope in key_row.scopes or []:
        if scope in API_KEY_SCOPES:
            effective.update(API_KEY_SCOPE_PERMISSIONS.get(scope, ()))
    request.state.api_key_scopes = effective & owner_permissions
    return owner, key_row


async def get_current_user(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    settings: Settings = request.app.state.settings  # DI via app.state (test-isolated)
    current_request.set(request)
    if credentials is None:
        raise UnauthorizedError()
    token = credentials.credentials

    api_scopes = None
    if token.startswith(API_KEY_PREFIX + "_"):
        user, _key = await _resolve_api_key(request, session, token, settings)
        api_scopes = request.state.api_key_scopes
    else:
        payload = decode_access_token(token, secret_key=settings.QBIT_SECRET_KEY)
        user_id = payload.get("sub")
        if not user_id:
            raise UnauthorizedError("Invalid token subject")
        try:
            uid = uuid.UUID(user_id)
        except ValueError as exc:
            raise UnauthorizedError("Invalid token subject") from exc

        user = await session.get(User, uid)
        if user is None:
            raise AuthFailedError("Account no longer exists")
        if not user.is_active:
            raise PermissionDeniedError("Account is deactivated")

        # --- Phase 11: session/revocation checks ------------------------------
        revoked_before = user.tokens_revoked_before
        if revoked_before is not None:
            issued = payload.get("iat")
            if isinstance(issued, (int, float)) and datetime.fromtimestamp(
                issued, tz=timezone.utc
            ) < utc_aware(revoked_before):
                raise UnauthorizedError("Session has been revoked")

        jti = payload.get("jti")
        if jti:
            session_row = await session.scalar(
                select(UserSession).where(UserSession.jti == jti)
            )
            if session_row is not None:
                if not session_row.is_live:
                    raise UnauthorizedError("Session has been revoked")
                # throttle last-seen writes (>=60s since previous)
                now = datetime.now(timezone.utc)
                last = utc_aware(session_row.last_seen_at)
                if last is None or (now - last).total_seconds() >= 60:
                    session_row.last_seen_at = now
                    await session.commit()
            # tokens issued before Phase 11 (no session row) remain valid —
            # governed by tokens_revoked_before; logins from Phase 11 always
            # create rows.

    if api_scopes is not None:
        request.state.permissions = api_scopes
    request.state.user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_permission(code: str):
    """Dependency factory: 401 without a valid token, 403 without the permission.

    Phase 11: also resolves the organization membership context once per request
    and stores it on request.state.member_context for downstream use.
    """

    async def _checker(
        request: Request,
        session: DbSession,
        user: CurrentUser,
    ):
        from app.services import authorization as authz
        from app.services import rbac as rbac_service

        cached = getattr(request.state, "permissions", None)
        if cached is None:
            cached = await rbac_service.load_user_permissions(session, user.id)
            request.state.permissions = cached

        ctx = getattr(request.state, "member_context", None)
        if ctx is None:
            # API-key principals bypass org membership resolution of a *session*
            # user: their organization comes from the key row.
            api_key = getattr(request.state, "api_key", None)
            if api_key is not None:
                ctx = await authz.resolve_context_for_organization(
                    session, user, api_key.organization_id, cached
                )
            else:
                ctx = await authz.resolve_context(session, user, cached)
            request.state.member_context = ctx

        if code not in cached:
            raise PermissionDeniedError(f"Missing required permission: {code}")
        return user

    return _checker


Principal = Annotated[User, Depends(require_permission)]


def require_context(code: str):
    """Dependency factory returning the resolved MemberContext AFTER enforcing
    `code` (ordering guaranteed by explicit chaining)."""

    async def _dep(
        request: Request,
        session: DbSession,
        user: CurrentUser,
    ):
        await require_permission(code)(request=request, session=session, user=user)
        return request.state.member_context

    return _dep


def optional_context(request: Request):
    """Best-effort context accessor for handlers that already declared a
    require_permission dependency earlier in the same request."""
    return getattr(request.state, "member_context", None)
