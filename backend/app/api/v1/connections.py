"""Connections API (Phase 6 §36).

    GET    /api/v1/connections                       all connections by channel
    GET    /api/v1/connections/whatsapp              WhatsApp sending accounts
    POST   /api/v1/connections/whatsapp              create (PENDING until validated)
    GET    /api/v1/connections/whatsapp/{id}         detail (masked identifiers)
    PATCH  /api/v1/connections/whatsapp/{id}         update / rotate credentials
    DELETE /api/v1/connections/whatsapp/{id}         remove (+ credential row)
    POST   /api/v1/connections/whatsapp/{id}/validate        provider validation flow
    POST   /api/v1/connections/whatsapp/{id}/health          health check (§7)
    POST   /api/v1/connections/whatsapp/{id}/sync-templates  template sync (§9)
    GET    /api/v1/connections/whatsapp/{id}/templates       synced templates

Security (§4): credentials are WRITE-ONLY — accepted, encrypted at rest,
never returned, never logged. Responses expose masked phones and booleans
(has_credentials), never tokens. Every route is permission-checked server-side
(§37); audit records every mutation (§38) without credential values.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditDep, DbSession, require_permission
from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.marketing import AccountStatus, SendingAccount
from app.schemas.connections import (
    WhatsAppConnectionCreate,
    WhatsAppConnectionUpdate,
)
from app.services.marketing.connections import ConnectionService
from app.services.marketing.providers import MarketingProviderRegistry

logger = get_logger("qbit.marketing.connections_api")

router = APIRouter(prefix="/connections", tags=["connections"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _registry(request: Request) -> MarketingProviderRegistry:
    registry = getattr(request.app.state, "marketing_providers", None)
    if registry is None:
        from app.services.marketing.providers import build_provider_registry

        registry = build_provider_registry(_settings(request))
        request.app.state.marketing_providers = registry
    return registry


def _service(request: Request) -> ConnectionService:
    return ConnectionService(_settings(request))


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
        raise NotFoundError("Connection not found")
    return account


def _account_view(request: Request, account: SendingAccount) -> dict:
    """Display-safe account payload (§4): no config dump, no credentials."""
    registry = _registry(request)
    provider = registry.get(account.provider)
    data = account.to_public_dict()
    data["provider_available"] = provider is not None
    data["provider_test_only"] = bool(provider.test_only) if provider else False
    data["provider_configured"] = bool((account.config_metadata or {}).get("configured"))
    data["last_health_error"] = (account.config_metadata or {}).get("last_health_error")
    return data


# ------------------------------------------------------------------ overview
@router.get("")
async def list_connections(
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("connections.view")),
):
    """Connection overview grouped by channel (WhatsApp/Email/SMS sections)."""
    rows = (await session.execute(
        select(SendingAccount).order_by(SendingAccount.created_at.desc())
    )).scalars().all()
    grouped: dict[str, list] = {"WHATSAPP": [], "EMAIL": [], "SMS": []}
    for account in rows:
        bucket = grouped.setdefault((account.channel or "OTHER").upper(), [])
        bucket.append(_account_view(request, account))
    return {"success": True, "data": {"channels": grouped}}


# ------------------------------------------------------------- whatsapp CRUD
@router.get("/whatsapp")
async def list_whatsapp_connections(
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("connections.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    query = select(SendingAccount).where(SendingAccount.channel == "WHATSAPP")
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await session.execute(
        query.order_by(SendingAccount.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return _page([_account_view(request, a) for a in rows], int(total or 0), page, page_size)


@router.post("/whatsapp", status_code=201)
async def create_whatsapp_connection(
    payload: WhatsAppConnectionCreate,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("connections.create")),
):
    service = _service(request)
    account = await service.create_whatsapp_account(
        session,
        name=payload.name,
        provider=payload.provider or _settings(request).WHATSAPP_PROVIDER,
        phone_number_id=payload.phone_number_id,
        business_account_id=payload.business_account_id,
        credentials=payload.credentials.model_dump(exclude_none=True) if payload.credentials else None,
        capabilities=payload.capabilities,
        created_by=user.id,
    )
    await audit.log(session, action="whatsapp_account.created", resource_type="sending_account",
                    resource_id=str(account.id), actor_user_id=user.id,
                    metadata={"provider": account.provider, "has_credentials": bool(account.credential_ref)})
    return {"success": True, "data": _account_view(request, account)}


@router.get("/whatsapp/{account_id}")
async def get_whatsapp_connection(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("connections.view")),
):
    account = await _get_account(session, account_id)
    return {"success": True, "data": _account_view(request, account)}


@router.patch("/whatsapp/{account_id}")
async def update_whatsapp_connection(
    account_id: uuid.UUID,
    payload: WhatsAppConnectionUpdate,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("connections.edit")),
):
    account = await _get_account(session, account_id)
    service = _service(request)
    rotated = False

    if payload.name is not None:
        clean = " ".join(payload.name.split())[:150]
        if not clean:
            raise ValidationError("Account name must not be empty")
        account.name = clean
    if payload.phone_number_id is not None:
        account.phone_number_id = payload.phone_number_id.strip() or None
        if account.phone_number_id and account.identifier == account.name:
            account.identifier = account.phone_number_id
    if payload.business_account_id is not None:
        account.business_account_id = payload.business_account_id.strip() or None
    if payload.capabilities is not None:
        account.capabilities = {**(account.capabilities or {}), **payload.capabilities}
        account.config_metadata = {**(account.config_metadata or {}),
                                   "capabilities": account.capabilities}
    if payload.config_metadata is not None:
        merged = {**payload.config_metadata}
        # secret-like keys never pass through the update path
        from app.core.logging import SECRET_KEYS
        for key in list(merged):
            if str(key).lower() in SECRET_KEYS and merged.get(key):
                raise ValidationError(f"Refusing to store secret-like field '{key}'")
        account.config_metadata = {**(account.config_metadata or {}), **merged}
    if payload.credentials is not None:
        await service.update_credentials(
            session, account, payload.credentials.model_dump(exclude_none=True),
        )
        rotated = True
    if payload.status is not None:
        await service.set_status(session, account, payload.status)

    await session.commit()
    await session.refresh(account)
    await audit.log(session, action="whatsapp_account.updated", resource_type="sending_account",
                    resource_id=str(account.id), actor_user_id=user.id,
                    metadata={"credentials_rotated": rotated})
    return {"success": True, "data": _account_view(request, account)}


@router.delete("/whatsapp/{account_id}")
async def delete_whatsapp_connection(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("connections.delete")),
):
    account = await _get_account(session, account_id)
    account_id_str, name = str(account.id), account.name
    await _service(request).delete_account(session, account)
    await audit.log(session, action="whatsapp_account.removed", resource_type="sending_account",
                    resource_id=account_id_str, actor_user_id=user.id,
                    metadata={"name": name})
    return {"success": True, "data": {"removed": True, "id": account_id_str}}


# ---------------------------------------------------------- provider actions
@router.post("/whatsapp/{account_id}/validate")
async def validate_whatsapp_connection(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("connections.validate")),
):
    """§5 connection flow: credentials → phone → business account.
    ACTIVE only when every step passes; ERROR otherwise."""
    account = await _get_account(session, account_id)
    report = await _service(request).validate_account(session, account, registry=_registry(request))
    await audit.log(session, action="whatsapp_account.credential_validated",
                    resource_type="sending_account", resource_id=str(account.id),
                    actor_user_id=user.id,
                    metadata={"ok": bool(report.get("ok")), "status": account.status})
    data = _account_view(request, account)
    data["validation"] = report
    return {"success": True, "data": data}


@router.post("/whatsapp/{account_id}/health")
async def whatsapp_connection_health(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("connections.health")),
):
    """§7 health check: credentials, availability, configuration, quality."""
    account = await _get_account(session, account_id)
    result = await _service(request).health_check(session, account, registry=_registry(request))
    await audit.log(session, action="whatsapp_account.health_checked",
                    resource_type="sending_account", resource_id=str(account.id),
                    actor_user_id=user.id, metadata={"health": account.health_status})
    data = _account_view(request, account)
    data["health_detail"] = result
    return {"success": True, "data": data}


@router.post("/whatsapp/{account_id}/sync-templates")
async def sync_whatsapp_templates(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("connections.sync_templates")),
):
    """§9 pull the provider template catalog into QBIT templates."""
    account = await _get_account(session, account_id)
    summary = await _service(request).sync_templates(session, account, registry=_registry(request))
    await audit.log(session, action="whatsapp_account.templates_synced",
                    resource_type="sending_account", resource_id=str(account.id),
                    actor_user_id=user.id,
                    metadata={"created": summary.get("created"), "updated": summary.get("updated")})
    return {"success": True, "data": summary}


@router.get("/whatsapp/{account_id}/templates")
async def list_whatsapp_templates(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("connections.view")),
    status: str | None = Query(default=None, max_length=20),
):
    account = await _get_account(session, account_id)
    rows = await _service(request).list_account_templates(session, account, status=status)
    return _page([t.to_public_dict() for t in rows], len(rows), 1, max(len(rows), 1))
