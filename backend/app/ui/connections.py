"""Phase 6 connections UI — WhatsApp account management (§31, §32, §35).

    GET  /connections                       hub: WhatsApp / Email / SMS sections
    GET  /connections/whatsapp/new          add-account wizard (7 visual steps)
    POST /connections/whatsapp/new          create + validate + health (real errors)
    GET  /connections/whatsapp/{id}         detail: status/health/phone/templates
    POST /connections/whatsapp/{id}/validate|health|sync-templates|status|delete
    POST /connections/whatsapp/{id}/credentials   rotate credentials (write-only)

Thin client: every action calls ConnectionService; no provider logic in
templates; no secret is ever displayed or carried back into a form (§4) —
credential inputs are write-only password fields, and validation/health
errors come from the provider report (sanitized) so the operator sees the
real restriction instead of a fake success.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import QBITError
from app.core.logging import get_logger
from app.models.marketing import SendingAccount
from app.services.marketing import ConnectionService, CredentialVault
from app.services.marketing.providers import build_provider_registry
from app.services.marketing.providers.whatsapp import WhatsAppMockProvider
from app.ui import _ctx, require_ui_permission, templates, ui_user_for

logger = get_logger("qbit.ui.connections")

connections_view = ui_user_for("connections.view")
require_connections_create = require_ui_permission("connections.create")
require_connections_edit = require_ui_permission("connections.edit")
require_connections_delete = require_ui_permission("connections.delete")
require_connections_validate = require_ui_permission("connections.validate")
require_connections_health = require_ui_permission("connections.health")
require_connections_sync = require_ui_permission("connections.sync_templates")

router = APIRouter(tags=["connections-ui"])


def _service(request: Request) -> ConnectionService:
    return ConnectionService(request.app.state.settings)


def _registry(request: Request):
    registry = getattr(request.app.state, "marketing_providers", None)
    if registry is None:
        registry = build_provider_registry(request.app.state.settings)
        request.app.state.marketing_providers = registry
    return registry


async def _account_or_404(session: AsyncSession, account_id: str) -> SendingAccount:
    try:
        aid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        raise QBITError("Connection not found")
    account = await session.get(SendingAccount, aid)
    if account is None:
        raise QBITError("Connection not found")
    return account


def _view(account: SendingAccount) -> dict:
    data = account.to_public_dict()
    data["last_health_error"] = (account.config_metadata or {}).get("last_health_error")
    return data


# ------------------------------------------------------------------- hub
@router.get("/connections", response_class=HTMLResponse)
async def connections_hub(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(connections_view)],
):
    from sqlalchemy import select

    rows = (await session.execute(
        select(SendingAccount).order_by(SendingAccount.created_at.desc())
    )).scalars().all()
    grouped = {"WHATSAPP": [], "EMAIL": [], "SMS": []}
    for account in rows:
        grouped.setdefault((account.channel or "OTHER").upper(), []).append(_view(account))
    perms = request.state.ui_permissions or set()
    return templates.TemplateResponse(request, "connections/index.html", _ctx(
        request, user,
        whatsapp=grouped.get("WHATSAPP", []),
        email=grouped.get("EMAIL", []),
        sms=grouped.get("SMS", []),
        can_manage="connections.create" in perms,
        can_edit="connections.edit" in perms,
        can_validate="connections.validate" in perms,
        can_health="connections.health" in perms,
        can_sync="connections.sync_templates" in perms,
        can_delete="connections.delete" in perms,
        mock_allowed=_mock_allowed(request),
        ok=request.query_params.get("ok"),
        err=request.query_params.get("err"),
    ))


def _mock_allowed(request: Request) -> bool:
    provider = _registry(request).get("whatsapp_mock")
    return isinstance(provider, WhatsAppMockProvider)


# ----------------------------------------------------------------- wizard
@router.get("/connections/whatsapp/new", response_class=HTMLResponse)
async def new_connection_form(
    request: Request,
    user: Annotated[object, Depends(require_connections_create)],
):
    return templates.TemplateResponse(request, "connections/new.html", _ctx(
        request, user,
        mock_allowed=_mock_allowed(request),
        error=None, form={},
    ))


@router.post("/connections/whatsapp/new")
async def create_connection(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_connections_create)],
    name: str = Form(""),
    provider: str = Form("whatsapp_cloud"),
    access_token: str = Form(""),
    app_secret: str = Form(""),
    phone_number_id: str = Form(""),
    business_account_id: str = Form(""),
):
    service = _service(request)
    credentials = {}
    if access_token.strip():
        credentials["access_token"] = access_token.strip()
    if app_secret.strip():
        credentials["app_secret"] = app_secret.strip()
    try:
        account = await service.create_whatsapp_account(
            session, name=name, provider=provider,
            phone_number_id=phone_number_id, business_account_id=business_account_id,
            credentials=credentials or None, created_by=user.id,
        )
        # §32 step 4–6: run the real validation flow + health check now, so
        # the operator lands on a complete page (or the exact provider error)
        report = await service.validate_account(session, account, registry=_registry(request))
        await service.health_check(session, account, registry=_registry(request))
        await request.app.state.audit.log(
            session, action="whatsapp_account.created", resource_type="sending_account",
            resource_id=str(account.id), actor_user_id=user.id,
            metadata={"provider": account.provider, "validated": bool(report.get("ok"))},
        )
    except QBITError as exc:
        return templates.TemplateResponse(request, "connections/new.html", _ctx(
            request, user,
            mock_allowed=_mock_allowed(request),
            error=exc.message, form={"name": name, "phone_number_id": phone_number_id,
                                     "business_account_id": business_account_id},
        ), status_code=400)
    if not report.get("ok"):
        # keep the wizard honest: account exists (ERROR) with the exact steps
        return RedirectResponse(
            f"/connections/whatsapp/{account.id}?validation=failed", status_code=303,
        )
    return RedirectResponse(
        f"/connections/whatsapp/{account.id}?ok=connected", status_code=303,
    )


# ----------------------------------------------------------------- detail
@router.get("/connections/whatsapp/{account_id}", response_class=HTMLResponse)
async def connection_detail(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(connections_view)],
):
    account = await _account_or_404(session, account_id)
    perms = request.state.ui_permissions or set()
    templates_rows = []
    if "connections.view" in perms:
        templates_rows = await _service(request).list_account_templates(session, account)
    return templates.TemplateResponse(request, "connections/detail.html", _ctx(
        request, user,
        account=_view(account),
        templates=[t.to_public_dict() for t in templates_rows],
        can_manage="connections.edit" in perms,
        can_validate="connections.validate" in perms,
        can_health="connections.health" in perms,
        can_sync="connections.sync_templates" in perms,
        can_delete="connections.delete" in perms,
        mock_allowed=_mock_allowed(request),
        ok=request.query_params.get("ok"),
        err=request.query_params.get("err"),
        validation_failed=request.query_params.get("validation") == "failed",
    ))


# ----------------------------------------------------------------- actions
@router.post("/connections/whatsapp/{account_id}/validate")
async def validate_connection(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_connections_validate)],
):
    account = await _account_or_404(session, account_id)
    report = await _service(request).validate_account(session, account, registry=_registry(request))
    await request.app.state.audit.log(
        session, action="whatsapp_account.credential_validated", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id,
        metadata={"ok": bool(report.get("ok"))},
    )
    if report.get("ok"):
        return RedirectResponse(f"/connections/whatsapp/{account_id}?ok=validated", status_code=303)
    return RedirectResponse(f"/connections/whatsapp/{account_id}?validation=failed", status_code=303)


@router.post("/connections/whatsapp/{account_id}/health")
async def health_connection(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_connections_health)],
):
    account = await _account_or_404(session, account_id)
    result = await _service(request).health_check(session, account, registry=_registry(request))
    await request.app.state.audit.log(
        session, action="whatsapp_account.health_checked", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id,
        metadata={"health": account.health_status},
    )
    detail = result.get("detail")
    if result.get("health") == "HEALTHY":
        return RedirectResponse(f"/connections/whatsapp/{account_id}?ok=healthy", status_code=303)
    message = detail if isinstance(detail, str) else "Health check reported a problem"
    return RedirectResponse(
        f"/connections/whatsapp/{account_id}?err={message[:180]}", status_code=303,
    )


@router.post("/connections/whatsapp/{account_id}/sync-templates")
async def sync_connection_templates(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_connections_sync)],
):
    account = await _account_or_404(session, account_id)
    try:
        summary = await _service(request).sync_templates(session, account, registry=_registry(request))
    except QBITError as exc:
        return RedirectResponse(
            f"/connections/whatsapp/{account_id}?err={exc.message[:180]}", status_code=303,
        )
    await request.app.state.audit.log(
        session, action="whatsapp_account.templates_synced", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id,
        metadata={"created": summary.get("created"), "updated": summary.get("updated")},
    )
    return RedirectResponse(
        f"/connections/whatsapp/{account_id}?ok=synced+{summary.get('created', 0)}+new+{summary.get('updated', 0)}+updated",
        status_code=303,
    )


@router.post("/connections/whatsapp/{account_id}/status")
async def connection_status(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_connections_edit)],
    status: str = Form(""),
):
    account = await _account_or_404(session, account_id)
    try:
        await _service(request).set_status(session, account, status)
    except QBITError as exc:
        return RedirectResponse(
            f"/connections/whatsapp/{account_id}?err={exc.message[:180]}", status_code=303,
        )
    await request.app.state.audit.log(
        session, action="whatsapp_account.status_changed", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id, metadata={"status": status},
    )
    return RedirectResponse(f"/connections/whatsapp/{account_id}?ok=status", status_code=303)


@router.post("/connections/whatsapp/{account_id}/credentials")
async def rotate_credentials(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_connections_edit)],
    access_token: str = Form(""),
    app_secret: str = Form(""),
):
    account = await _account_or_404(session, account_id)
    payload = {}
    if access_token.strip():
        payload["access_token"] = access_token.strip()
    if app_secret.strip():
        payload["app_secret"] = app_secret.strip()
    if not payload:
        return RedirectResponse(
            f"/connections/whatsapp/{account_id}?err=Nothing+to+update", status_code=303,
        )
    try:
        await _service(request).update_credentials(session, account, payload)
    except QBITError as exc:
        return RedirectResponse(
            f"/connections/whatsapp/{account_id}?err={exc.message[:180]}", status_code=303,
        )
    await request.app.state.audit.log(
        session, action="whatsapp_account.credentials_rotated", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id,
        metadata={"fields": sorted(payload.keys())},
    )
    return RedirectResponse(
        f"/connections/whatsapp/{account_id}?ok=credentials+updated+-+revalidate", status_code=303,
    )


@router.post("/connections/whatsapp/{account_id}/details")
async def update_connection_details(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_connections_edit)],
    phone_number_id: str = Form(""),
    business_account_id: str = Form(""),
):
    account = await _account_or_404(session, account_id)
    account.phone_number_id = phone_number_id.strip() or None
    account.business_account_id = business_account_id.strip() or None
    if account.phone_number_id and account.identifier == account.name:
        account.identifier = account.phone_number_id
    config = dict(account.config_metadata or {})
    config["configured"] = bool(config.get("configured"))
    account.config_metadata = config
    await session.commit()
    await request.app.state.audit.log(
        session, action="whatsapp_account.updated", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id,
        metadata={"fields": ["phone_number_id", "business_account_id"]},
    )
    return RedirectResponse(
        f"/connections/whatsapp/{account_id}?ok=details+saved", status_code=303,
    )


@router.post("/connections/whatsapp/{account_id}/delete")
async def delete_connection(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_connections_delete)],
):
    account = await _account_or_404(session, account_id)
    name = account.name
    await _service(request).delete_account(session, account)
    await request.app.state.audit.log(
        session, action="whatsapp_account.removed", resource_type="sending_account",
        resource_id=account_id, actor_user_id=user.id, metadata={"name": name},
    )
    return RedirectResponse(f"/connections?ok=removed+{name}", status_code=303)
