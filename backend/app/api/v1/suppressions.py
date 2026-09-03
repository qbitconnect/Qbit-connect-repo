"""Suppression endpoints (Phase 7 §47) + unsubscribe public endpoint
(§13, §14) + tracking endpoints (§29, §30).

Webhooks live in webhooks_marketing.py; this module covers the authenticated
suppression API and the two PUBLIC endpoints (unsubscribe, tracking).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.api.deps import DbSession, get_client_ip, get_user_agent, require_permission
from app.core.errors import ValidationError
from app.models.user import User
from app.schemas.marketing import ActionOut, ListOut, SuppressionCreate
from app.services.marketing.suppression import SuppressionService, UnsubscribeService

router = APIRouter(tags=["suppression"])
public_router = APIRouter(tags=["unsubscribe"])


# ===================================================================== API
@router.get("/api/v1/suppressions", response_model=ListOut)
async def list_suppressions(
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("suppression.email.view"))],
    channel: str | None = Query(default=None, pattern="^(EMAIL|WHATSAPP)$"),
    reason: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Note: when channel=WHATSAPP the whatsapp.view permission is enforced."""
    if channel == "WHATSAPP":
        from app.services import rbac as rbac_service

        granted = getattr(request.state, "permissions", None) or await rbac_service.load_user_permissions(
            session, actor.id
        )
        if "suppression.whatsapp.view" not in granted:
            from app.core.errors import PermissionDeniedError

            raise PermissionDeniedError("Missing required permission: suppression.whatsapp.view")
    rows = await SuppressionService().list_suppressions(
        session, channel=channel, reason=reason, limit=limit, offset=offset
    )
    return ListOut(data={"items": [s.to_public_dict() for s in rows], "total": len(rows)})


@router.post("/api/v1/suppressions", response_model=ActionOut, status_code=201)
async def create_suppression(
    payload: SuppressionCreate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("suppression.email.manage"))],
):
    if payload.channel == "WHATSAPP":
        from app.services import rbac as rbac_service

        granted = getattr(request.state, "permissions", None) or await rbac_service.load_user_permissions(
            session, actor.id
        )
        if "suppression.whatsapp.manage" not in granted:
            from app.core.errors import PermissionDeniedError

            raise PermissionDeniedError("Missing required permission: suppression.whatsapp.manage")
    row = await SuppressionService().add(
        session,
        channel=payload.channel,
        address=payload.address,
        reason=payload.reason,
        source="manual",
        notes=payload.notes,
        created_by=actor.id,
    )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="suppression.changed",
        actor_user_id=actor.id,
        resource_type="suppression",
        resource_id=str(row.id),
        metadata={"channel": payload.channel, "reason": payload.reason},
    )
    return ActionOut(data={"suppression": row.to_public_dict()})


@router.delete("/api/v1/suppressions/{suppression_id}", response_model=ActionOut)
async def delete_suppression(
    suppression_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("suppression.email.manage"))],
):
    from app.core.errors import NotFoundError
    from app.models.marketing import Suppression

    row = await session.get(Suppression, suppression_id)
    if row is None:
        raise NotFoundError("Suppression not found")
    permission = (
        "suppression.whatsapp.manage" if row.channel == "WHATSAPP" else "suppression.email.manage"
    )
    from app.services import rbac as rbac_service

    granted = getattr(request.state, "permissions", None) or await rbac_service.load_user_permissions(
        session, actor.id
    )
    if permission not in granted:
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError(f"Missing required permission: {permission}")
    address_norm = row.address_norm
    channel = row.channel
    removed = await SuppressionService().remove(
        session, channel=channel, address_norm=address_norm, actor=actor.id
    )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="suppression.changed",
        actor_user_id=actor.id,
        resource_type="suppression",
        resource_id=str(suppression_id),
        metadata={"removed": removed, "channel": channel},
    )
    return ActionOut(data={"removed": removed})


# ================================================================ PUBLIC
_UNSUBSCRIBE_PAGE = """
<!doctype html>
<html><head><meta charset="utf-8"><title>Unsubscribe</title>
<style>body{font-family:system-ui,sans-serif;background:#0b0f17;color:#e5e9f0;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#131a26;padding:2rem 3rem;border-radius:12px;text-align:center;max-width:480px}
h1{font-size:1.3rem;margin-top:0}p{color:#9aa5b5;line-height:1.5}
form{margin-top:1.5rem}button{background:#2563eb;color:#fff;border:0;padding:.7rem 1.6rem;
border-radius:8px;font-size:1rem;cursor:pointer}button:hover{background:#1d4ed8}
.ok{color:#34d399}.bad{color:#f87171}</style></head>
<body><div class="card">{body}</div></body></html>
"""


