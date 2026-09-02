"""API dependencies: DB session, storage, auth principal, RBAC enforcement (Brief §10).

Every protected backend endpoint verifies permissions here — the UI is never the
security boundary (Brief §10).
"""

from __future__ import annotations

import uuid
from typing import Annotated, AsyncIterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AuthFailedError, PermissionDeniedError, UnauthorizedError
from app.core.security import decode_access_token
from app.db.session import DatabaseManager
from app.models.user import User
from app.redis_client import RedisManager
from app.services.audit import AuditService
from app.services.files import FileService
from app.services.storage import StorageService

bearer_scheme = HTTPBearer(auto_error=False)


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
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:500]


async def get_current_user(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    settings: Settings = request.app.state.settings  # DI via app.state (test-isolated)
    if credentials is None:
        raise UnauthorizedError()
    payload = decode_access_token(
        credentials.credentials, secret_key=settings.QBIT_SECRET_KEY
    )
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

    request.state.user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_permission(code: str):
    """Dependency factory: 401 without a valid token, 403 without the permission."""

    async def _checker(
        request: Request,
        session: DbSession,
        user: CurrentUser,
    ) -> User:
        from app.services import rbac as rbac_service

        cached = getattr(request.state, "permissions", None)
        if cached is None:
            cached = await rbac_service.load_user_permissions(session, user.id)
            request.state.permissions = cached
        if code not in cached:
            raise PermissionDeniedError(f"Missing required permission: {code}")
        return user

    return _checker


Principal = Annotated[User, Depends(require_permission)]
