"""Phase 5 campaign UI — server-rendered marketing control center.

    GET  /campaigns                    dashboard cards + campaign list
    GET  /campaigns/new                creation wizard (9-step single form)
    POST /campaigns/preview            eligibility preview (wizard step 6)
    POST /campaigns                    create campaign (wizard step 8/9)
    GET  /campaigns/{id}               detail: overview/recipients/events/analytics
    POST /campaigns/{id}/validate|launch|pause|resume|cancel|archive
    GET  /campaigns/templates          template list + create form
    POST /campaigns/templates          create template
    POST /campaigns/templates/{id}/delete
    GET  /campaigns/accounts           sending accounts (never shows secrets)
    POST /campaigns/accounts           create sending account
    GET  /campaigns/suppression        suppression list + opt-outs
    POST /campaigns/suppression        add entry / record opt-out
    POST /campaigns/suppression/{id}/delete

Thin client: every action calls the service layer; no marketing logic lives
in templates; all numbers come from backend queries (§34, §35). The UI never
pretends a provider is connected — unconfigured providers surface an honest
"Provider not configured" state and launch is blocked.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import QBITError
from app.models.marketing import Campaign, CampaignTemplate, SendingAccount
from app.services.marketing import (
    AnalyticsService,
    AudienceService,
    CampaignService,
    SuppressionService,
    TemplateService,
)
from app.services.marketing.providers import build_provider_registry
from app.ui import _ctx, require_ui_permission, templates, ui_user_for

campaigns_view = ui_user_for("campaigns.view")
require_campaigns_create = require_ui_permission("campaigns.create")
require_campaigns_edit = require_ui_permission("campaigns.edit")
require_campaigns_validate = require_ui_permission("campaigns.validate")
require_campaigns_launch = require_ui_permission("campaigns.launch")
require_campaigns_pause = require_ui_permission("campaigns.pause")
require_campaigns_resume = require_ui_permission("campaigns.resume")
require_campaigns_cancel = require_ui_permission("campaigns.cancel")
require_templates_view = require_ui_permission("templates.view")
require_templates_create = require_ui_permission("templates.create")
require_templates_delete = require_ui_permission("templates.delete")
require_accounts_view = require_ui_permission("sending_accounts.view")
require_accounts_manage = require_ui_permission("sending_accounts.manage")
require_suppression_view = require_ui_permission("suppression.view")
require_suppression_manage = require_ui_permission("suppression.manage")

router = APIRouter(tags=["campaigns-ui"])

campaigns_service = CampaignService()
audience_service = AudienceService()
template_service = TemplateService()
suppression_service = SuppressionService()
analytics_service = AnalyticsService()

CHANNEL_CHOICES = [("WHATSAPP", "WhatsApp"), ("EMAIL", "Email"), ("SMS", "SMS (architecture only)")]
AUDIENCE_TYPES = [("filters", "Filters"), ("saved_view", "Saved view"), ("tags", "Tags"), ("selected", "Selected leads")]


def _perms(request: Request) -> set[str]:
    return getattr(request.state, "ui_permissions", None) or set()


def _registry(request: Request):
    registry = getattr(request.app.state, "marketing_providers", None)
    if registry is None:
        registry = build_provider_registry(request.app.state.settings)
        request.app.state.marketing_providers = registry
    return registry


def _flash(request: Request) -> tuple[str | None, str | None]:
    return request.query_params.get("ok"), request.query_params.get("err")


def _redirect(url: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    from urllib.parse import quote

    sep = "&" if "?" in url else "?"
    if ok:
        url += f"{sep}ok={quote(ok[:200])}"
    elif err:
        url += f"{sep}err={quote(err[:300])}"
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ dashboard
@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_index(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(campaigns_view)],
    status: str = Query(default="", max_length=20),
    page: int = Query(default=1, ge=1),
    ok: str | None = None,
    err: str | None = None,
):
    totals = await analytics_service.dashboard_totals(session)
    rows, total = await campaigns_service.list(session, status=status or None, page=page, page_size=20)
    counts = {}
    for c in rows:
        counts[str(c.id)] = await analytics_service.campaign_analytics(session, c.id)
    # honest provider state for the empty-state banner (§35)
    accounts, accounts_total = await _list_accounts(session)
    provider_configured = any(a.config_metadata.get("configured") for a in accounts)
    return templates.TemplateResponse(request, "campaigns/index.html", _ctx(
        request, user, totals=totals, campaigns=rows, total=total, page=page,
        page_size=20, counts=counts, status=status,
        statuses=["", "DRAFT", "SCHEDULED", "QUEUED", "RUNNING", "PAUSED", "COMPLETED", "CANCELLED", "FAILED", "ARCHIVED"],
        ok=ok, err=err, provider_configured=provider_configured,
        accounts_total=accounts_total,
        can_create="campaigns.create" in _perms(request),
    ))


async def _list_accounts(session: AsyncSession) -> tuple[list[SendingAccount], int]:
    from sqlalchemy import func, select

    from app.models.marketing import SendingAccount as SA

    total = await session.scalar(select(func.count()).select_from(SA))
    rows = await session.execute(select(SA).order_by(SA.created_at.desc()).limit(200))
    return list(rows.scalars().all()), int(total or 0)


async def _list_templates(session: AsyncSession) -> list[CampaignTemplate]:
    from sqlalchemy import select

    rows = await session.execute(
        select(CampaignTemplate).where(CampaignTemplate.status != "ARCHIVED")
        .order_by(CampaignTemplate.updated_at.desc()).limit(200)
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------- wizard
@router.get("/campaigns/new", response_class=HTMLResponse)
async def campaign_wizard(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_campaigns_create)],
    ok: str | None = None,
    err: str | None = None,
):
    from sqlalchemy import select

    from app.models.lead import SavedView

    views_rows = await session.execute(
        select(SavedView).where(SavedView.visibility != "PRIVATE")
        .order_by(SavedView.name).limit(200)
    )
    views = list(views_rows.scalars().all())
    templates_list = await _list_templates(session)
    accounts, _total = await _list_accounts(session)
    from app.models.lead import LeadTag

    tag_rows = await session.execute(select(LeadTag.name).limit(100))
    tags = [row[0] for row in tag_rows.all()]
    return templates.TemplateResponse(request, "campaigns/new.html", _ctx(
        request, user,
        channels=CHANNEL_CHOICES, audience_types=AUDIENCE_TYPES,
        saved_views=views, templates=templates_list, accounts=accounts, tags=tags,
        ok=ok, err=err,
        provider_configured=any(a.config_metadata.get("configured") for a in accounts),
    ))


def _audience_from_form(
    audience_type: str, saved_view_id: str, filters_raw: str,
    tags_raw: str, lead_ids_raw: str, statuses: str,
) -> dict:
    definition: dict = {"type": (audience_type or "filters").strip().lower()}
    if definition["type"] == "saved_view":
        definition["saved_view_id"] = saved_view_id.strip()
    elif definition["type"] == "filters":
        try:
            definition["filters"] = json.loads(filters_raw) if filters_raw.strip() else {}
        except ValueError as exc:
            raise QBITError("Audience filters must be valid JSON") from exc
    elif definition["type"] == "tags":
        definition["tags"] = [t.strip() for t in tags_raw.split(",") if t.strip()]
        definition["match"] = "any"
    elif definition["type"] == "selected":
        definition["lead_ids"] = [t.strip() for t in lead_ids_raw.replace("\n", ",").split(",") if t.strip()]
    status_list = [s.strip().upper() for s in statuses.split(",") if s.strip()]
    if status_list:
        definition["statuses"] = status_list
    return definition


@router.post("/campaigns/preview")
async def campaign_preview(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_campaigns_create)],
    name: str = Form(default=""),
    description: str = Form(default=""),
    channel: str = Form(...),
    audience_type: str = Form(...),
    saved_view_id: str = Form(default=""),
    filters_raw: str = Form(default=""),
    tags_raw: str = Form(default=""),
    lead_ids_raw: str = Form(default=""),
    statuses: str = Form(default=""),
    template_id: str = Form(default=""),
    sending_account_id: str = Form(default=""),
    schedule_type: str = Form(default="SEND_NOW"),
    scheduled_at: str = Form(default=""),
):
    """Wizard step 6: eligibility preview over the configured audience."""
    from app.services.marketing.eligibility import EligibilityService

    error = None
    report = None
    form = {
        "name": name, "description": description, "channel": channel,
        "audience_type": audience_type, "saved_view_id": saved_view_id,
        "filters_raw": filters_raw, "tags_raw": tags_raw,
        "lead_ids_raw": lead_ids_raw, "statuses": statuses,
        "template_id": template_id, "sending_account_id": sending_account_id,
        "schedule_type": schedule_type, "scheduled_at": scheduled_at,
    }
    try:
        definition = _audience_from_form(
            audience_type, saved_view_id, filters_raw, tags_raw, lead_ids_raw, statuses,
        )
        total = await audience_service.count(session, definition, user_id=user.id)
        eligible = skipped = suppressed = missing = no_opt_in = 0
        eligibility = EligibilityService()
        condition = await audience_service.build_condition(session, definition, user_id=user.id)
        last_id = None
        from sqlalchemy import select

        from app.models.scrape import Lead

        batch = 500
        while True:
            stmt = select(Lead).where(condition)
            if last_id is not None:
                stmt = stmt.where(Lead.id > last_id)
            stmt = stmt.order_by(Lead.id).limit(batch)
            leads = list((await session.execute(stmt)).scalars().all())
            if not leads:
                break
            last_id = leads[-1].id
            results = await eligibility.check_batch(
                session, channel=channel.upper(), leads=leads,
                provider_registry=_registry(request),
                opt_in_required=True,
            )
            for _lid, (status, reason) in results.items():
                if status == "ELIGIBLE":
                    eligible += 1
                else:
                    skipped += 1
                    if reason in ("SUPPRESSED", "UNSUBSCRIBED"):
                        suppressed += 1
                    elif reason in ("MISSING_PHONE", "MISSING_EMAIL", "INVALID_ADDRESS"):
                        missing += 1
                    elif reason == "NO_OPT_IN":
                        no_opt_in += 1
        report = {
            "total": total, "eligible": eligible, "skipped": skipped,
            "suppressed": suppressed, "missing": missing, "no_opt_in": no_opt_in,
        }
    except QBITError as exc:
        error = exc.message
    # re-render wizard with the preview block filled
    from sqlalchemy import select

    from app.models.lead import LeadTag, SavedView

    views_rows = await session.execute(
        select(SavedView).where(SavedView.visibility != "PRIVATE").order_by(SavedView.name).limit(200)
    )
    templates_list = await _list_templates(session)
    accounts, _t = await _list_accounts(session)
    tag_rows = await session.execute(select(LeadTag.name).limit(100))
    return templates.TemplateResponse(request, "campaigns/new.html", _ctx(
        request, user,
        channels=CHANNEL_CHOICES, audience_types=AUDIENCE_TYPES,
        saved_views=list(views_rows.scalars().all()), templates=templates_list,
        accounts=accounts, tags=[r[0] for r in tag_rows.all()],
        preview=report, preview_error=error,
        form=form,
        provider_configured=any(a.config_metadata.get("configured") for a in accounts),
        err=error,
    ))


@router.post("/campaigns")
async def campaign_create(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_campaigns_create)],
    name: str = Form(...),
    description: str = Form(default=""),
    channel: str = Form(...),
    audience_type: str = Form(...),
    saved_view_id: str = Form(default=""),
    filters_raw: str = Form(default=""),
    tags_raw: str = Form(default=""),
    lead_ids_raw: str = Form(default=""),
    statuses: str = Form(default=""),
    template_id: str = Form(default=""),
    sending_account_id: str = Form(default=""),
    schedule_type: str = Form(default="SEND_NOW"),
    scheduled_at: str = Form(default=""),
):
    try:
        definition = _audience_from_form(
            audience_type, saved_view_id, filters_raw, tags_raw, lead_ids_raw, statuses,
        )
        scheduled = None
        if schedule_type.upper() == "SCHEDULED":
            if not scheduled_at.strip():
                raise QBITError("Scheduled campaigns need a date/time")
            try:
                scheduled = datetime.fromisoformat(scheduled_at.strip()).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise QBITError("Invalid schedule date (use YYYY-MM-DD HH:MM)") from exc
        campaign = await campaigns_service.create(
            session,
            name=name, channel=channel, description=description or None,
            audience_definition=definition,
            template_id=_uuid(template_id), sending_account_id=_uuid(sending_account_id),
            schedule_type=schedule_type.upper() or "SEND_NOW",
            scheduled_at=scheduled, timezone_name="UTC", created_by=user.id,
        )
        return _redirect(f"/campaigns/{campaign.id}", ok="Campaign created — validate, then launch")
    except QBITError as exc:
        return _redirect("/campaigns/new", err=exc.message)


def _uuid(raw: str) -> uuid.UUID | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise QBITError("Invalid identifier") from exc


# ------------------------------------------------------------------ templates
@router.get("/campaigns/templates", response_class=HTMLResponse)
async def templates_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_templates_view)],
    ok: str | None = None,
    err: str | None = None,
):
    templates_list = await _list_templates(session)
    return templates.TemplateResponse(request, "campaigns/templates.html", _ctx(
        request, user, templates=templates_list, channels=CHANNEL_CHOICES,
        ok=ok, err=err,
        can_create="templates.create" in _perms(request),
        can_delete="templates.delete" in _perms(request),
    ))


@router.post("/campaigns/templates")
async def template_create(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_templates_create)],
    name: str = Form(...),
    channel: str = Form(...),
    subject: str = Form(default=""),
    body: str = Form(...),
):
    try:
        await template_service.create(
            session, name=name, channel=channel.upper(),
            subject=subject or None, body=body,
            status="ACTIVE", created_by=user.id,
        )
        return _redirect("/campaigns/templates", ok="Template created")
    except QBITError as exc:
        return _redirect("/campaigns/templates", err=exc.message)


@router.post("/campaigns/templates/{template_id}/delete")
async def template_delete(
    template_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_templates_delete)],
):
    try:
        await template_service.delete(session, template_id)
        return _redirect("/campaigns/templates", ok="Template removed (archived if in use)")
    except QBITError as exc:
        return _redirect("/campaigns/templates", err=exc.message)


# ----------------------------------------------------------- sending accounts
@router.get("/campaigns/accounts", response_class=HTMLResponse)
async def accounts_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_accounts_view)],
    ok: str | None = None,
    err: str | None = None,
):
    accounts, total = await _list_accounts(session)
    registry = _registry(request)
    account_data = []
    for a in accounts:
        provider = registry.get(a.provider)
        account_data.append({
            "row": a,
            "test_only": bool(provider.test_only) if provider else False,
            "available": provider is not None,
            "configured": bool((a.config_metadata or {}).get("configured")),
        })
    return templates.TemplateResponse(request, "campaigns/accounts.html", _ctx(
        request, user, accounts=account_data, accounts_total=total,
        channels=CHANNEL_CHOICES,
        ok=ok, err=err,
        can_manage="sending_accounts.manage" in _perms(request),
    ))


@router.post("/campaigns/accounts")
async def account_create(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_accounts_manage)],
    name: str = Form(...),
    channel: str = Form(...),
    provider: str = Form(...),
    identifier: str = Form(...),
    display_identifier: str = Form(default=""),
    from_address: str = Form(default=""),
    phone_number_id: str = Form(default=""),
    rate_per_minute: str = Form(default=""),
):
    from app.core.errors import ValidationError as QValidationError
    from app.services.marketing.channels import get_channel

    try:
        spec = get_channel(channel.upper())
        if spec is None:
            raise QBITError("Unknown channel")
        if provider not in spec.providers:
            raise QBITError(f"Provider '{provider}' cannot serve {channel}")
        config: dict = {}
        if from_address.strip():
            config["from_address"] = from_address.strip()
        if phone_number_id.strip():
            config["phone_number_id"] = phone_number_id.strip()
        if rate_per_minute.strip().isdigit():
            config["rate_policy"] = {"messages_per_minute": int(rate_per_minute)}
        # NOTE: no credentials accepted here — vault arrives in a later phase
        account = SendingAccount(
            name=" ".join(name.split())[:150], channel=channel.upper(),
            provider=provider, identifier=identifier.strip()[:300],
            display_identifier=(display_identifier or identifier).strip()[:300],
            capabilities={}, config_metadata=config,
        )
        session.add(account)
        await session.commit()
        return _redirect("/campaigns/accounts", ok="Sending account created (status PENDING until a provider is configured)")
    except (QBITError, QValidationError) as exc:
        return _redirect("/campaigns/accounts", err=getattr(exc, "message", str(exc)))


# ----------------------------------------------------------------- suppression
@router.get("/campaigns/suppression", response_class=HTMLResponse)
async def suppression_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_suppression_view)],
    ok: str | None = None,
    err: str | None = None,
    page: int = Query(default=1, ge=1),
):
    entries, total = await suppression_service.list_entries(session, page=page, page_size=30)
    opt_outs, opt_total = await suppression_service.list_opt_outs(session, page_size=20)
    return templates.TemplateResponse(request, "campaigns/suppression.html", _ctx(
        request, user, entries=entries, total=total, page=page, page_size=30,
        opt_outs=opt_outs, opt_total=opt_total,
        types=["EMAIL", "PHONE", "LEAD", "CHANNEL"], reasons=[
            "UNSUBSCRIBED", "BOUNCED", "COMPLAINT", "BLOCKED", "MANUAL", "PROVIDER_RESTRICTION",
        ],
        ok=ok, err=err,
        can_manage="suppression.manage" in _perms(request),
    ))


@router.post("/campaigns/suppression")
async def suppression_add(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_suppression_manage)],
    type: str = Form(...),
    address: str = Form(...),
    reason: str = Form(...),
    channel: str = Form(default=""),
):
    try:
        await suppression_service.add(
            session, entry_type=type.upper(), address=address, reason=reason.upper(),
            channel=(channel or None), source="ui", created_by=user.id,
        )
        return _redirect("/campaigns/suppression", ok="Suppression entry added")
    except QBITError as exc:
        return _redirect("/campaigns/suppression", err=exc.message)


@router.post("/campaigns/suppression/opt-out")
async def suppression_opt_out(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_suppression_manage)],
    channel: str = Form(...),
    address: str = Form(...),
):
    try:
        await suppression_service.record_opt_out(
            session, channel=channel.upper(), address=address, source="ui",
        )
        return _redirect("/campaigns/suppression", ok="Opt-out recorded and suppressed")
    except QBITError as exc:
        return _redirect("/campaigns/suppression", err=exc.message)


@router.post("/campaigns/suppression/{entry_id}/delete")
async def suppression_delete(
    entry_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_suppression_manage)],
):
    try:
        await suppression_service.remove(session, entry_id)
        return _redirect("/campaigns/suppression", ok="Suppression entry removed")
    except QBITError as exc:
        return _redirect("/campaigns/suppression", err=exc.message)
# --------------------------------------------------------------------- detail
@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(
    request: Request,
    campaign_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(campaigns_view)],
    ok: str | None = None,
    err: str | None = None,
    recipients_page: int = Query(default=1, ge=1),
):
    try:
        campaign = await campaigns_service.get(session, campaign_id)
    except QBITError:
        return _redirect("/campaigns", err="Campaign not found")
    analytics = await analytics_service.campaign_analytics(session, campaign_id)
    recipients, rec_total = await _recipients_page(session, campaign_id, recipients_page)
    events, _ev_total = await _events_page(session, campaign_id)
    template = (
        await session.get(CampaignTemplate, campaign.template_id) if campaign.template_id else None
    )
    account = (
        await session.get(SendingAccount, campaign.sending_account_id)
        if campaign.sending_account_id else None
    )
    perms = _perms(request)
    account_configured = bool(account and (account.config_metadata or {}).get("configured"))
    return templates.TemplateResponse(request, "campaigns/detail.html", _ctx(
        request, user, campaign=campaign, analytics=analytics,
        recipients=recipients, rec_total=rec_total, recipients_page=recipients_page,
        events=events, template=template, account=account,
        account_configured=account_configured,
        provider_label=_provider_label(account),
        ok=ok, err=err,
        can_validate="campaigns.validate" in perms,
        can_launch="campaigns.launch" in perms,
        can_pause="campaigns.pause" in perms,
        can_resume="campaigns.resume" in perms,
        can_cancel="campaigns.cancel" in perms,
        can_edit="campaigns.edit" in perms,
    ))


def _provider_label(account: SendingAccount | None) -> str:
    if account is None:
        return "No sending account"
    return f"{account.provider} ({account.display_identifier or account.identifier})"


async def _recipients_page(session: AsyncSession, campaign_id: uuid.UUID, page: int):
    from sqlalchemy import func, select

    from app.models.marketing import CampaignRecipient

    total = await session.scalar(
        select(func.count()).select_from(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id)
    )
    rows = await session.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id)
        .order_by(CampaignRecipient.created_at)
        .offset((page - 1) * 50).limit(50)
    )
    return list(rows.scalars().all()), int(total or 0)


async def _events_page(session: AsyncSession, campaign_id: uuid.UUID):
    from app.services.marketing.events import EventService

    return await EventService().list_events(session, campaign_id=campaign_id, page_size=50)


@router.post("/campaigns/{campaign_id}/validate")
async def campaign_validate(
    campaign_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
    user: Annotated[object, Depends(require_campaigns_validate)],
):
    try:
        report = await campaigns_service.validate(
            session, campaign_id, actor_id=user.id, provider_registry=_registry(request),
        )
        if report.get("ok"):
            return _redirect(f"/campaigns/{campaign_id}", ok="Validation passed")
        first_fail = next(
            (f"{k}: {v.get('detail') or 'failed'}" for k, v in report.get("checks", {}).items()
             if v.get("status") != "PASS"),
            "validation failed",
        )
        return _redirect(f"/campaigns/{campaign_id}", err=first_fail)
    except QBITError as exc:
        return _redirect(f"/campaigns/{campaign_id}", err=exc.message)


@router.post("/campaigns/{campaign_id}/launch")
async def campaign_launch(
    campaign_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
    user: Annotated[object, Depends(require_campaigns_launch)],
):
    try:
        campaign = await campaigns_service.request_launch(
            session, campaign_id, actor_id=user.id, provider_registry=_registry(request),
        )
        # small audiences: process the launch inline so the UI reflects it fast
        if campaign.status == "QUEUED":
            await campaigns_service.process_launch(
                session, campaign, actor_id=user.id,
                provider_registry=_registry(request),
                batch_size=request.app.state.settings.QBIT_MARKETING_SNAPSHOT_BATCH_SIZE,
                max_audience=request.app.state.settings.QBIT_MARKETING_MAX_AUDIENCE,
            )
        return _redirect(f"/campaigns/{campaign_id}", ok="Campaign launched")
    except QBITError as exc:
        return _redirect(f"/campaigns/{campaign_id}", err=exc.message)


@router.post("/campaigns/{campaign_id}/pause")
async def campaign_pause(
    campaign_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_campaigns_pause)],
):
    try:
        await campaigns_service.pause(session, campaign_id)
        return _redirect(f"/campaigns/{campaign_id}", ok="Campaign paused")
    except QBITError as exc:
        return _redirect(f"/campaigns/{campaign_id}", err=exc.message)


@router.post("/campaigns/{campaign_id}/resume")
async def campaign_resume(
    campaign_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_campaigns_resume)],
):
    try:
        await campaigns_service.resume(session, campaign_id)
        return _redirect(f"/campaigns/{campaign_id}", ok="Campaign resumed")
    except QBITError as exc:
        return _redirect(f"/campaigns/{campaign_id}", err=exc.message)


@router.post("/campaigns/{campaign_id}/cancel")
async def campaign_cancel(
    campaign_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_campaigns_cancel)],
):
    try:
        await campaigns_service.cancel(session, campaign_id)
        return _redirect(f"/campaigns/{campaign_id}", ok="Campaign cancelled")
    except QBITError as exc:
        return _redirect(f"/campaigns/{campaign_id}", err=exc.message)


@router.post("/campaigns/{campaign_id}/archive")
async def campaign_archive(
    campaign_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[object, Depends(require_campaigns_edit)],
):
    try:
        await campaigns_service.archive(session, campaign_id)
        return _redirect(f"/campaigns/{campaign_id}", ok="Campaign archived")
    except QBITError as exc:
        return _redirect(f"/campaigns/{campaign_id}", err=exc.message)


