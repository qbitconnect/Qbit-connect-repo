"""Email provider webhook endpoint (Phase 7 §27, §28).

    POST /api/v1/webhooks/email/{provider}

Contract (the mechanism the QBIT email adapters actually define):
    X-QBIT-Signature:  sha256=<hmac-sha256(raw body, shared secret)>
    X-QBIT-Timestamp:  <unix seconds, replay-window enforced>

These routes are intentionally NOT JWT-authenticated: they are called by the
provider. They are secured the way the contract requires — HMAC over the RAW
body (never parsed-then-signed), constant-time comparison, timestamp replay
protection, payload size cap. Invalid requests are rejected (401/400/413);
nothing secret-like is ever logged (§28, §57).

Processing is idempotent (§26): duplicate deliveries update nothing and
double-count nothing (ProviderEvent UNIQUE(provider, provider_event_id)).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import Settings
from app.core.errors import QBITError, ValidationError
from app.core.logging import get_logger
from app.services.marketing.webhooks_email import (
    PROVIDER_IDS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    EmailInboundWebhookService,
    EmailWebhookService,
)

logger = get_logger("qbit.marketing.webhooks_email_api")

router = APIRouter(prefix="/webhooks/email", tags=["webhooks-email"])


def _webhook_unauthorized(message_text: str) -> QBITError:
    class _WebhookUnauthorized(QBITError):
        status_code = 401
        code = "WEBHOOK_UNAUTHORIZED"
        message = "Webhook unauthorized"

    error = _WebhookUnauthorized()
    error.message = message_text
    return error


def _payload_too_large() -> QBITError:
    class _PayloadTooLarge(QBITError):
        status_code = 413
        code = "PAYLOAD_TOO_LARGE"
        message = "Webhook payload exceeds the accepted size"

    return _PayloadTooLarge()


@router.post("/{provider}")
async def email_webhook_receive(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Email event delivery (§27). The raw body is read for exact HMAC
    verification BEFORE parsing — parsed-then-signed is a classic bypass."""
    provider_id = (provider or "").strip().lower()
    if provider_id not in PROVIDER_IDS:
        raise ValidationError(f"Unknown email provider '{provider}'")
    settings: Settings = request.app.state.settings
    if provider_id == "email_mock" and settings.is_production:
        # Phase 12 (audit H6): the test provider's webhook is never exposed
        # in production, regardless of registry configuration.
        raise ValidationError("Unknown email provider 'email_mock'")
    service = EmailWebhookService(settings)

    raw_body = await request.body()
    if len(raw_body) > settings.QBIT_WEBHOOK_MAX_BODY_BYTES:
        raise _payload_too_large()
    secret = service.resolve_secret(provider_id)
    if not secret:
        # honest configuration failure — never process unverified events (§28)
        logger.error("Email webhook rejected: no shared secret configured")
        raise _webhook_unauthorized("Webhook signature validation is not configured")
    if not service.verify_signature(
        raw_body=raw_body,
        signature_header=request.headers.get(SIGNATURE_HEADER),
        timestamp_header=request.headers.get(TIMESTAMP_HEADER),
        secret=secret,
    ):
        logger.warning("Email webhook rejected: missing/invalid signature or stale timestamp")
        raise _webhook_unauthorized("Invalid webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Webhook payload is not valid JSON") from exc

    summary = await service.process_payload(session, payload, provider_id=provider_id)
    return {"success": True, "data": summary}


@router.post("/inbound/{provider}")
async def email_inbound_webhook_receive(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Inbound email ingestion (Phase 8 §38): provider/mailbox event →
    signature + replay validation → normalize → idempotent ProviderEvent →
    Message → Lead match → Conversation → unread → inbox.

    Same X-QBIT-Signature / X-QBIT-Timestamp contract as the delivery
    webhook; NOT JWT-authenticated (machine endpoint)."""
    provider_id = (provider or "").strip().lower()
    if provider_id not in PROVIDER_IDS:
        raise ValidationError(f"Unknown email provider '{provider}'")
    settings: Settings = request.app.state.settings
    if provider_id == "email_mock" and settings.is_production:
        raise ValidationError("Unknown email provider 'email_mock'")
    service = EmailInboundWebhookService(settings)

    raw_body = await request.body()
    if len(raw_body) > settings.QBIT_WEBHOOK_MAX_BODY_BYTES:
        raise _payload_too_large()
    secret = service.resolve_secret(provider_id)
    if not secret:
        logger.error("Email inbound webhook rejected: no shared secret configured")
        raise _webhook_unauthorized("Webhook signature validation is not configured")
    if not service.verify_signature(
        raw_body=raw_body,
        signature_header=request.headers.get(SIGNATURE_HEADER),
        timestamp_header=request.headers.get(TIMESTAMP_HEADER),
        secret=secret,
    ):
        logger.warning("Email inbound webhook rejected: missing/invalid signature or stale timestamp")
        raise _webhook_unauthorized("Invalid webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Webhook payload is not valid JSON") from exc

    summary = await service.process_inbound_payload(session, payload, provider_id=provider_id)
    return {"success": True, "data": summary}
