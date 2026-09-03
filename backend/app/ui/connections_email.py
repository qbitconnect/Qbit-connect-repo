"""Phase 7 email connections UI — sender account management (§8, §9, §44).

    GET  /connections/email              email sender account dashboard
    GET  /connections/email/new          add-account wizard (7 visual steps)
    POST /connections/email/new          create + validate + health (real errors)
    GET  /connections/email/{id}         detail: status/health/sender/reputation
    POST /connections/email/{id}/validate|health|status|delete
    POST /connections/email/{id}/credentials   rotate credentials (write-only)

Thin client: every action calls EmailConnectionService; no provider logic in
templates; no secret is ever displayed or carried back into a form (§6) —
credential inputs are write-only password fields, and validation/health errors
come from the provider report (sanitized) so the operator sees the real
restriction instead of a fake success.
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
from app.services.marketing.connections_email import EmailConnectionService
from app.services.marketing.providers import build_provider_registry
from app.services.marketing.providers.email import EmailMockProvider
from app.services.marketing.reputation import SenderReputationService
from app.ui import _ctx, require_ui_permission, templates, ui_user_for

logger = get_logger("qbit.ui.connections_email")

email_view = ui_user_for("email.connections.view")
require_email_create = require_ui_permission("email.connections.create")
require_email_edit = require_ui_permission("email.connections.edit")
require_email_delete = require_ui_permission("email.connections.delete")
require_email_validate = require_ui_permission("email.connections.validate")
require_email_health = require_ui_permission("email.connections.health")

router = APIRouter(tags=["connections-email-ui"])


def _service(request: Request) -> EmailConnectionService:
    return EmailConnectionService(request.app.state.settings)


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
        raise QBITError("Email connection not found")
    account = await session.get(SendingAccount, aid)
    if account is None or account.channel != "EMAIL":
        raise QBITError("Email connection not found")
    return account


def _view(account: SendingAccount) -> dict:
    config = account.config_metadata or {}
    data = account.to_public_dict()
    data.update({
        "sender_name": config.get("sender_name"),
        "sender_email": account.identifier,
        "reply_to": config.get("reply_to"),
        "smtp_host": config.get("smtp_host"),
        "smtp_port": config.get("smtp_port"),
        "smtp_security": config.get("smtp_security"),
        "api_base_url": config.get("api_base_url"),
        "provider_configured": bool(config.get("configured")),
        "last_health_error": config.get("last_health_error"),
    })
    return data


def _mock_allowed(request: Request) -> bool:
    provider = _registry(request).get("email_mock")
    return isinstance(provider, EmailMockProvider)


# ---------------------------------------------------------------- dashboard
@router.get("/connections/email", response_class=HTMLResponse)
async def email_connections_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(email_view)],
):
    rows = (await session.execute(
        __import__("sqlalchemy").select(SendingAccount)
        .where(SendingAccount.channel == "EMAIL")
        .order_by(SendingAccount.created_at.desc())
    )).scalars().all()
    accounts = [_view(a) for a in rows]
    # §44: per-account delivery metrics from ACTUAL data (never fabricated)
    reputation = SenderReputationService(request.app.state.settings)
    metrics = {str(a.id): await reputation.account_metrics(session, a.id) for a in rows}
    perms = request.state.ui_permissions or set()
    return templates.TemplateResponse(request, "connections/email_index.html", _ctx(
        request, user,
        accounts=accounts, metrics=metrics,
        can_manage="email.connections.create" in perms,
        can_edit="email.connections.edit" in perms,
        can_validate="email.connections.validate" in perms,
        can_health="email.connections.health" in perms,
        can_delete="email.connections.delete" in perms,
        mock_allowed=_mock_allowed(request),
        ok=request.query_params.get("ok"),
        err=request.query_params.get("err"),
    ))


# ------------------------------------------------------------------- wizard
@router.get("/connections/email/new", response_class=HTMLResponse)
async def new_email_connection_form(
    request: Request,
    user: Annotated[object, Depends(require_email_create)],
):
    return templates.TemplateResponse(request, "connections/email_new.html", _ctx(
        request, user, mock_allowed=_mock_allowed(request), error=None, form={},
    ))


@router.post("/connections/email/new")
async def create_email_connection(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_email_create)],
    name: str = Form(""),
    provider: str = Form("smtp"),
    sender_name: str = Form(""),
    sender_email: str = Form(""),
    reply_to: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_security: str = Form("STARTTLS"),
    api_base_url: str = Form(""),
    region: str = Form(""),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    api_key: str = Form(""),
):
    credentials: dict = {}
    if smtp_password.strip():
        credentials["smtp_username"] = smtp_username.strip() or sender_email.strip()
        credentials["smtp_password"] = smtp_password.strip()
    if api_key.strip():
        credentials["api_key"] = api_key.strip()

    def _form() -> dict:
        return {
            "name": name, "provider": provider, "sender_name": sender_name,
            "sender_email": sender_email, "reply_to": reply_to,
            "smtp_host": smtp_host, "smtp_port": smtp_port,
            "smtp_security": smtp_security, "api_base_url": api_base_url,
            "region": region,
        }

    try:
        account = await _service(request).create_email_account(
            session, name=name, provider=provider,
            sender_name=sender_name or None, sender_email=sender_email,
            reply_to=reply_to or None,
            smtp_host=smtp_host or None,
            smtp_port=int(smtp_port) if smtp_port.strip().isdigit() else None,
            smtp_security=smtp_security or None,
            api_base_url=api_base_url or None, region=region or None,
            credentials=credentials or None, created_by=user.id,
        )
        # §9 steps 5–6 run for real: validate + health check now
        report = await _service(request).validate_account(
            session, account, registry=_registry(request))
        await _service(request).health_check(
            session, account, registry=_registry(request))
        await request.app.state.audit.log(
            session, action="email_account.created", resource_type="sending_account",
            resource_id=str(account.id), actor_user_id=user.id,
            metadata={"provider": account.provider, "validated": bool(report.get("ok"))},
        )
    except QBITError as exc:
        return templates.TemplateResponse(request, "connections/email_new.html", _ctx(
            request, user, mock_allowed=_mock_allowed(request),
            error=exc.message, form=_form(),
        ), status_code=400)
    if not report.get("ok"):
        # keep the wizard honest: account exists (ERROR) with the exact steps
        return RedirectResponse(
            f"/connections/email/{account.id}?validation=failed", status_code=303,
        )
    return RedirectResponse(
        f"/connections/email/{account.id}?ok=connected", status_code=303,
    )


# ------------------------------------------------------------------- detail
@router.get("/connections/email/{account_id}", response_class=HTMLResponse)
async def email_connection_detail(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(email_view)],
):
    account = await _account_or_404(session, account_id)
    perms = request.state.ui_permissions or set()
    metrics = await SenderReputationService(request.app.state.settings).account_metrics(
        session, account.id)
    return templates.TemplateResponse(request, "connections/email_detail.html", _ctx(
        request, user,
        account=_view(account), metrics=metrics,
        can_edit="email.connections.edit" in perms,
        can_validate="email.connections.validate" in perms,
        can_health="email.connections.health" in perms,
        can_delete="email.connections.delete" in perms,
        ok=request.query_params.get("ok"),
        err=request.query_params.get("err"),
        validation_failed=request.query_params.get("validation") == "failed",
    ))


# ------------------------------------------------------------------ actions
@router.post("/connections/email/{account_id}/validate")
async def validate_email_connection(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_email_validate)],
):
    account = await _account_or_404(session, account_id)
    report = await _service(request).validate_account(
        session, account, registry=_registry(request))
    await request.app.state.audit.log(
        session, action="email_account.validated", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id,
        metadata={"ok": bool(report.get("ok"))},
    )
    if report.get("ok"):
        return RedirectResponse(f"/connections/email/{account_id}?ok=validated",
                                status_code=303)
    return RedirectResponse(f"/connections/email/{account_id}?validation=failed",
                            status_code=303)


@router.post("/connections/email/{account_id}/health")
async def email_connection_health(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_email_health)],
):
    account = await _account_or_404(session, account_id)
    await _service(request).health_check(session, account, registry=_registry(request))
    await request.app.state.audit.log(
        session, action="email_account.health_checked", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id,
        metadata={"health": account.health_status},
    )
    return RedirectResponse(f"/connections/email/{account_id}?ok=health-checked",
                            status_code=303)


@router.post("/connections/email/{account_id}/status")
async def email_connection_status(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_email_edit)],
    status: str = Form(""),
):
    account = await _account_or_404(session, account_id)
    try:
        await _service(request).set_status(session, account, status)
        err = None
    except QBITError as exc:
        err = exc.message
    await request.app.state.audit.log(
        session, action="email_account.status_changed", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id, metadata={"status": status},
    )
    suffix = f"err={err}" if err else "ok=status-updated"
    return RedirectResponse(f"/connections/email/{account_id}?{suffix}",
                            status_code=303)


@router.post("/connections/email/{account_id}/credentials")
async def rotate_email_credentials(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_email_edit)],
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    api_key: str = Form(""),
):
    account = await _account_or_404(session, account_id)
    credentials: dict = {}
    if smtp_password.strip():
        credentials["smtp_username"] = smtp_username.strip() or account.identifier
        credentials["smtp_password"] = smtp_password.strip()
    if api_key.strip():
        credentials["api_key"] = api_key.strip()
    try:
        if not credentials:
            raise QBITError("Nothing to rotate — enter a new password or API key")
        await _service(request).update_credentials(session, account, credentials)
        err = None
    except QBITError as exc:
        err = exc.message
    await request.app.state.audit.log(
        session, action="email_account.credentials_rotated",
        resource_type="sending_account", resource_id=str(account.id),
        actor_user_id=user.id, metadata={"rotated": bool(credentials)},
    )
    if err:
        return RedirectResponse(f"/connections/email/{account_id}?err={err}",
                                status_code=303)
    return RedirectResponse(f"/connections/email/{account_id}?ok=credentials-rotated",
                            status_code=303)


@router.post("/connections/email/{account_id}/delete")
async def delete_email_connection(
    account_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_email_delete)],
):
    account = await _account_or_404(session, account_id)
    await _service(request).delete_account(session, account)
    await request.app.state.audit.log(
        session, action="email_account.removed", resource_type="sending_account",
        resource_id=str(account.id), actor_user_id=user.id, metadata={"name": account.name},
    )
    return RedirectResponse("/connections/email?ok=removed", status_code=303)
