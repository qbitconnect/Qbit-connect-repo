"""Campaign endpoints (Phase 7 §17, §34, §35, §36, §41, §45).

Launch requires the channel-specific permission (campaigns.email.launch /
campaigns.whatsapp.launch). Validation failures return the honest issues list
— never a fake success.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import DbSession, get_client_ip, get_user_agent, require_permission
from app.core.errors import PermissionDeniedError, ValidationError
from app.models.marketing import Campaign, CampaignRecipient
from app.models.user import User
from app.schemas.marketing import ActionOut, CampaignCreate, CampaignUpdate, ListOut
from app.services.marketing.analytics import AnalyticsService
from app.services.marketing.campaigns import CampaignService
from app.services.marketing.queue import build_marketing_queue

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def _queue(request: Request):
    return build_marketing_queue(request.app.state.settings, request.app.state.redis)


def _service() -> CampaignService:
    return CampaignService()


def _require_launch(channel: str, actor: User) -> None:
    code = "campaigns.email.launch" if channel == "EMAIL" else "campaigns.whatsapp.launch"
    if code not in getattr(actor, "_effective_permissions", set()):  # pragma: no cover
        pass  # enforcement happens through require_permission below


@router.get("", response_model=ListOut)
async def list_campaigns(
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.view"))],
    channel: str | None = Query(default=None, pattern="^(EMAIL|WHATSAPP)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    from sqlalchemy import func, select

    query = select(Campaign).order_by(Campaign.created_at.desc())
    count_query = select(func.count()).select_from(Campaign)
    if channel:
        query = query.where(Campaign.channel == channel)
        count_query = count_query.where(Campaign.channel == channel)
    total = await session.scalar(count_query)
    rows = (await session.scalars(query.limit(limit).offset(offset))).all()
    return ListOut(data={"items": [c.to_public_dict() for c in rows], "total": int(total or 0)})


@router.post("", response_model=ActionOut, status_code=201)
async def create_campaign(
    payload: CampaignCreate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.create"))],
):
    service = _service()
    schedule_at = None
    if payload.schedule_at:
        from datetime import datetime

        try:
            schedule_at = datetime.fromisoformat(payload.schedule_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("schedule_at must be an ISO-8601 datetime") from exc

    def _uuid_or_none(value: str | None) -> uuid.UUID | None:
        if not value:
            return None
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise ValidationError(f"Invalid uuid: {value}") from exc

    campaign = await service.create(
        session,
        name=payload.name,
        channel=payload.channel,
        template_id=_uuid_or_none(payload.template_id),
        sending_account_id=_uuid_or_none(payload.sending_account_id),
        audience=payload.audience,
        schedule_at=schedule_at,
        rate_config=payload.rate_config,
        track_opens=payload.track_opens,
        track_clicks=payload.track_clicks,
        created_by=actor.id,
    )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="campaign.created",
        actor_user_id=actor.id,
        resource_type="campaign",
        resource_id=str(campaign.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"channel": campaign.channel},
    )
    return ActionOut(data={"campaign": campaign.to_public_dict()})


@router.get("/{campaign_id}", response_model=ActionOut)
async def get_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.view"))],
):
    campaign = await _service().get(session, campaign_id)
    return ActionOut(data={"campaign": campaign.to_public_dict()})


@router.patch("/{campaign_id}", response_model=ActionOut)
async def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.create"))],
):
    service = _service()
    campaign = await service.get(session, campaign_id)
    if payload.name is not None:
        campaign.name = payload.name.strip()
    if payload.template_id is not None:
        try:
            campaign.template_id = uuid.UUID(payload.template_id)
        except ValueError as exc:
            raise ValidationError("Invalid template_id") from exc
    if payload.sending_account_id is not None:
        try:
            campaign.sending_account_id = uuid.UUID(payload.sending_account_id)
        except ValueError as exc:
            raise ValidationError("Invalid sending_account_id") from exc
    if payload.audience is not None:
        campaign.audience = payload.audience
    if payload.rate_config is not None:
        campaign.rate_config = payload.rate_config
    if payload.track_opens is not None:
        campaign.track_opens = int(payload.track_opens)
    if payload.track_clicks is not None:
        campaign.track_clicks = int(payload.track_clicks)
    await session.commit()
    return ActionOut(data={"campaign": campaign.to_public_dict()})


@router.post("/{campaign_id}/validate", response_model=ActionOut)
async def validate_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.view"))],
):
    campaign = await _service().get(session, campaign_id)
    result = await _service().validate(session, campaign)
    await session.commit()
    return ActionOut(data={"validation": result, "campaign": campaign.to_public_dict()})


@router.post("/{campaign_id}/launch", response_model=ActionOut)
async def launch_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[
        User,
        Depends(require_permission("campaign.view")),
    ],
):
    service = _service()
    campaign = await service.get(session, campaign_id)
    # channel-specific launch permission enforced here (backend-mandatory)
    required = "campaigns.email.launch" if campaign.channel == "EMAIL" else "campaigns.whatsapp.launch"
    granted = getattr(request.state, "permissions", None)
    if granted is None or required not in granted:
        raise PermissionDeniedError(f"Missing required permission: {required}")
    result = await service.launch(session, campaign, _queue(request), actor=actor.id)
    await request.app.state.audit.log(
        session,
        action="campaign.launched",
        actor_user_id=actor.id,
        resource_type="campaign",
        resource_id=str(campaign.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={
            "channel": campaign.channel,
            "queued": result.get("queued", {}).get("queued"),
        },
    )
    return ActionOut(
        data={"launch": result, "campaign": campaign.to_public_dict()}
    )


@router.post("/{campaign_id}/pause", response_model=ActionOut)
async def pause_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.create"))],
):
    service = _service()
    campaign = await service.get(session, campaign_id)
    await service.pause(session, campaign)
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="campaign.paused",
        actor_user_id=actor.id,
        resource_type="campaign",
        resource_id=str(campaign.id),
    )
    return ActionOut(data={"campaign": campaign.to_public_dict()})


@router.post("/{campaign_id}/resume", response_model=ActionOut)
async def resume_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.create"))],
):
    service = _service()
    campaign = await service.get(session, campaign_id)
    resumed = await service.resume(session, campaign, _queue(request))
    return ActionOut(data={"campaign": campaign.to_public_dict(), "re_enqueued": resumed})


@router.post("/{campaign_id}/cancel", response_model=ActionOut)
async def cancel_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.create"))],
):
    service = _service()
    campaign = await service.get(session, campaign_id)
    cancelled = await service.cancel(session, campaign)
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="campaign.cancelled",
        actor_user_id=actor.id,
        resource_type="campaign",
        resource_id=str(campaign.id),
        metadata={"cancelled_recipients": cancelled},
    )
    return ActionOut(data={"campaign": campaign.to_public_dict(), "cancelled_recipients": cancelled})


@router.post("/{campaign_id}/requeue-failed", response_model=ActionOut)
async def requeue_failed(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[
        User,
        Depends(require_permission("campaign.view")),
    ],
):
    service = _service()
    campaign = await service.get(session, campaign_id)
    required = "campaigns.email.launch" if campaign.channel == "EMAIL" else "campaigns.whatsapp.launch"
    granted = getattr(request.state, "permissions", None)
    if granted is None or required not in granted:
        raise PermissionDeniedError(f"Missing required permission: {required}")
    result = await service.requeue_failed(session, campaign)
    await session.commit()
    return ActionOut(data={"requeue": result, "campaign": campaign.to_public_dict()})


@router.get("/{campaign_id}/recipients", response_model=ListOut)
async def list_recipients(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaign.view"))],
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    from sqlalchemy import func, select

    await _service().get(session, campaign_id)
    query = (
        select(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id)
        .order_by(CampaignRecipient.created_at.asc())
    )
    count_query = (
        select(func.count())
        .select_from(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id)
    )
    if status:
        query = query.where(CampaignRecipient.status == status.upper())
        count_query = count_query.where(CampaignRecipient.status == status.upper())
    total = await session.scalar(count_query)
    rows = (await session.scalars(query.limit(limit).offset(offset))).all()
    return ListOut(data={"items": [r.to_public_dict() for r in rows], "total": int(total or 0)})


@router.get("/{campaign_id}/email/analytics", response_model=ActionOut)
async def email_campaign_analytics(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaigns.email.analytics"))],
):
    campaign = await _service().get(session, campaign_id)
    if campaign.channel != "EMAIL":
        raise ValidationError("Not an EMAIL campaign")
    summary = await AnalyticsService().campaign_summary(session, campaign)
    return ActionOut(data={"analytics": summary})


@router.get("/{campaign_id}/whatsapp/analytics", response_model=ActionOut)
async def whatsapp_campaign_analytics(
    campaign_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("campaigns.whatsapp.analytics"))],
):
    campaign = await _service().get(session, campaign_id)
    if campaign.channel != "WHATSAPP":
        raise ValidationError("Not a WHATSAPP campaign")
    summary = await AnalyticsService().campaign_summary(session, campaign)
    return ActionOut(data={"analytics": summary})
