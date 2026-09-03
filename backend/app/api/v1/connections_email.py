"""Email connections API (Phase 7 §2, §45).

    GET    /api/v1/connections/email                    list email sender accounts
    POST   /api/v1/connections/email                    create (PENDING until validated)
    GET    /api/v1/connections/email/{id}               detail (no secrets)
    PATCH  /api/v1/connections/email/{id}               update / rotate credentials
    DELETE /api/v1/connections/email/{id}               remove (+ credential row)
    POST   /api/v1/connections/email/{id}/validate      sender validation flow (§7)
    POST   /api/v1/connections/email/{id}/health        health check
    GET    /api/v1/connections/email/{id}/templates     usable EMAIL templates
    GET    /api/v1/connections/email/{id}/reputation    delivery metrics + warnings (§43)

Security (§6): credentials are WRITE-ONLY — accepted, encrypted at rest,
never returned, never logged. Responses expose the sender email (display
data), booleans (has_credentials) and masked tails — never smtp_password or
api_key. Every route is permission-checked server-side (§48); audit records
every mutation without credential values.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AuditDep, DbSession, require_permission
from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.marketing import CampaignTemplate, SendingAccount
from app.schemas.marketing import TemplateCreate
from app.services.marketing.connections_email import EmailConnectionService
from app.services.marketing.providers import MarketingProviderRegistry
from app.services.marketing.reputation import SenderReputationService
from app.services.marketing.template import TemplateService

logger = get_logger("qbit.marketing.connections_email_api")

router = APIRouter(prefix="/connections/email", tags=["connections-email"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _registry(request: Request) -> MarketingProviderRegistry:
    registry = getattr(request.app.state, "marketing_providers", None)
    if registry is None:
        from app.services.marketing.providers import build_provider_registry

        registry = build_provider_registry(_settings(request))
        request.app.state.marketing_providers = registry
    return registry


def _service(request: Request) -> EmailConnectionService:
    return EmailConnectionService(_settings(request))


# ------------------------------------------------------------- request schemas
class EmailCredentials(BaseModel):
    """Provider secrets (write-only). Stored ENCRYPTED in the vault (§6)."""

    model_config = {"extra": "forbid"}

    smtp_username: str | None = Field(default=None, max_length=320,
                                      description="SMTP username (usually the sender)")
    smtp_password: str | None = Field(default=None, min_length=1, max_length=1024,
                                      description="SMTP password")
    api_key: str | None = Field(default=None, min_length=1, max_length=1024,
                                description="Email API key")
    webhook_secret: str | None = Field(default=None, max_length=256,
                                       description="Reserved per-account webhook secret")


class EmailConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    provider: str | None = Field(default=None, max_length=50,
                                 description="smtp | email_api (default: EMAIL_PROVIDER env)")
    sender_name: str | None = Field(default=None, max_length=200)
    sender_email: str = Field(min_length=3, max_length=320)
    reply_to: str | None = Field(default=None, max_length=320)
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: str | None = Field(default=None, max_length=20)
    api_base_url: str | None = Field(default=None, max_length=500)
    region: str | None = Field(default=None, max_length=64)
    credentials: EmailCredentials | None = None
    capabilities: dict | None = None


class EmailConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    sender_name: str | None = Field(default=None, max_length=200)
    reply_to: str | None = Field(default=None, max_length=320)
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: str | None = Field(default=None, max_length=20)
    api_base_url: str | None = Field(default=None, max_length=500)
    region: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)
    credentials: EmailCredentials | None = Field(
        default=None, description="Presenting credentials ROTATES them (write-only)"
    )


class EmailTemplateFromAccount(TemplateCreate):
    """Template creation scoped to this account (provider preset)."""


def _page(items: list, total: int, page: int, page_size: int) -> dict:
    return {
        "success": True,
        "data": {
            "items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, -(-total // page_size)) if total else 1,
        },
    }


async def _get_account(session, account_id: uuid.UUID) -> SendingAccount:
    account = await session.get(SendingAccount, account_id)
    if account is None or account.channel != "EMAIL":
        raise NotFoundError("Email connection not found")
    return account


def _account_view(request: Request, account: SendingAccount) -> dict:
    """Display-safe payload (§6): no credentials, no secret-like config."""
    registry = _registry(request)
    provider = registry.get(account.provider)
    config = account.config_metadata or {}
    data = account.to_public_dict()
    data["provider_available"] = provider is not None
    data["provider_test_only"] = bool(provider.test_only) if provider else False
    data["provider_configured"] = bool(config.get("configured"))
    data["sender_name"] = config.get("sender_name")
    data["sender_email"] = account.identifier
    data["reply_to"] = config.get("reply_to")
    data["smtp_host"] = config.get("smtp_host")
    data["smtp_port"] = config.get("smtp_port")
    data["smtp_security"] = config.get("smtp_security")
    data["api_base_url"] = config.get("api_base_url")
    data["last_health_error"] = config.get("last_health_error")
    return data


# ------------------------------------------------------------------------ list
@router.get("")
async def list_email_connections(
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("email.connections.view")),
    status: str | None = Query(default=None, max_length=20),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    rows, total = await _service(request).list_email_accounts(
        session, status=status, page=page, page_size=page_size,
    )
    return _page([_account_view(request, a) for a in rows], total, page, page_size)


# ----------------------------------------------------------------------- create
@router.post("", status_code=201)
async def create_email_connection(
    payload: EmailConnectionCreate,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("email.connections.create")),
):
    account = await _service(request).create_email_account(
        session,
        name=payload.name,
        provider=payload.provider or _settings(request).EMAIL_PROVIDER,
        sender_name=payload.sender_name,
        sender_email=payload.sender_email,
        reply_to=payload.reply_to,
        smtp_host=payload.smtp_host,
        smtp_port=payload.smtp_port,
        smtp_security=payload.smtp_security,
        api_base_url=payload.api_base_url,
        region=payload.region,
        credentials=(payload.credentials.model_dump(exclude_none=True)
                     if payload.credentials else None),
        capabilities=payload.capabilities,
        created_by=user.id,
    )
    await audit.log(session, action="email_account.created", resource_type="sending_account",
                    resource_id=str(account.id), actor_user_id=user.id,
                    metadata={"provider": account.provider,
                              "has_credentials": bool(account.credential_ref)})
    return {"success": True, "data": _account_view(request, account)}


# ------------------------------------------------------------------------ read
@router.get("/{account_id}")
async def get_email_connection(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("email.connections.view")),
):
    account = await _get_account(session, account_id)
    return {"success": True, "data": _account_view(request, account)}


# ---------------------------------------------------------------------- update
@router.patch("/{account_id}")
async def update_email_connection(
    account_id: uuid.UUID,
    payload: EmailConnectionUpdate,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("email.connections.edit")),
):
    account = await _get_account(session, account_id)
    service = _service(request)
    config = dict(account.config_metadata or {})
    rotated = False

    if payload.name is not None:
        clean = " ".join(payload.name.split())[:150]
        if not clean:
            raise ValidationError("Account name must not be empty")
        account.name = clean
    if payload.sender_name is not None:
        config["sender_name"] = " ".join(payload.sender_name.split())[:200] or None
    if payload.reply_to is not None:
        from app.services.marketing.connections_email import _require_email_address

        config["reply_to"] = (
            _require_email_address(payload.reply_to, "reply_to") if payload.reply_to else None
        )
    if payload.smtp_host is not None:
        config["smtp_host"] = payload.smtp_host.strip() or None
    if payload.smtp_port is not None:
        config["smtp_port"] = payload.smtp_port
    if payload.smtp_security is not None:
        security = payload.smtp_security.strip().upper()
        from app.services.marketing.providers.email.smtp import SECURITY_MODES

        if security not in SECURITY_MODES:
            raise ValidationError(f"smtp_security must be one of {', '.join(SECURITY_MODES)}")
        config["smtp_security"] = security
    if payload.api_base_url is not None:
        base = payload.api_base_url.strip()
        if base and not base.startswith(("http://", "https://")):
            raise ValidationError("api_base_url must be an http(s) URL")
        config["api_base_url"] = base or None
    if payload.region is not None:
        config["region"] = payload.region.strip()[:64] or None
    # any config change resets the configured flag until revalidation
    if any(f is not None for f in (
        payload.sender_name, payload.reply_to, payload.smtp_host, payload.smtp_port,
        payload.smtp_security, payload.api_base_url, payload.region,
    )):
        config["configured"] = False
        account.config_metadata = config
        if account.status == "ACTIVE":
            account.status = "INACTIVE"
    if payload.credentials is not None:
        await service.update_credentials(
            session, account, payload.credentials.model_dump(exclude_none=True),
        )
        rotated = True
    if payload.status is not None:
        await service.set_status(session, account, payload.status)

    await session.commit()
    await session.refresh(account)
    await audit.log(session, action="email_account.updated", resource_type="sending_account",
                    resource_id=str(account.id), actor_user_id=user.id,
                    metadata={"credentials_rotated": rotated})
    return {"success": True, "data": _account_view(request, account)}


# ---------------------------------------------------------------------- delete
@router.delete("/{account_id}")
async def delete_email_connection(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("email.connections.delete")),
):
    account = await _get_account(session, account_id)
    account_id_str, name = str(account.id), account.name
    await _service(request).delete_account(session, account)
    await audit.log(session, action="email_account.removed", resource_type="sending_account",
                    resource_id=account_id_str, actor_user_id=user.id,
                    metadata={"name": name})
    return {"success": True, "data": {"removed": True, "id": account_id_str}}


# -------------------------------------------------------------------- validate
@router.post("/{account_id}/validate")
async def validate_email_connection(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("email.connections.validate")),
):
    """§7 sender validation: config → sender → connectivity → auth.
    ACTIVE only when every step passes; ERROR otherwise (real errors shown)."""
    account = await _get_account(session, account_id)
    report = await _service(request).validate_account(session, account, registry=_registry(request))
    await audit.log(session, action="email_account.validated",
                    resource_type="sending_account", resource_id=str(account.id),
                    actor_user_id=user.id,
                    metadata={"ok": bool(report.get("ok")), "status": account.status})
    data = _account_view(request, account)
    data["validation"] = report
    return {"success": True, "data": data}


# ---------------------------------------------------------------------- health
@router.post("/{account_id}/health")
async def email_connection_health(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("email.connections.health")),
):
    account = await _get_account(session, account_id)
    result = await _service(request).health_check(session, account, registry=_registry(request))
    await audit.log(session, action="email_account.health_checked",
                    resource_type="sending_account", resource_id=str(account.id),
                    actor_user_id=user.id, metadata={"health": account.health_status})
    data = _account_view(request, account)
    data["health_detail"] = result
    return {"success": True, "data": data}


# ------------------------------------------------------------------- templates
@router.get("/{account_id}/templates")
async def list_email_connection_templates(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("email.templates.view")),
):
    """EMAIL templates usable with this account (LOCAL templates authored in
    QBIT — email providers do not host template catalogs)."""
    account = await _get_account(session, account_id)
    rows = (await session.execute(
        select(CampaignTemplate)
        .where(CampaignTemplate.channel == "EMAIL",
               CampaignTemplate.status == "ACTIVE")
        .order_by(CampaignTemplate.updated_at.desc())
    )).scalars().all()
    return _page([t.to_public_dict() for t in rows], len(rows), 1, max(len(rows), 1))


@router.post("/{account_id}/templates", status_code=201)
async def create_email_connection_template(
    account_id: uuid.UUID,
    payload: EmailTemplateFromAccount,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("email.templates.manage")),
):
    """Create an EMAIL template bound to this account's context."""
    account = await _get_account(session, account_id)
    if (payload.channel or "").upper() != "EMAIL":
        raise ValidationError("channel must be EMAIL for email account templates")
    template = await TemplateService().create(
        session, name=payload.name, channel="EMAIL",
        subject=payload.subject, body=payload.body,
        language=payload.language, status=payload.status,
        variables=payload.variables, text_body=payload.text_body,
        created_by=user.id,
    )
    await audit.log(session, action="email_template.created", resource_type="campaign_template",
                    resource_id=str(template.id), actor_user_id=user.id,
                    metadata={"account_id": str(account.id), "channel": "EMAIL"})
    return {"success": True, "data": template.to_public_dict()}


# ------------------------------------------------------------------ reputation
@router.get("/{account_id}/reputation")
async def email_connection_reputation(
    account_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("email.connections.view")),
):
    """§43 sender-reputation monitoring foundation: delivery/bounce/complaint
    metrics from actual events + threshold warnings. No inbox-placement claims."""
    account = await _get_account(session, account_id)
    metrics = await SenderReputationService(_settings(request)).account_metrics(
        session, account.id,
    )
    return {"success": True, "data": metrics}
