"""Campaign REST API (Phase 5 §29).

RBAC is enforced server-side on every endpoint (require_permission).
The provider registry is resolved from app.state (built at startup via
build_provider_registry — the mock provider can never appear in production).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditDep, DbSession, require_permission
from app.core.errors import ValidationError
from app.schemas.marketing import CampaignCreate, CampaignUpdate, ProviderEventRequest
from app.services.marketing import CampaignService, AnalyticsService
from app.services.marketing.audience import AudienceService
from app.services.marketing.events import EventService
from app.services.marketing.providers import MarketingProviderRegistry

logger = __import__("app.core.logging", fromlist=["get_logger"]).get_logger("qbit.api.campaigns")

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

campaigns_service = CampaignService()
audience_service = AudienceService()
analytics_service = AnalyticsService()
events_service = EventService()


def _registry(request: Request) -> MarketingProviderRegistry:
    registry = getattr(request.app.state, "marketing_providers", None)
    if registry is None:
        from app.services.marketing.providers import build_provider_registry

        registry = build_provider_registry(request.app.state.settings)
        request.app.state.marketing_providers = registry
    return registry


def _page(items: list, total: int, page: int, page_size: int) -> dict:
    return {
        "success": True,
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, -(-total // page_size)) if total else 1,
        },
    }


# ------------------------------------------------------------------- campaigns
@router.get("")
async def list_campaigns(
    session: DbSession,
    _user=Depends(require_permission("campaigns.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    status: str | None = Query(default=None, max_length=20),
    channel: str | None = Query(default=None, max_length=20),
    search: str = Query(default="", max_length=200),
):
    rows, total = await campaigns_service.list(
        session, status=status, channel=channel, search=search or None,
        page=page, page_size=page_size,
    )
    return _page([c.to_public_dict() for c in rows], total, page, page_size)


@router.post("", status_code=201)
async def create_campaign(
    payload: CampaignCreate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("campaigns.create")),
):
    campaign = await campaigns_service.create(
        session,
        name=payload.name, channel=payload.channel,
        description=payload.description,
        audience_definition=payload.audience_definition,
        template_id=_uuid_or_none(payload.template_id),
        sending_account_id=_uuid_or_none(payload.sending_account_id),
        schedule_type=payload.schedule_type,
        scheduled_at=payload.scheduled_at,
        timezone_name=payload.timezone,
        campaign_metadata=payload.campaign_metadata,
        created_by=user.id,
    )
    await audit.log(session, action="campaign.created", resource_type="campaign",
                    resource_id=str(campaign.id), actor_user_id=user.id,
                    metadata={"channel": campaign.channel, "name": campaign.name})
    return {"success": True, "data": campaign.to_public_dict()}


@router.get("/dashboard")
async def dashboard(
    session: DbSession,
    _user=Depends(require_permission("campaigns.view")),
):
    return {"success": True, "data": await analytics_service.dashboard_totals(session)}


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("campaigns.view")),
):
    campaign = await campaigns_service.get(session, campaign_id)
    return {"success": True, "data": campaign.to_public_dict()}


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("campaigns.edit")),
):
    campaign = await campaigns_service.update(
        session, campaign_id,
        name=payload.name, description=payload.description,
        audience_definition=payload.audience_definition,
        template_id=payload.template_id if payload.template_id is not None else "__unset__",
        sending_account_id=(
            payload.sending_account_id if payload.sending_account_id is not None else "__unset__"
        ),
        schedule_type=payload.schedule_type,
        scheduled_at=payload.scheduled_at,
        timezone_name=payload.timezone,
        campaign_metadata=payload.campaign_metadata,
        actor_id=user.id,
    )
    await audit.log(session, action="campaign.updated", resource_type="campaign",
                    resource_id=str(campaign.id), actor_user_id=user.id)
    return {"success": True, "data": campaign.to_public_dict()}


@router.post("/{campaign_id}/validate")
async def validate_campaign(
    campaign_id: uuid.UUID,
    session: DbSession,
    request: Request,
    audit: AuditDep,
    user=Depends(require_permission("campaigns.validate")),
):
    report = await campaigns_service.validate(
        session, campaign_id, actor_id=user.id, provider_registry=_registry(request),
        settings=request.app.state.settings,
    )
    await audit.log(session, action="campaign.validated", resource_type="campaign",
                    resource_id=str(campaign_id), actor_user_id=user.id,
                    metadata={"ok": report.get("ok", False)})
    return {"success": True, "data": report}


@router.post("/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: uuid.UUID,
    session: DbSession,
    request: Request,
    audit: AuditDep,
    user=Depends(require_permission("campaigns.launch")),
):
    # Phase 6 §37: WhatsApp launches additionally require the channel-scoped
    # permission, enforced server-side (never frontend-only)
    campaign = await campaigns_service.get(session, campaign_id)
    channel = (campaign.channel or "").upper()
    if channel == "WHATSAPP":
        from app.api.deps import require_permission as _rp
        await _rp("campaigns.whatsapp.launch")(request, session, user)
    # Phase 7 §48: EMAIL launches require campaigns.email.launch the same way
    if channel == "EMAIL":
        from app.api.deps import require_permission as _rp
        await _rp("campaigns.email.launch")(request, session, user)
    campaign = await campaigns_service.request_launch(
        session, campaign_id, actor_id=user.id, provider_registry=_registry(request),
        settings=request.app.state.settings,
    )
    await audit.log(session, action="campaign.launch_requested", resource_type="campaign",
                    resource_id=str(campaign_id), actor_user_id=user.id,
                    metadata={"status": campaign.status})
    return {"success": True, "data": campaign.to_public_dict()}


@router.post("/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("campaigns.pause")),
):
    campaign = await campaigns_service.pause(session, campaign_id)
    await audit.log(session, action="campaign.paused", resource_type="campaign",
                    resource_id=str(campaign_id), actor_user_id=user.id)
    return {"success": True, "data": campaign.to_public_dict()}


@router.post("/{campaign_id}/resume")
async def resume_campaign(
    campaign_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("campaigns.resume")),
):
    campaign = await campaigns_service.resume(session, campaign_id)
    await audit.log(session, action="campaign.resumed", resource_type="campaign",
                    resource_id=str(campaign_id), actor_user_id=user.id)
    return {"success": True, "data": campaign.to_public_dict()}


@router.post("/{campaign_id}/cancel")
async def cancel_campaign(
    campaign_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("campaigns.cancel")),
):
    campaign = await campaigns_service.cancel(session, campaign_id)
    await audit.log(session, action="campaign.cancelled", resource_type="campaign",
                    resource_id=str(campaign_id), actor_user_id=user.id)
    return {"success": True, "data": campaign.to_public_dict()}


@router.post("/{campaign_id}/archive")
async def archive_campaign(
    campaign_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("campaigns.edit")),
):
    campaign = await campaigns_service.archive(session, campaign_id)
    await audit.log(session, action="campaign.archived", resource_type="campaign",
                    resource_id=str(campaign_id), actor_user_id=user.id)
    return {"success": True, "data": campaign.to_public_dict()}


# ------------------------------------------------------------------ recipients
@router.get("/{campaign_id}/recipients")
async def list_recipients(
    campaign_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("campaigns.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None, max_length=20),
):
    from sqlalchemy import func, select

    from app.models.marketing import CampaignRecipient

    await campaigns_service.get(session, campaign_id)
    query = select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id)
    count_q = select(func.count()).select_from(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign_id
    )
    if status:
        query = query.where(CampaignRecipient.status == status.upper())
        count_q = count_q.where(CampaignRecipient.status == status.upper())
    total = await session.scalar(count_q)
    rows = await session.execute(
        query.order_by(CampaignRecipient.created_at)
        .offset((page - 1) * page_size).limit(page_size)
    )
    return _page([r.to_public_dict() for r in rows.scalars().all()],
                 int(total or 0), page, page_size)


@router.get("/{campaign_id}/recipients/{recipient_id}")
async def get_recipient(
    campaign_id: uuid.UUID,
    recipient_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("campaigns.view")),
):
    """Recipient detail (§28): eligibility, queue status, provider id, events."""
    from sqlalchemy import select

    from app.models.marketing import CampaignQueueItem

    await campaigns_service.get(session, campaign_id)
    from app.models.marketing import CampaignRecipient

    recipient = await session.get(CampaignRecipient, recipient_id)
    if recipient is None or recipient.campaign_id != campaign_id:
        from app.core.errors import NotFoundError

        raise NotFoundError("Recipient not found")
    lead = None
    if recipient.lead_id:
        from app.models.scrape import Lead

        lead_row = await session.get(Lead, recipient.lead_id)
        if lead_row is not None:
            lead = {
                "id": str(lead_row.id),
                "business_name": lead_row.business_name,
                "contact_name": lead_row.contact_name,
                "email": lead_row.email,
                "phone": lead_row.phone,
                "city": lead_row.city,
            }
    queue_item = (await session.execute(
        select(CampaignQueueItem).where(
            CampaignQueueItem.recipient_id == recipient.id,
            CampaignQueueItem.message_version == 1,
        ).order_by(CampaignQueueItem.created_at.desc()).limit(1)
    )).scalars().first()
    events, _total = await events_service.list_events(
        session, campaign_id=campaign_id, recipient_id=recipient.id, page_size=50,
    )
    return {
        "success": True,
        "data": {
            "recipient": recipient.to_public_dict(),
            "lead": lead,
            "queue": queue_item.to_public_dict() if queue_item else None,
            "events": [e.to_public_dict() for e in events],
        },
    }


# ---------------------------------------------------------------------- events
@router.get("/{campaign_id}/events")
async def list_events(
    campaign_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("campaigns.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    event_type: str | None = Query(default=None, max_length=50),
):
    await campaigns_service.get(session, campaign_id)
    rows, total = await events_service.list_events(
        session, campaign_id=campaign_id, event_type=event_type,
        page=page, page_size=page_size,
    )
    return _page([e.to_public_dict() for e in rows], total, page, page_size)


# ------------------------------------------------------------------- analytics
@router.get("/{campaign_id}/analytics")
async def campaign_analytics(
    campaign_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("campaigns.analytics")),
):
    data = await analytics_service.campaign_analytics(session, campaign_id)
    if not data:
        from app.core.errors import NotFoundError

        raise NotFoundError("Campaign not found")
    data["event_counts"] = await analytics_service.event_counts(session, campaign_id)
    return {"success": True, "data": data}


# ------------------------------------------------------- email analytics (§45)
@router.get("/{campaign_id}/email/analytics")
async def email_campaign_analytics(
    campaign_id: uuid.UUID,
    session: DbSession,
    request: Request,
    _user=Depends(require_permission("campaigns.analytics")),
):
    """EMAIL-channel analytics (Phase 7 §34, §45): recipients, sent,
    delivered, bounced (hard/soft), complaints, opens, clicks, replies,
    unsubscribes + rates. All values come from actual events."""
    from app.api.deps import require_permission as _rp

    await _rp("campaigns.email.analytics")(request, session, _user)
    data = await analytics_service.email_campaign_analytics(session, campaign_id)
    if not data:
        from app.core.errors import NotFoundError

        raise NotFoundError("Campaign not found")
    if (data.get("email") or {}).get("channel") != "EMAIL":
        campaign = await campaigns_service.get(session, campaign_id)
        from app.core.errors import ValidationError as _VE

        raise _VE(
            f"Campaign channel is {campaign.channel} — email analytics apply to EMAIL campaigns"
        )
    return {"success": True, "data": data}


# -------------------------------------------------- provider event ingestion
@router.post("/events/provider", include_in_schema=True)
async def ingest_provider_event(
    payload: ProviderEventRequest,
    session: DbSession,
    request: Request,
    _user=Depends(require_permission("campaigns.edit")),
):
    """Generic provider event interface (§37): normalize → apply.

    Provider webhooks will call dedicated endpoints in the provider phases;
    this endpoint exposes the normalization pipeline for operators/tests.
    """
    registry = _registry(request)
    normalized = await events_service.normalize_provider_event(registry, payload.model_dump())
    from app.models.marketing import CampaignRecipient

    provider_message_id = normalized.get("provider_message_id")
    applied = 0
    if provider_message_id:
        recipient = (await session.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.provider_message_id == provider_message_id
            ).limit(1)
        )).scalars().first()
        if recipient is not None:
            from app.services.marketing.worker import _apply_event_to_recipient

            applied = await _apply_event_to_recipient(session, recipient, normalized)
    return {"success": True, "data": {"normalized": normalized, "applied": applied}}


def _uuid_or_none(raw: str | None) -> uuid.UUID | None:
    if raw in (None, ""):
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Invalid UUID: {raw!r}") from exc
