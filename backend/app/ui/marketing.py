"""Operator UI — marketing pages (Phase 7 §8, §9, §35, §36, §44).

Cookie-authenticated server-rendered pages over the SAME services the API
uses; the UI is never the security boundary. Credentials entered in the
connections wizard are posted to the backend, encrypted into the vault and
NEVER rendered back (masked refs only).

Pages:
    GET  /connections                       sending accounts (EMAIL/WHATSAPP)
    GET  /connections/email/new             7-step add email account wizard
    GET  /connections/whatsapp/new          add WhatsApp account wizard
    POST /connections/email | /connections/whatsapp
    POST /connections/{channel}/{id}/validate | /health | /delete
    GET  /campaigns                         campaign list
    GET  /campaigns/new                     10-step campaign wizard
    POST /campaigns/new                     create (+ optional launch)
    GET  /campaigns/{id}                    detail + real analytics
    POST /campaigns/{id}/pause|resume|cancel|requeue
    GET  /marketing/templates               template list + create form
    POST /marketing/templates | /{id}/delete
    GET  /marketing/suppression             suppression list + add form
    POST /marketing/suppression | /{id}/delete
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.marketing import Campaign, MarketingTemplate, SendingAccount, Suppression
from app.models.user import User
from app.services.marketing.accounts import SendingAccountService
from app.services.marketing.analytics import AnalyticsService
from app.services.marketing.campaigns import CampaignService
from app.services.marketing.queue import build_marketing_queue
from app.services.marketing.secrets import SecretVault
from app.services.marketing.suppression import SuppressionService
from app.ui import _ctx, templates, ui_user_for, UiRedirect

router = APIRouter(tags=["ui-marketing"])


def _vault(request: Request) -> SecretVault:
    return SecretVault(request.app.state.settings.QBIT_SECRET_KEY)


def _accounts_service(request: Request) -> SendingAccountService:
    return SendingAccountService(_vault(request))


async def _perms(session: AsyncSession, user: User) -> set[str]:
    from app.services import rbac as rbac_service

    return await rbac_service.load_user_permissions(session, user.id)


# ---------------------------------------------------------------- connections
@router.get("/connections", response_class=HTMLResponse)
async def connections_home(
    request: Request,
    user: Annotated[User, Depends(ui_user_for("email.connections.view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    perms = await _perms(session, user)
    service = _accounts_service(request)
    accounts = await service.list(session)
    analytics = AnalyticsService()
    email_rows, whatsapp_rows = [], []
    for account in accounts:
        item = {"account": account.to_public_dict()}
        if account.channel == "EMAIL":
            summary = await analytics.account_summary(session, account.id)
            item["metrics"] = summary
            email_rows.append(item)
        else:
            whatsapp_rows.append(item)
    can_view_whatsapp = "whatsapp.connections.view" in perms
    can_add_email = "email.connections.create" in perms
    can_add_whatsapp = "whatsapp.connections.create" in perms
    return templates.TemplateResponse(
        request, "marketing/connections.html",
        _ctx(request, user, email_rows=email_rows, whatsapp_rows=whatsapp_rows,
             show_whatsapp=can_view_whatsapp, can_add_email=can_add_email,
             can_add_whatsapp=can_add_whatsapp,
             can_edit="email.connections.edit" in perms,
             can_delete="email.connections.delete" in perms,
             can_validate="email.connections.validate" in perms,
             can_health="email.connections.health" in perms,
             flash=request.query_params.get("flash")),
    )


def _wizard_ctx(request, user, step: int, error: str | None = None, form: dict | None = None):
    return _ctx(request, user, step=step, error=error, form=form or {})


@router.get("/connections/email/new", response_class=HTMLResponse)
async def new_email_account(
    request: Request,
    user: Annotated[User, Depends(ui_user_for("email.connections.create"))],
):
    return templates.TemplateResponse(
        request, "marketing/account_wizard.html", _wizard_ctx(request, user, 1, channel="EMAIL")
    )


@router.get("/connections/whatsapp/new", response_class=HTMLResponse)
async def new_whatsapp_account(
    request: Request,
    user: Annotated[User, Depends(ui_user_for("whatsapp.connections.create"))],
):
    return templates.TemplateResponse(
        request, "marketing/account_wizard.html", _wizard_ctx(request, user, 1, channel="WHATSAPP")
    )


@router.post("/connections/email")
async def create_email_account_ui(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("email.connections.create"))],
    name: str = Form(...),
    provider: str = Form(...),
    sender_name: str = Form(""),
    sender_email: str = Form(...),
    reply_to: str = Form(""),
    host: str = Form(""),
    port: str = Form("587"),
    security: str = Form("STARTTLS"),
    api_base_url: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    api_key: str = Form(""),
):
    if provider == "smtp":
        config = {"host": host.strip(), "port": int(port or 587), "security": security}
        credentials = {"username": username.strip(), "password": password}
    else:
        config = {"api_base_url": api_base_url.strip()}
        credentials = {"api_key": api_key}
    try:
        account = await _accounts_service(request).create(
            session,
            channel="EMAIL",
            provider=provider,
            name=name.strip(),
            config=config,
            credentials=credentials,
            sender_name=sender_name.strip() or None,
            sender_email=sender_email.strip(),
            reply_to=reply_to.strip() or None,
            created_by=user.id,
            is_production=request.app.state.settings.is_production,
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — show honest provider error
        detail = getattr(exc, "message", str(exc))
        return templates.TemplateResponse(
            request, "marketing/account_wizard.html",
            _wizard_ctx(request, user, 4, error=detail,
                        form={"name": name, "provider": provider, "sender_email": sender_email}),
            status_code=422,
        )
    return RedirectResponse(url="/connections?flash=Account+added+-+run+Validate+to+activate", status_code=303)


@router.post("/connections/whatsapp")
async def create_whatsapp_account_ui(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("whatsapp.connections.create"))],
    name: str = Form(...),
    phone_number_id: str = Form(...),
    business_account_id: str = Form(""),
    access_token: str = Form(...),
    api_version: str = Form("v21.0"),
):
    try:
        account = await _accounts_service(request).create(
            session,
            channel="WHATSAPP",
            provider="whatsapp_cloud",
            name=name.strip(),
            config={"api_version": api_version, "phone_number_id": phone_number_id.strip()},
            credentials={"access_token": access_token},
            phone_number_id=phone_number_id.strip(),
            business_account_id=business_account_id.strip() or None,
            created_by=user.id,
            is_production=request.app.state.settings.is_production,
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            request, "marketing/account_wizard.html",
            _wizard_ctx(request, user, 4, error=getattr(exc, "message", str(exc)),
                        form={"name": name, "phone_number_id": phone_number_id}),
            status_code=422,
        )
    return RedirectResponse(url="/connections?flash=Account+added", status_code=303)


async def _channel_account(session: AsyncSession, channel: str, account_id: uuid.UUID):
    service_account = await session.get(SendingAccount, account_id)
    if service_account is None or service_account.channel != channel:
        raise UiRedirect("/connections")
    return service_account


@router.post("/connections/{channel}/{account_id}/validate")
async def validate_account_ui(
    channel: str,
    account_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("email.connections.validate"))],
):
    if channel == "WHATSAPP" and "whatsapp.connections.validate" not in await _perms(session, user):
        raise UiRedirect("/403")
    account = await _channel_account(session, channel.upper(), account_id)
    await _accounts_service(request).validate(session, account)
    await session.commit()
    return RedirectResponse(url="/connections?flash=Validation+finished", status_code=303)


@router.post("/connections/{channel}/{account_id}/health")
async def health_account_ui(
    channel: str,
    account_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("email.connections.health"))],
):
    if channel == "WHATSAPP" and "whatsapp.connections.health" not in await _perms(session, user):
        raise UiRedirect("/403")
    account = await _channel_account(session, channel.upper(), account_id)
    await _accounts_service(request).health_check(session, account)
    await session.commit()
    return RedirectResponse(url="/connections?flash=Health+check+finished", status_code=303)


@router.post("/connections/{channel}/{account_id}/delete")
async def delete_account_ui(
    channel: str,
    account_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("email.connections.delete"))],
):
    if channel == "WHATSAPP" and "whatsapp.connections.delete" not in await _perms(session, user):
        raise UiRedirect("/403")
    account = await _channel_account(session, channel.upper(), account_id)
    await _accounts_service(request).delete(session, account)
    await session.commit()
    return RedirectResponse(url="/connections?flash=Account+removed", status_code=303)


# ------------------------------------------------------------------ campaigns
@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_home(
    request: Request,
    user: Annotated[User, Depends(ui_user_for("campaign.view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await session.scalars(
        select(Campaign).order_by(Campaign.created_at.desc()).limit(100)
    )).all()
    return templates.TemplateResponse(
        request, "marketing/campaigns.html",
        _ctx(request, user, campaigns=[c.to_public_dict() for c in rows],
             can_create="campaign.create" in await _perms(session, user)),
    )


async def _wizard_options(session: AsyncSession):
    templates_rows = (await session.scalars(
        select(MarketingTemplate).order_by(MarketingTemplate.updated_at.desc())
    )).all()
    accounts = (await session.scalars(
        select(SendingAccount).order_by(SendingAccount.created_at.desc())
    )).all()
    return templates_rows, accounts


@router.get("/campaigns/new", response_class=HTMLResponse)
async def new_campaign_wizard(
    request: Request,
    user: Annotated[User, Depends(ui_user_for("campaign.create"))],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    templates_rows, accounts = await _wizard_options(session)
    return templates.TemplateResponse(
        request, "marketing/campaign_wizard.html",
        _ctx(request, user, step=1, templates=[t.to_public_dict() for t in templates_rows],
             accounts=[a.to_public_dict() for a in accounts], error=None, form={}),
    )


@router.post("/campaigns/new")
async def create_campaign_ui(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("campaign.create"))],
    name: str = Form(...),
    channel: str = Form(...),
    template_id: str = Form(""),
    sending_account_id: str = Form(""),
    audience_filter: str = Form(""),
    require_opt_in: str = Form("off"),
    company_name: str = Form(""),
    company_address: str = Form(""),
    emails_per_minute: str = Form(""),
    track_opens: str = Form("off"),
    track_clicks: str = Form("off"),
    launch_now: str = Form("off"),
):
    import json as json_module

    try:
        filter_spec = json_module.loads(audience_filter) if audience_filter.strip() else {}
    except json_module.JSONDecodeError:
        templates_rows, accounts = await _wizard_options(session)
        return templates.TemplateResponse(
            request, "marketing/campaign_wizard.html",
            _ctx(request, user, step=3, templates=[t.to_public_dict() for t in templates_rows],
                 accounts=[a.to_public_dict() for a in accounts],
                 error="Audience filter must be valid JSON", form={}),
            status_code=422,
        )
    rate = {}
    if emails_per_minute.strip().isdigit():
        rate = {"emails_per_minute": int(emails_per_minute)}
    service = CampaignService()
    try:
        campaign = await service.create(
            session,
            name=name.strip(),
            channel=channel.upper(),
            template_id=uuid.UUID(template_id) if template_id else None,
            sending_account_id=uuid.UUID(sending_account_id) if sending_account_id else None,
            audience={
                "filter": filter_spec,
                "require_opt_in": require_opt_in == "on",
                "company_name": company_name.strip(),
                "company_address": company_address.strip(),
            },
            rate_config=rate,
            track_opens=track_opens == "on",
            track_clicks=track_clicks == "on",
            created_by=user.id,
        )
        if launch_now == "on":
            queue = build_marketing_queue(request.app.state.settings, request.app.state.redis)
            await service.launch(session, campaign, queue, actor=user.id)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — honest validation errors
        templates_rows, accounts = await _wizard_options(session)
        detail = getattr(exc, "message", str(exc))
        issues = getattr(exc, "details", {}).get("issues")
        if issues:
            detail = "; ".join(i["message"] for i in issues)
        return templates.TemplateResponse(
            request, "marketing/campaign_wizard.html",
            _ctx(request, user, step=10, templates=[t.to_public_dict() for t in templates_rows],
                 accounts=[a.to_public_dict() for a in accounts], error=detail, form={}),
            status_code=422,
        )
    return RedirectResponse(url=f"/campaigns/{campaign.id}", status_code=303)


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(
    campaign_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(ui_user_for("campaign.view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise UiRedirect("/campaigns")
    summary = await AnalyticsService().campaign_summary(session, campaign)
    template = await session.get(MarketingTemplate, campaign.template_id) if campaign.template_id else None
    account = await session.get(SendingAccount, campaign.sending_account_id) if campaign.sending_account_id else None
    perms = await _perms(session, user)
    can_launch = (
        ("campaigns.email.launch" in perms and campaign.channel == "EMAIL")
        or ("campaigns.whatsapp.launch" in perms and campaign.channel == "WHATSAPP")
    )
    return templates.TemplateResponse(
        request, "marketing/campaign_detail.html",
        _ctx(request, user, campaign=campaign.to_public_dict(), analytics=summary,
             template_name=template.name if template else None,
             account=account.to_public_dict() if account else None,
             can_launch=can_launch, can_control="campaign.create" in perms,
             flash=request.query_params.get("flash")),
    )


async def _campaign_action(request, campaign_id, user, session, action: str):
    service = CampaignService()
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise UiRedirect("/campaigns")
    queue = build_marketing_queue(request.app.state.settings, request.app.state.redis)
    if action == "pause":
        await service.pause(session, campaign)
    elif action == "resume":
        await service.resume(session, campaign, queue)
    elif action == "cancel":
        await service.cancel(session, campaign)
    elif action == "requeue":
        await service.requeue_failed(session, campaign)
    await session.commit()
    return RedirectResponse(url=f"/campaigns/{campaign_id}?flash={action}+done", status_code=303)


@router.post("/campaigns/{campaign_id}/pause")
async def campaign_pause_ui(
    campaign_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("campaign.create"))],
):
    return await _campaign_action(request, campaign_id, user, session, "pause")


@router.post("/campaigns/{campaign_id}/resume")
async def campaign_resume_ui(
    campaign_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("campaign.create"))],
):
    return await _campaign_action(request, campaign_id, user, session, "resume")


@router.post("/campaigns/{campaign_id}/cancel")
async def campaign_cancel_ui(
    campaign_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("campaign.create"))],
):
    return await _campaign_action(request, campaign_id, user, session, "cancel")


@router.post("/campaigns/{campaign_id}/requeue")
async def campaign_requeue_ui(
    campaign_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("campaign.create"))],
):
    return await _campaign_action(request, campaign_id, user, session, "requeue")


# ------------------------------------------------------------------ templates
@router.get("/marketing/templates", response_class=HTMLResponse)
async def templates_home(
    request: Request,
    user: Annotated[User, Depends(ui_user_for("marketing.view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await session.scalars(
        select(MarketingTemplate).order_by(MarketingTemplate.updated_at.desc())
    )).all()
    perms = await _perms(session, user)
    return templates.TemplateResponse(
        request, "marketing/templates.html",
        _ctx(request, user, templates_list=[t.to_public_dict() for t in rows],
             can_manage="email.templates.manage" in perms,
             can_manage_whatsapp="whatsapp.templates.manage" in perms,
             flash=request.query_params.get("flash")),
    )


@router.post("/marketing/templates")
async def create_template_ui(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("marketing.view"))],
    name: str = Form(...),
    channel: str = Form("EMAIL"),
    subject: str = Form(""),
    html_body: str = Form(""),
    text_body: str = Form(""),
    body: str = Form(""),
):
    perms = await _perms(session, user)
    needed = "email.templates.manage" if channel == "EMAIL" else "whatsapp.templates.manage"
    if needed not in perms:
        raise UiRedirect("/403")
    from app.services.marketing import templates as template_engine

    try:
        unknown = template_engine.validate_variable_usage(subject, html_body, text_body, body)
        if unknown:
            raise ValueError(f"Unknown variables: {', '.join(unknown)}")
        row = MarketingTemplate(
            name=name.strip(),
            channel=channel.upper(),
            status="DRAFT",
            subject=subject.strip() or None,
            html_body=template_engine.sanitize_html(html_body) if html_body else None,
            text_body=text_body or None,
            body=body or None,
            variables=sorted(set(template_engine.extract_variables(subject, html_body, text_body, body))),
            created_by=user.id,
        )
        session.add(row)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/marketing/templates?flash=Error:+{getattr(exc, 'message', str(exc))[:120]}",
            status_code=303,
        )
    return RedirectResponse(url="/marketing/templates?flash=Template+created", status_code=303)


@router.post("/marketing/templates/{template_id}/delete")
async def delete_template_ui(
    template_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("marketing.view"))],
):
    row = await session.get(MarketingTemplate, template_id)
    if row is None:
        raise UiRedirect("/marketing/templates")
    needed = "email.templates.manage" if row.channel == "EMAIL" else "whatsapp.templates.manage"
    if needed not in await _perms(session, user):
        raise UiRedirect("/403")
    await session.delete(row)
    await session.commit()
    return RedirectResponse(url="/marketing/templates?flash=Template+deleted", status_code=303)


# ---------------------------------------------------------------- suppression
@router.get("/marketing/suppression", response_class=HTMLResponse)
async def suppression_home(
    request: Request,
    user: Annotated[User, Depends(ui_user_for("suppression.email.view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await session.scalars(
        select(Suppression).order_by(Suppression.created_at.desc()).limit(300)
    )).all()
    perms = await _perms(session, user)
    return templates.TemplateResponse(
        request, "marketing/suppression.html",
        _ctx(request, user,
             suppressions=[s.to_public_dict() for s in rows],
             can_manage="suppression.email.manage" in perms,
             can_manage_whatsapp="suppression.whatsapp.manage" in perms,
             flash=request.query_params.get("flash")),
    )


@router.post("/marketing/suppression")
async def add_suppression_ui(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("suppression.email.view"))],
    channel: str = Form("EMAIL"),
    address: str = Form(...),
    reason: str = Form("MANUAL"),
    notes: str = Form(""),
):
    perms = await _perms(session, user)
    needed = "suppression.email.manage" if channel == "EMAIL" else "suppression.whatsapp.manage"
    if needed not in perms:
        raise UiRedirect("/403")
    try:
        await SuppressionService().add(
            session, channel=channel, address=address, reason=reason,
            source="manual", notes=notes or None, created_by=user.id,
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "message", str(exc))
        return RedirectResponse(url=f"/marketing/suppression?flash=Error:+{detail[:120]}", status_code=303)
    return RedirectResponse(url="/marketing/suppression?flash=Suppression+added", status_code=303)


@router.post("/marketing/suppression/{suppression_id}/delete")
async def delete_suppression_ui(
    suppression_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for("suppression.email.view"))],
):
    row = await session.get(Suppression, suppression_id)
    if row is None:
        raise UiRedirect("/marketing/suppression")
    perms = await _perms(session, user)
    needed = "suppression.email.manage" if row.channel == "EMAIL" else "suppression.whatsapp.manage"
    if needed not in perms:
        raise UiRedirect("/403")
    try:
        await SuppressionService().remove(
            session, channel=row.channel, address_norm=row.address_norm, actor=user.id
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — terminal reasons refuse removal
        detail = getattr(exc, "message", str(exc))
        return RedirectResponse(url=f"/marketing/suppression?flash=Error:+{detail[:150]}", status_code=303)
    return RedirectResponse(url="/marketing/suppression?flash=Removed", status_code=303)
