"""WhatsApp webhook endpoints (Phase 6 §18, §19).

    GET  /api/v1/webhooks/whatsapp   Meta subscription handshake
                                     (hub.mode/hub.verify_token/hub.challenge)
    POST /api/v1/webhooks/whatsapp   event delivery: signature → parse →
                                     normalize → store idempotently → apply

These routes are intentionally NOT JWT-authenticated: they are called by the
provider. They are secured the way the provider requires — the verification
challenge token (GET) and the X-Hub-Signature-256 HMAC of the raw body keyed
with the app secret (POST). Invalid requests are rejected (401/400/413);
nothing secret-like is ever logged (§18, §44).

Processing is idempotent (§20): duplicate deliveries update nothing and
double-count nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import Settings
from app.core.errors import QBITError, ValidationError
from app.core.logging import get_logger
from app.services.marketing.webhooks import WhatsAppWebhookService

logger = get_logger("qbit.marketing.webhooks_api")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _webhook_unauthorized(message_text: str) -> QBITError:
    """401 for signature/auth failures — conventional for machine endpoints."""

    class _WebhookUnauthorized(QBITError):
        status_code = 401
        code = "WEBHOOK_UNAUTHORIZED"
        message = "Webhook unauthorized"

    error = _WebhookUnauthorized()
    error.message = message_text
    return error


def _service(request: Request) -> WhatsAppWebhookService:
    settings: Settings = request.app.state.settings
    return WhatsAppWebhookService(settings)


@router.get("/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook_verify(
    request: Request,
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """Subscription verification (§19 GET): echo hub.challenge ONLY when the
    verify token matches; otherwise reject."""
    service = _service(request)
    try:
        challenge = service.verify_challenge(
            mode=hub_mode, token=hub_verify_token, challenge=hub_challenge,
        )
    except ValidationError:
        logger.warning("Webhook verification rejected (token/mode mismatch)")
        raise _webhook_unauthorized("Webhook verification failed")
    if not challenge:
        raise ValidationError("hub.challenge is required")
    return PlainTextResponse(content=challenge, status_code=200)


@router.post("/whatsapp")
async def whatsapp_webhook_receive(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Event delivery (§19 POST). The raw body is read for exact HMAC
    verification BEFORE parsing — parsed-then-signed is a classic bypass."""
    settings: Settings = request.app.state.settings
    service = _service(request)

    raw_body = await request.body()
    if len(raw_body) > settings.QBIT_WEBHOOK_MAX_BODY_BYTES:
        from app.core.errors import QBITError

        class _PayloadTooLarge(QBITError):
            status_code = 413
            code = "PAYLOAD_TOO_LARGE"
            message = "Webhook payload exceeds the accepted size"

        raise _PayloadTooLarge()
    if not service.resolve_app_secret():
        # honest configuration failure — never process unverified events (§18)
        logger.error("Webhook rejected: no app secret configured for signature validation")
        raise _webhook_unauthorized("Webhook signature validation is not configured")
    signature = request.headers.get("x-hub-signature-256")
    if not service.verify_signature(raw_body=raw_body, signature_header=signature):
        logger.warning("Webhook rejected: missing or invalid signature")
        raise _webhook_unauthorized("Invalid webhook signature")

    import json

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Webhook payload is not valid JSON") from exc

    summary = await service.process_payload(session, payload)
    return {"success": True, "data": summary}
