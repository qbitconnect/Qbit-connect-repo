"""Sending account API (Phase 5 §4, §5, §29).

Security rules:
- provider API keys/tokens are NEVER accepted or stored in Phase 5 — the
  credential vault arrives in a later phase (architecture doc 17); only
  non-secret configuration is persisted, secret-like keys are rejected
- responses expose display-safe fields only (to_public_dict omits config)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditDep, DbSession, require_permission
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import SECRET_KEYS
from app.models.marketing import (
    AccountHealth,
    AccountStatus,
    SendingAccount,
)
from app.schemas.marketing import SendingAccountCreate, SendingAccountUpdate
from app.services.marketing.channels import get_channel
from app.services.marketing.providers import MarketingProviderRegistry

router = APIRouter(prefix="/sending-accounts", tags=["sending-accounts"])

VALID_STATUSES = tuple(s.value for s in AccountStatus)


def _registry(request: Request) -> MarketingProviderRegistry:
    registry = getattr(request.app.state, "marketing_providers", None)
    if registry is None:
        from app.services.marketing.providers import build_provider_registry

        registry = build_provider_registry(request.app.state.settings)
        request.app.state.marketing_providers = registry
    return registry


def _reject_secrets(config: dict) -> None:
    for key in config or {}:
        if str(key).lower() in SECRET_KEYS and config.get(key):
            raise ValidationError(
                f"Refusing to store secret-like field '{key}' — the credential vault "
                "arrives in a later phase; reference credentials by name instead"
            )


def _page(items: list, total: int, page: int, page_size: int) -> dict:
    return {
        "success": True,
        "data": {
            "items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, -(-total // page_size)) if total else 1,
        },
    }


async def _get_account(session: AsyncSession, account_id: uuid.UUID) -> SendingAccount:
    account = await session.get(SendingAccount, account_id)
    if account is None:
        raise NotFoundError("Sending account not found")
    return account


@router.get("")
async def list_accounts(
    session: DbSession,
    request: Request,
    _user=Depends(require_permission("sending_accounts.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    channel: str | None = Query(default=None, max_length=20),
):
    query = select(SendingAccount)
    if channel:
        query = query.where(SendingAccount.channel == channel.upper())
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.execute(
        query.order_by(SendingAccount.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = []
    registry = _registry(request)
    for account in rows.scalars().all():
        data = account.to_public_dict()
        provider = registry.get(account.provider)
        data["provider_available"] = provider is not None
        data["provider_test_only"] = bool(provider.test_only) if provider else False
        data["provider_configured"] = bool((account.config_metadata or {}).get("configured"))
        items.append(data)
    return _page(items, int(total or 0), page, page_size)


@router.post("", status_code=201)
async def create_account(
    payload: SendingAccountCreate,
    session: DbSession,
    request: Request,
    audit: AuditDep,
    user=Depends(require_permission("sending_accounts.manage")),
):
    channel = (payload.channel or "").upper()
    if get_channel(channel) is None:
        raise ValidationError(f"Unknown channel: {channel}")
    if get_channel(channel) and payload.provider not in get_channel(channel).providers:
        raise ValidationError(
            f"Provider '{payload.provider}' cannot serve channel {channel}"
        )
    registry = _registry(request)
    provider = registry.get(payload.provider)
    if provider is None:
        raise ValidationError(f"Unknown provider: {payload.provider}")
    if provider.test_only and request.app.state.settings.QBIT_ENV == "production":
        raise ValidationError("MOCK provider accounts can never be created in production")
    _reject_secrets(payload.config_metadata)
    account = SendingAccount(
        name=" ".join(payload.name.split())[:150],
        channel=channel,
        provider=payload.provider,
        identifier=payload.identifier[:300],
        display_identifier=(payload.display_identifier or payload.identifier)[:300],
        status=AccountStatus.PENDING,
        capabilities=payload.capabilities or {},
        config_metadata=payload.config_metadata or {},
        health_status=AccountHealth.UNKNOWN,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    await audit.log(session, action="sending_account.created", resource_type="sending_account",
                    resource_id=str(account.id), actor_user_id=user.id,
                    metadata={"provider": account.provider, "channel": account.channel})
    data = account.to_public_dict()
    data["provider_available"] = True
    data["provider_test_only"] = provider.test_only
    data["provider_configured"] = bool((account.config_metadata or {}).get("configured"))
    return {"success": True, "data": data}


@router.get("/{account_id}")
async def get_account(
    account_id: uuid.UUID,
    session: DbSession,
    request: Request,
    _user=Depends(require_permission("sending_accounts.view")),
):
    account = await _get_account(session, account_id)
    registry = _registry(request)
    provider = registry.get(account.provider)
    data = account.to_public_dict()
    data["provider_available"] = provider is not None
    data["provider_test_only"] = bool(provider.test_only) if provider else False
    data["provider_configured"] = bool((account.config_metadata or {}).get("configured"))
    return {"success": True, "data": data}


@router.patch("/{account_id}")
async def update_account(
    account_id: uuid.UUID,
    payload: SendingAccountUpdate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("sending_accounts.manage")),
):
    account = await _get_account(session, account_id)
    if payload.name is not None:
        clean = " ".join(payload.name.split())[:150]
        if not clean:
            raise ValidationError("Account name must not be empty")
        account.name = clean
    if payload.display_identifier is not None:
        account.display_identifier = payload.display_identifier[:300] or None
    if payload.capabilities is not None:
        account.capabilities = payload.capabilities
    if payload.config_metadata is not None:
        _reject_secrets(payload.config_metadata)
        account.config_metadata = payload.config_metadata
    if payload.status is not None:
        status = payload.status.upper()
        if status not in VALID_STATUSES:
            raise ValidationError(f"status must be one of: {', '.join(VALID_STATUSES)}")
        account.status = status
    await session.commit()
    await session.refresh(account)
    await audit.log(session, action="sending_account.updated", resource_type="sending_account",
                    resource_id=str(account.id), actor_user_id=user.id)
    return {"success": True, "data": account.to_public_dict()}


@router.post("/{account_id}/health")
async def account_health(
    account_id: uuid.UUID,
    session: DbSession,
    request: Request,
    audit: AuditDep,
    user=Depends(require_permission("sending_accounts.manage")),
):
    """Provider health probe (§3 health_check) — honest UNKNOWN when the
    provider is an unconfigured interface."""
    account = await _get_account(session, account_id)
    provider = _registry(request).get(account.provider)
    if provider is None:
        result = {"health": "UNKNOWN", "detail": "Provider not registered"}
    else:
        try:
            result = await provider.health_check(account.config_metadata or {})
        except Exception:  # noqa: BLE001 — health probes never raise
            result = {"health": "UNHEALTHY", "detail": "Provider probe crashed"}
    account.health_status = result.get("health", "UNKNOWN")
    account.last_health_check = datetime.now(timezone.utc)
    if result.get("health") == "HEALTHY" and account.status == AccountStatus.PENDING:
        account.status = AccountStatus.ACTIVE
    await session.commit()
    await audit.log(session, action="sending_account.health_checked",
                    resource_type="sending_account", resource_id=str(account.id),
                    actor_user_id=user.id, metadata={"health": account.health_status})
    return {"success": True, "data": {"health": account.health_status, "detail": result}}