@public_router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_page(token: str, request: Request, session: DbSession):
    """Public opt-out page (no login; spec §13). Token is resolved but the
    suppression only takes effect after the explicit POST confirmation."""
    from app.core.errors import QBITError

    service = _unsubscribe_service(request)
    try:
        await service.resolve(session, token)
    except QBITError as exc:
        body = (
            f'<h1 class="bad">Link not valid</h1><p>{exc.message}</p>'
            "<p>If you believe this is an error, contact the sender directly.</p>"
        )
        return HTMLResponse(_UNSUBSCRIBE_PAGE.replace("{body}", body), status_code=200)
    body = (
        "<h1>Confirm unsubscribe</h1>"
        "<p>You will stop receiving marketing emails to this address. "
        "This cannot be undone silently — future campaigns will skip you.</p>"
        '<form method="post" action="/unsubscribe/{token}">'
        '<button type="submit">Confirm opt-out</button></form>'
    ).format(token=token)
    return HTMLResponse(_UNSUBSCRIBE_PAGE.replace("{body}", body))


@public_router.post("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_confirm(
    token: str,
    request: Request,
    session: DbSession,
):
    from app.core.errors import QBITError

    service = _unsubscribe_service(request)
    try:
        await service.confirm(session, token, ip=get_client_ip(request))
        await session.commit()
    except QBITError as exc:
        body = f'<h1 class="bad">Could not unsubscribe</h1><p>{exc.message}</p>'
        return HTMLResponse(_UNSUBSCRIBE_PAGE.replace("{body}", body), status_code=200)
    body = (
        '<h1 class="ok">You are unsubscribed</h1>'
        "<p>This address has been suppressed from future marketing emails. "
        "If you change your mind, you must explicitly re-subscribe with the sender.</p>"
    )
    return HTMLResponse(_UNSUBSCRIBE_PAGE.replace("{body}", body))


# ---------------------------------------------------------------- tracking
@public_router.get("/t/open/{token}")
async def track_open(token: str, request: Request, session: DbSession):
    """1x1 tracking pixel (optional, per-campaign opt-in)."""
    from app.services.marketing.campaigns import CampaignService
    from app.services.marketing.tracking import (
        TrackingService,
        png_pixel,
        verify_open_token,
    )

    settings = request.app.state.settings
    try:
        data = verify_open_token(token, settings.QBIT_SECRET_KEY)
    except ValidationError:
        return Response(content=png_pixel(), media_type="image/png")

    from app.models.marketing import Campaign, CampaignRecipient

    recipient = await session.get(CampaignRecipient, uuid.UUID(data["r"]))
    campaign = await session.get(Campaign, uuid.UUID(data["c"]))
    if recipient is not None and campaign is not None and campaign.track_opens:
        await TrackingService(settings.QBIT_SECRET_KEY).record_open(
            session,
            campaign=campaign,
            recipient=recipient,
            user_agent=get_user_agent(request),
            campaigns=CampaignService(),
        )
        await session.commit()
    return Response(content=png_pixel(), media_type="image/png", headers={"Cache-Control": "no-store"})


@public_router.get("/t/click/{token}")
async def track_click(token: str, request: Request, session: DbSession):
    """Signed click redirect — http/https destinations only (spec §30)."""
    from fastapi.responses import RedirectResponse

    from app.services.marketing.campaigns import CampaignService
    from app.services.marketing.tracking import (
        TrackingService,
        is_safe_destination,
        verify_click_token,
    )

    settings = request.app.state.settings
    try:
        data = verify_click_token(token, settings.QBIT_SECRET_KEY)
        url = data["u"]
        if not is_safe_destination(url):
            raise ValidationError("Unsafe destination")
    except ValidationError:
        from app.core.errors import NotFoundError

        raise NotFoundError("Tracking link is invalid")

    from app.models.marketing import Campaign, CampaignRecipient

    recipient = await session.get(CampaignRecipient, uuid.UUID(data["r"]))
    campaign = await session.get(Campaign, uuid.UUID(data["c"]))
    if recipient is not None and campaign is not None and campaign.track_clicks:
        await TrackingService(settings.QBIT_SECRET_KEY).record_click(
            session,
            campaign=campaign,
            recipient=recipient,
            url=url,
            user_agent=get_user_agent(request),
            campaigns=CampaignService(),
        )
        await session.commit()
    return RedirectResponse(url=url, status_code=302)


def _unsubscribe_service(request: Request) -> UnsubscribeService:
    settings = request.app.state.settings
    return UnsubscribeService(
        ttl_days=settings.QBIT_MARKETING_UNSUBSCRIBE_TOKEN_TTL_DAYS,
        base_url=settings.QBIT_PUBLIC_BASE_URL,
    )
