"""Email sending account endpoints (Phase 7 §8, §44, §45).

Base path: /api/v1/connections/email
Every route enforces RBAC server-side; credentials are never returned.
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
    EmailAccountCreate,
    EmailAccountUpdate,
    ListOut,
)
from app.services.marketing.accounts import SendingAccountService
from app.services.marketing.secrets import SecretVault
from app.services.settings import SystemSettingsService

router = APIRouter(prefix="/connections/email", tags=["connections"])


def _service(request: Request) -> SendingAccountService:
    return SendingAccountService(SecretVault(request.app.state.settings.QBIT_SECRET_KEY))


async def _company_name(session, request: Request) -> str:
    try:
        service = SystemSettingsService()
        values = await service.get_all(session)
        return str(values.get("system.name", "QBIT Connect"))
    except Exception:  # noqa: BLE001
        return "QBIT Connect"


@router.get("", response_model=ListOut)
async def list_email_accounts(
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("email.connections.view"))],
):
    service = _service(request)
    accounts = await service.list(session, channel="EMAIL")
    from app.services.marketing.analytics import AnalyticsService

    analytics = AnalyticsService()
    data = []
    for account in accounts:
        item = account.to_public_dict()
        summary = await analytics.account_summary(session, account.id)
        item["delivery_metrics"] = {
            k: summary[k]
            for k in ("delivery_rate", "bounce_rate", "complaint_rate", "sent_total")
        }
        data.append(item)
    return ListOut(data={"items": data, "total": len(data)})


@router.post("", response_model=ActionOut, status_code=201)
async def create_email_account(
    payload: EmailAccountCreate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("email.connections.create"))],
):
    service = _service(request)
    account = await service.create(
        session,
        channel="EMAIL",
        provider=payload.provider,
        name=payload.name,
        config=payload.config,
        credentials=payload.credentials,
        sender_name=payload.sender_name,
        sender_email=payload.sender_email,
        reply_to=payload.reply_to,
        created_by=actor.id,
        is_production=request.app.state.settings.is_production,
    )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="email.account_created",
        actor_user_id=actor.id,
        resource_type="sending_account",
        resource_id=str(account.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"provider": payload.provider, "channel": "EMAIL"},
    )
    return ActionOut(data={"account": account.to_public_dict()})


@router.get("/{account_id}", response_model=ActionOut)
async def get_email_account(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("email.connections.view"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "EMAIL":
        raise NotFoundError("Sending account not found")
    return ActionOut(data={"account": account.to_public_dict()})


@router.patch("/{account_id}", response_model=ActionOut)
async def update_email_account(
    account_id: uuid.UUID,
    payload: EmailAccountUpdate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("email.connections.edit"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "EMAIL":
        raise NotFoundError("Sending account not found")
    updated = await service.update(
        session,
        account,
        name=payload.name,
        config=payload.config,
        credentials=payload.credentials,
        sender_name=payload.sender_name,
        sender_email=payload.sender_email,
        reply_to=payload.reply_to,
    )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="email.account_modified",
        actor_user_id=actor.id,
        resource_type="sending_account",
        resource_id=str(account.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return ActionOut(data={"account": updated.to_public_dict()})


@router.delete("/{account_id}", response_model=ActionOut)
async def delete_email_account(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("email.connections.delete"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "EMAIL":
        raise NotFoundError("Sending account not found")
    account_id_str = str(account.id)
    await service.delete(session, account)
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="email.account_disabled",
        actor_user_id=actor.id,
        resource_type="sending_account",
        resource_id=account_id_str,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return ActionOut(data={"deleted": True})


@router.post("/{account_id}/validate", response_model=ActionOut)
async def validate_email_account(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("email.connections.validate"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "EMAIL":
        raise NotFoundError("Sending account not found")
    result = await service.validate(session, account)
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="email.validation",
        actor_user_id=actor.id,
        resource_type="sending_account",
        resource_id=str(account.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"ok": result["ok"]},
    )
    return ActionOut(data={"validation": result, "account": account.to_public_dict()})


@router.post("/{account_id}/health", response_model=ActionOut)
async def email_account_health(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("email.connections.health"))],
):
    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "EMAIL":
        raise NotFoundError("Sending account not found")
    result = await service.health_check(session, account)
    await session.commit()
    return ActionOut(data={"health": result, "account": account.to_public_dict()})


@router.get("/{account_id}/templates", response_model=ListOut)
async def email_account_templates(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("email.connections.view"))],
):
    from sqlalchemy import select

    from app.models.marketing import MarketingTemplate

    service = _service(request)
    account = await service.get(session, account_id)
    if account.channel != "EMAIL":
        raise NotFoundError("Sending account not found")
    rows = (
        await session.scalars(
            select(MarketingTemplate)
            .where(MarketingTemplate.channel == "EMAIL")
            .order_by(MarketingTemplate.updated_at.desc())
        )
    ).all()
    return ListOut(data={"items": [t.to_public_dict() for t in rows], "total": len(rows)})
