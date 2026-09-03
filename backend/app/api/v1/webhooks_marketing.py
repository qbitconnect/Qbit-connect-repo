"""Provider webhook endpoints (Phase 7 §27, §28).

    POST /api/v1/webhooks/email/{provider}   — generic HMAC-verified email events
    GET  /api/v1/webhooks/whatsapp           — official Cloud API verification challenge
    POST /api/v1/webhooks/whatsapp           — X-Hub-Signature-256 verified events

These routes are PUBLIC (providers cannot authenticate as users); every
payload is verified via provider-supported schemes before anything is applied.
Invalid requests are rejected with 401; secrets/authorization headers are
never logged (§28, §57).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import PlainTextResponse

from app.api.deps import DbSession
from app.core.errors import NotFoundError, ValidationError
from app.services.marketing.webhooks import (
    MarketingWebhookService,
    WebhookVerificationError,
    verify_whatsapp_challenge,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/email/{provider}")
async def email_webhook(
    provider: str,
    request: Request,
    session: DbSession,
):
    if provider not in ("email_api", "mock_email", "smtp"):
        raise NotFoundError(f"Unknown email provider webhook: {provider}")
    raw_body = await request.body()
    if len(raw_body) > 1_000_000:
        raise ValidationError("Webhook payload too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    settings = request.app.state.settings

    # The webhook shared secret comes from the provider configuration; a
    # single platform-level secret is supported via env for simple setups.
    secret = getattr(settings, "QBIT_MARKETING_WEBHOOK_SECRET", None)
    service = MarketingWebhookService(
        timestamp_tolerance=settings.QBIT_MARKETING_WEBHOOK_TIMESTAMP_TOLERANCE
    )
    try:
        result = await service.process_email_webhook(
            session,
            provider=provider,
            raw_body=raw_body,
            headers=headers,
            secret=secret,
        )
    except WebhookVerificationError:
        raise
    return {"success": True, "data": result}


@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_verify(
    request: Request,
    session: DbSession,
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    settings = request.app.state.settings
    expected = getattr(settings, "QBIT_MARKETING_WHATSAPP_VERIFY_TOKEN", None)
    challenge = await verify_whatsapp_challenge(
        {
            "hub.mode": hub_mode,
            "hub.verify_token": hub_verify_token,
            "hub.challenge": hub_challenge,
        },
        expected_token=expected,
    )
    if challenge is None:
        raise NotFoundError("Not a verification request")
    return PlainTextResponse(challenge)


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    session: DbSession,
):
    raw_body = await request.body()
    if len(raw_body) > 1_000_000:
        raise ValidationError("Webhook payload too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    settings = request.app.state.settings
    app_secret = getattr(settings, "QBIT_MARKETING_WHATSAPP_APP_SECRET", None)
    service = MarketingWebhookService(
        timestamp_tolerance=settings.QBIT_MARKETING_WEBHOOK_TIMESTAMP_TOLERANCE
    )
    result = await service.process_whatsapp_webhook(
        session,
        raw_body=raw_body,
        headers=headers,
        app_secret=app_secret,
    )
    return {"success": True, "data": result}
