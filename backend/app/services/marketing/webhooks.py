"""Provider webhook processing: verification + idempotency + application
(Phase 7 §24–§28).

Security (§28):
- generic email providers: HMAC-SHA256 over the raw body in
  `X-QBIT-Signature: sha256=<hex>` + `X-QBIT-Timestamp` replay window
- WhatsApp Cloud API: official `X-Hub-Signature-256` (HMAC with app secret)
- invalid signature/timestamp → 401 by the API layer; payload never applied
- authorization headers and secrets are NEVER logged (§28, §57)

Idempotency (§26, §27):
- (channel, provider, provider_event_id) UNIQUE — duplicates become
  ProviderEvent(status=DUPLICATE) and are never applied twice.

Effects (§24, §25, §26):
- DELIVERED/FAILED → recipient status + counters
- BOUNCED  → status; HARD bounces additionally suppress the address
- COMPLAINED → status + suppression (COMPLAINT) — never contacted again
- OPENED/CLICKED → optional tracking events (also reachable via /t/*)
- REPLIED → conversation ingestion (reply foundation, §32)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.core.logging import get_logger, log_with
from app.models.marketing import (
    Campaign,
    CampaignRecipient,
    ProviderEvent,
    ProviderTemplateStatus,
    RecipientStatus,
)
from app.services.marketing.campaigns import CampaignService
from app.services.marketing.providers.registry import get_provider
from app.services.marketing.suppression import SuppressionService

logger = get_logger("qbit.marketing.webhooks")


class WebhookVerificationError(ValidationError):
    code = "WEBHOOK_UNAUTHORIZED"
    status_code = 401
    message = "Webhook verification failed"


def verify_generic_signature(
    *, raw_body: bytes, signature: str | None, secret: str
) -> None:
    """HMAC-SHA256 shared-secret verification (generic email providers)."""
    if not signature or not secret:
        raise WebhookVerificationError("Missing webhook signature")
    provided = signature.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided.lower()):
        raise WebhookVerificationError("Invalid webhook signature")


def verify_timestamp(*, timestamp: str | None, tolerance_seconds: int) -> None:
    """Replay protection: reject stale timestamps (spec §28)."""
    if not timestamp:
        raise WebhookVerificationError("Missing webhook timestamp")
    try:
        value = int(float(timestamp))
    except ValueError as exc:
        raise WebhookVerificationError("Invalid webhook timestamp") from exc
    delta = abs(time.time() - value)
    if delta > tolerance_seconds:
        raise WebhookVerificationError("Webhook timestamp outside tolerance")


class MarketingWebhookService:
    def __init__(self, *, timestamp_tolerance: int) -> None:
        self.timestamp_tolerance = timestamp_tolerance
        self.campaigns = CampaignService()
        self.suppression = SuppressionService()

    # ------------------------------------------------------------- email path
    async def process_email_webhook(
        self,
        session: AsyncSession,
        *,
        provider: str,
        raw_body: bytes,
        headers: dict[str, str],
        secret: str | None,
    ) -> dict:
        verify_generic_signature(
            raw_body=raw_body, signature=headers.get("x-qbit-signature"), secret=secret or ""
        )
        verify_timestamp(
            timestamp=headers.get("x-qbit-timestamp"),
            tolerance_seconds=self.timestamp_tolerance,
        )
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Malformed webhook payload") from exc

        provider_adapter = get_provider("EMAIL", provider)
        events = await provider_adapter.handle_event(payload, headers, {})
        return await self._apply(
            session, channel="EMAIL", provider=provider, events=events
        )

    # ---------------------------------------------------------- whatsapp path
    async def process_whatsapp_webhook(
        self,
        session: AsyncSession,
        *,
        raw_body: bytes,
        headers: dict[str, str],
        app_secret: str | None,
    ) -> dict:
        provider_adapter = get_provider("WHATSAPP", "whatsapp_cloud")
        signature = headers.get("x-hub-signature-256")
        if app_secret:
            if not provider_adapter.verify_webhook_signature(
                raw_body=raw_body, signature_header=signature or "", app_secret=app_secret
            ):
                raise WebhookVerificationError("Invalid webhook signature")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Malformed webhook payload") from exc
        events = await provider_adapter.handle_event(payload, headers, {})
        return await self._apply(
            session, channel="WHATSAPP", provider="whatsapp_cloud", events=events
        )

    # ------------------------------------------------------------------ apply
    async def _apply(
        self,
        session: AsyncSession,
        *,
        channel: str,
        provider: str,
        events,
    ) -> dict:
        applied = duplicates = skipped = 0
        for event in events:
            # --- idempotency gate (provider_event_id unique per provider) ---
            existing = await session.scalar(
                select(ProviderEvent).where(
                    ProviderEvent.channel == channel,
                    ProviderEvent.provider == provider,
                    ProviderEvent.provider_event_id == event.provider_event_id,
                )
            )
            if existing is not None:
                duplicates += 1
                continue
            session.add(
                ProviderEvent(
                    channel=channel,
                    provider=provider,
                    provider_event_id=event.provider_event_id,
                    event_type=event.event_type,
                    status="PROCESSED",
                    payload={"reason": event.reason} if event.reason else {},
                    processed_at=datetime.now(timezone.utc),
                )
            )

            handled = await self._apply_event(
                session, channel=channel, provider=provider, event=event
            )
            if handled:
                applied += 1
            else:
                skipped += 1
        await session.commit()
        return {"received": len(events), "applied": applied, "duplicates": duplicates, "skipped": skipped}

    async def _apply_event(self, session: AsyncSession, *, channel: str, provider: str, event) -> bool:
        # --- locate the recipient ------------------------------------------------
        recipient: CampaignRecipient | None = None
        if event.provider_message_id:
            recipient = await session.scalar(
                select(CampaignRecipient).where(
                    CampaignRecipient.provider_message_id == event.provider_message_id
                )
            )
        if recipient is None and event.recipient:
            # fallback: most recent campaign send to this address
            recipient = (
                await session.scalars(
                    select(CampaignRecipient)
                    .where(
                        CampaignRecipient.address_norm == event.recipient.lower(),
                        CampaignRecipient.campaign_id.isnot(None),
                    )
                    .order_by(CampaignRecipient.created_at.desc())
                    .limit(1)
                )
            ).first()
        if recipient is None:
            log_with(
                logger, 20, "webhook_event_unmatched",
                channel=channel, event_type=event.event_type,
                provider_event_id=event.provider_event_id,
            )
            return False

        campaign = await session.get(Campaign, recipient.campaign_id)
        if campaign is None:
            return False

        if channel == "EMAIL":
            if event.event_type == "OPENED":
                await self._record_tracking(
                    session, campaign, recipient, "OPEN", None, event
                )
                return True
            if event.event_type == "CLICKED":
                await self._record_tracking(
                    session, campaign, recipient, "CLICK", (event.payload or {}).get("url"), event
                )
                return True

        if event.event_type in ("SENT", "DELIVERED", "FAILED", "READ"):
            applied = await self.campaigns.record_event(
                session, campaign, recipient, event.event_type,
                provider=provider,
                provider_message_id=event.provider_message_id,
                provider_event_id=event.provider_event_id,
                payload={"reason": event.reason} if event.reason else {},
            )
            return applied

        if event.event_type == "BOUNCED":
            applied = await self.campaigns.record_event(
                session, campaign, recipient, "BOUNCED",
                provider=provider,
                provider_message_id=event.provider_message_id,
                provider_event_id=event.provider_event_id,
                payload={"hard": event.hard_bounce, "reason": event.reason},
            )
            if applied and event.hard_bounce:
                # §24: hard bounce → suppression, prevent future sends
                await self.suppression.add(
                    session,
                    channel=channel,
                    address=recipient.address_norm,
                    reason="HARD_BOUNCE",
                    source="webhook",
                    lead_id=recipient.lead_id,
                    metadata={"campaign_id": str(campaign.id)},
                )
            return applied

        if event.event_type == "COMPLAINED":
            applied = await self.campaigns.record_event(
                session, campaign, recipient, "COMPLAINED",
                provider=provider,
                provider_message_id=event.provider_message_id,
                provider_event_id=event.provider_event_id,
                payload={"reason": event.reason},
            )
            if applied:
                # §25: complaint → suppress from future marketing
                await self.suppression.add(
                    session,
                    channel=channel,
                    address=recipient.address_norm,
                    reason="COMPLAINT",
                    source="webhook",
                    lead_id=recipient.lead_id,
                    metadata={"campaign_id": str(campaign.id)},
                )
            return applied

        if event.event_type == "UNSUBSCRIBED":
            await self.suppression.add(
                session,
                channel=channel,
                address=recipient.address_norm,
                reason="UNSUBSCRIBED",
                source="webhook",
                lead_id=recipient.lead_id,
                metadata={"campaign_id": str(campaign.id)},
            )
            campaign.unsubscribed_count += 1
            recipient.unsubscribed_at = datetime.now(timezone.utc)
            return True

        if event.event_type == "REPLIED":
            campaign.replied_count += 1
            from app.services.marketing.conversations import ConversationService

            await ConversationService().ingest_reply(session, campaign=campaign, event=event)
            return True

        return False

    async def _record_tracking(
        self, session, campaign, recipient, event_type: str, url: str | None, event
    ) -> None:
        from app.models.marketing import EmailTrackingEvent

        session.add(
            EmailTrackingEvent(
                campaign_id=campaign.id,
                recipient_id=recipient.id,
                event_type=event_type,
                url=url,
            )
        )
        if event_type == "OPEN":
            if recipient.opened_at is None:
                recipient.opened_at = datetime.now(timezone.utc)
            await self.campaigns.record_event(
                session, campaign, recipient, "OPENED",
                provider_message_id=event.provider_message_id,
                provider_event_id=event.provider_event_id,
            )
        else:
            if recipient.clicked_at is None:
                recipient.clicked_at = datetime.now(timezone.utc)
                campaign.clicked_count += 1
            await self.campaigns.record_event(
                session, campaign, recipient, "CLICKED",
                provider_message_id=event.provider_message_id,
                provider_event_id=event.provider_event_id,
                payload={"url": url} if url else {},
            )


async def verify_whatsapp_challenge(params: dict, *, expected_token: str | None) -> str | None:
    """Official GET verification (Cloud API). Returns the challenge to echo
    when the verify token matches; None otherwise."""
    from app.services.marketing.providers.whatsapp import WhatsAppProvider

    challenge = WhatsAppProvider.extract_challenge(params)
    if challenge is None:
        return None
    if not expected_token or not hmac.compare_digest(
        str(params.get("hub.verify_token", "")), expected_token
    ):
        raise WebhookVerificationError("Webhook verify token mismatch")
    return challenge
