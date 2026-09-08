"""API key management (Phase 11 §22).

- plaintext key returned EXACTLY ONCE at creation; only SHA-256 stored
- scopes allowlisted (API_KEY_SCOPES); keys never grant super-admin
- keys inherit the owner's active status and organization
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.api.deps import DbSession, get_client_ip, require_context
from app.core.errors import NotFoundError, ValidationError
from app.core.security import hash_token_secret, new_api_key_secret
from app.models.enterprise import API_KEY_SCOPES, ApiKey
from app.schemas.common import PageMeta
from app.schemas.enterprise import (
    ApiKeyCreatedOut,
    ApiKeyCreate,
    ApiKeyListOut,
    ApiKeyOut,
)
from app.services.authorization import MemberContext

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _to_out(key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(key.id),
        organization_id=str(key.organization_id),
        name=key.name,
        prefix=key.prefix,
        scopes=key.scopes or [],
        created_by=str(key.created_by) if key.created_by else None,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        is_live=key.is_live,
        created_at=key.created_at,
    )


@router.get("", response_model=ApiKeyListOut)
async def list_api_keys(
    session: DbSession,
    ctx: MemberContext = Depends(require_context("apikeys.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    base = select(ApiKey).where(ApiKey.organization_id == ctx.organization_id)
    total = int(
        await session.scalar(
            select(func.count()).select_from(ApiKey)
            .where(ApiKey.organization_id == ctx.organization_id)
        )
        or 0
    )
    rows = (
        (await session.execute(base.order_by(ApiKey.created_at.desc())
                               .offset((page - 1) * page_size).limit(page_size)))
        .scalars().all()
    )
    return ApiKeyListOut(data=[_to_out(k) for k in rows],
                         meta=PageMeta(page=page, page_size=page_size, total=total))


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    payload: ApiKeyCreate,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("apikeys.create")),
):
    limiter = request.app.state.apikey_limiter
    if not limiter.check(str(ctx.user.id)):
        from app.core.errors import RateLimitedError

        raise RateLimitedError("Too many API keys created. Try again shortly.")

    unknown = [s for s in payload.scopes if s not in API_KEY_SCOPES]
    if unknown:
        raise ValidationError(f"Unknown scope(s): {sorted(set(unknown))}")
    scopes = list(dict.fromkeys(payload.scopes))

    full_key, prefix, secret = new_api_key_secret()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    key = ApiKey(
        organization_id=ctx.organization_id,
        name=payload.name.strip(),
        prefix=prefix,
        key_hash=hash_token_secret(secret),
        scopes=scopes,
        created_by=ctx.user.id,
        expires_at=expires_at,
    )
    session.add(key)
    await session.commit()

    await request.app.state.audit.log(
        session,
        action="apikey.created",
        actor_user_id=ctx.user.id,
        resource_type="api_key",
        resource_id=str(key.id),
        ip_address=get_client_ip(request),
        metadata={"name": key.name, "scopes": scopes},  # plaintext NEVER logged
    )
    return ApiKeyCreatedOut(data=_to_out(key), api_key=full_key)


@router.post("/{key_id}/revoke", response_model=ApiKeyListOut)
async def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("apikeys.revoke")),
):
    key = await session.get(ApiKey, key_id)
    if key is None or key.organization_id != ctx.organization_id:
        raise NotFoundError("API key not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        # Phase 11 §25: notify the key owner about the security event
        if key.created_by and key.created_by != ctx.user.id:
            from app.services import notifications as notification_service

            await notification_service.emit(
                session,
                user_id=key.created_by,
                organization_id=ctx.organization_id,
                type="SECURITY",
                title="An API key of yours was revoked",
                resource_type="api_key",
                resource_id=str(key.id),
            )
        await session.commit()
        await request.app.state.audit.log(
            session,
            action="apikey.revoked",
            actor_user_id=ctx.user.id,
            resource_type="api_key",
            resource_id=str(key.id),
            ip_address=get_client_ip(request),
        )
    return ApiKeyListOut(data=[_to_out(key)], meta=PageMeta(page=1, page_size=1, total=1))
