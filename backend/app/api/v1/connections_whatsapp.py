"""WhatsApp sending account endpoints (Phase 7 provider foundation).

Base path: /api/v1/connections/whatsapp — mirrors the email connections API.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import DbSession, get_client_ip, get_user_agent, require_permission
from app.core.errors import NotFoundError
from app.models.user import User
from app.schemas.marketing import (
    ActionOut,
    ListOut,
    WhatsAppAccountCreate,
    WhatsAppAccountUpdate,
)
from app.services.marketing.accounts import SendingAccountService
from app.services.marketing.secrets import SecretVault

router = APIRouter(prefix="/connections/whatsapp", tags=["connections"])


def _service(request: Request) -> SendingAccountService:
    return SendingAccountService(SecretVault(request.app.state.settings.QBIT_SECRET_KEY))


@router.get("", response_model=ListOut)
async def list_whatsapp_accounts(
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("whatsapp.connections.view"))],
):
    service = _service(request)
    accounts = await service.list(session, channel="WHATSAPP")
    return ListOut(data={"items": [a.to_public_dict() for a in accounts], "total": len(accounts)})


@router.post("", response_model=ActionOut, status_code=201)
async def create_whatsapp_account(
    payload: WhatsAppAccountCreate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("whatsapp.connections.create"))],
):
    service = _service(request)
    account = await service.create(
        session,
        channel="WHATSAPP",
        provider=payload.provider,
        name=payload.name,
        config=payload.config,
        credentials=payload.credentials,
        phone_number_id=payload.phone_number_id,
        business_account_id=payload.business_account_id,
        created_by=actor.id,
        is_production=request.app.state.settings.is_production,
    )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="whatsapp.account_created",
        actor_user_id=actor.id,
        resource_type="sending_account",
        resource_id=str(account.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"provider": payload.provider, "channel": "WHATSAPP"},
    )
    return ActionOut(data={"account": account.to_public_dict()})


@router.get("/{account_id}", response_model=ActionOut)
async def get_whatsapp_account(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("whatsapp.connections.view"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "WHATSAPP":
        raise NotFoundError("Sending account not found")
    return ActionOut(data={"account": account.to_public_dict()})


@router.patch("/{account_id}", response_model=ActionOut)
async def update_whatsapp_account(
    account_id: uuid.UUID,
    payload: WhatsAppAccountUpdate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("whatsapp.connections.edit"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "WHATSAPP":
        raise NotFoundError("Sending account not found")
    updated = await service.update(
        session,
        account,
        name=payload.name,
        config=payload.config,
        credentials=payload.credentials,
        phone_number_id=payload.phone_number_id,
        business_account_id=payload.business_account_id,
    )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="whatsapp.account_modified",
        actor_user_id=actor.id,
        resource_type="sending_account",
        resource_id=str(account.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return ActionOut(data={"account": updated.to_public_dict()})


@router.delete("/{account_id}", response_model=ActionOut)
async def delete_whatsapp_account(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("whatsapp.connections.delete"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "WHATSAPP":
        raise NotFoundError("Sending account not found")
    account_id_str = str(account.id)
    await service.delete(session, account)
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="whatsapp.account_disabled",
        actor_user_id=actor.id,
        resource_type="sending_account",
        resource_id=account_id_str,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return ActionOut(data={"deleted": True})


@router.post("/{account_id}/validate", response_model=ActionOut)
async def validate_whatsapp_account(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("whatsapp.connections.validate"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "WHATSAPP":
        raise NotFoundError("Sending account not found")
    result = await service.validate(session, account)
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="whatsapp.validation",
        actor_user_id=actor.id,
        resource_type="sending_account",
        resource_id=str(account.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"ok": result["ok"]},
    )
    return ActionOut(data={"validation": result, "account": account.to_public_dict()})


@router.post("/{account_id}/health", response_model=ActionOut)
async def whatsapp_account_health(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("whatsapp.connections.health"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "WHATSAPP":
        raise NotFoundError("Sending account not found")
    result = await service.health_check(session, account)
    await session.commit()
    return ActionOut(data={"health": result, "account": account.to_public_dict()})
