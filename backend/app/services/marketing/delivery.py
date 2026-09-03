"""Email delivery pipeline used by the worker (Phase 7 §18–§23, §29–§31, §40).

Per recipient:
    claim (QUEUED→SENDING, optimistic) → render → personalize → unsubscribe
    link → optional tracking rewrite → provider.send() → record outcome

Safety rules:
- individual recipient delivery (no CC batching) — §40
- idempotency: the uq idempotency_key + optimistic claim make worker restarts
  safe; an UNKNOWN provider acceptance is FAILED (SEND_STATE_UNKNOWN) and is
  never blindly resent (§20)
- TRANSIENT failures retry with exponential backoff while attempts remain;
  PERMANENT failures never retry (§22)
- header injection attempts are rejected by providers; subject/values are
  CRLF-sanitized here too (§39)
- tracking is opt-in per campaign and never mandatory (§29–§31)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from email.utils import make_msgid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger, log_with
from app.models.marketing import (
    Campaign,
    CampaignRecipient,
    MarketingTemplate,
    RecipientStatus,
    SendingAccount,
)
from app.models.scrape import Lead
from app.services.marketing import templates as template_engine
from app.services.marketing import tracking as tracking_service
from app.services.marketing.campaigns import CampaignService
from app.services.marketing.normalization import normalize_email
from app.services.marketing.providers.base import OutboundMessage
from app.services.marketing.providers.errors import retry_class
from app.services.marketing.providers.registry import get_provider
from app.services.marketing.queue import MarketingQueue
from app.services.marketing.secrets import SecretVault
from app.services.marketing.suppression import UnsubscribeService

logger = get_logger("qbit.marketing.delivery")

SEND_STATE_UNKNOWN = "SEND_STATE_UNKNOWN"


class EmailDeliveryService:
    def __init__(self, settings: Settings, vault: SecretVault | None = None) -> None:
        self.settings = settings
        self.vault = vault or SecretVault(settings.QBIT_SECRET_KEY)
        self.campaigns = CampaignService()
        self.unsubscribe = UnsubscribeService(
            ttl_days=settings.QBIT_MARKETING_UNSUBSCRIBE_TOKEN_TTL_DAYS,
            base_url=settings.QBIT_PUBLIC_BASE_URL,
        )

    # ------------------------------------------------------------------ claim
    async def claim(self, session: AsyncSession, recipient_id: uuid.UUID) -> CampaignRecipient | None:
        """Optimistic QUEUED→SENDING claim; returns None when another worker
        won the race or the row is not QUEUED anymore."""
        now = datetime.now(timezone.utc)
        result = await session.execute(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.id == recipient_id,
                CampaignRecipient.status == RecipientStatus.QUEUED,
            )
            .values(status=RecipientStatus.SENDING, last_attempt_at=now)
            .returning(CampaignRecipient.id)
        )
        claimed = result.scalar_one_or_none()
        await session.commit()
        if claimed is None:
            return None
        return await session.get(CampaignRecipient, recipient_id)

    # ---------------------------------------------------------------- process
    async def process(
        self,
        session: AsyncSession,
        recipient_id: uuid.UUID,
        *,
        queue: MarketingQueue,
    ) -> str:
        """Send one queued recipient. Returns the resulting status string."""
        recipient = await session.get(CampaignRecipient, recipient_id)
        if recipient is None:
            return "MISSING"
        if recipient.status != RecipientStatus.QUEUED:
            return recipient.status  # already claimed/processed

        recipient = await self.claim(session, recipient_id)
        if recipient is None:
            return "RACE_LOST"

        campaign = await session.get(Campaign, recipient.campaign_id)
        if campaign is None:
            return "MISSING_CAMPAIGN"

        # Respect campaign control plane (pause/cancel).
        if campaign.status in ("PAUSED", "CANCELLED"):
            if campaign.status == "CANCELLED":
                recipient.status = RecipientStatus.SKIPPED
                recipient.reason = "CAMPAIGN_CANCELLED"
                await session.commit()
                return RecipientStatus.SKIPPED
            recipient.status = RecipientStatus.QUEUED  # wait for resume
            await session.commit()
            await queue.enqueue(str(recipient.id), delay_seconds=5)
            return "DEFERRED_PAUSED"

        template = await session.get(MarketingTemplate, campaign.template_id) if campaign.template_id else None
        account = await session.get(SendingAccount, campaign.sending_account_id) if campaign.sending_account_id else None
        if template is None or account is None:
            return await self._fail(
                session, campaign, recipient,
                error_code="CONFIGURATION_ERROR",
                error_message="Campaign template/account missing",
                retryable=False, queue=queue,
            )

        lead = await session.get(Lead, recipient.lead_id) if recipient.lead_id else None
        values = self._personalization(recipient, lead)

        # --- unsubscribe link (REAL token; spec §12–§14) --------------------
        raw_token = await self.unsubscribe.issue_token(
            session,
            channel="EMAIL",
            address=recipient.address,
            address_norm=recipient.address_norm,
            lead_id=recipient.lead_id,
            campaign_id=campaign.id,
            recipient_id=recipient.id,
        )
        unsubscribe_url = self.unsubscribe.build_url(raw_token)
        values["unsubscribe_url"] = unsubscribe_url

        renderer = template_engine.TemplateRenderer(
            unsubscribe_url=unsubscribe_url,
            company_name=str((campaign.audience or {}).get("company_name", "")),
            company_address=str((campaign.audience or {}).get("company_address", "")),
        )
        rendered = renderer.render(
            channel="EMAIL",
            subject=template.subject,
            html_body=template.html_body,
            text_body=template.text_body,
            body=None,
            values=values,
        )

        # --- optional tracking rewrite (§29–§31) ----------------------------
        html = rendered.html
        if campaign.track_clicks and html:
            html = tracking_service.rewrite_links(html, campaign.id, recipient.id, secret=self.settings.QBIT_SECRET_KEY)
        if campaign.track_opens and html:
            html = tracking_service.append_open_pixel(html, campaign.id, recipient.id, secret=self.settings.QBIT_SECRET_KEY)

        # --- headers: Message-ID for reply threading (§33) -------------------
        domain = (account.sender_email or "localhost").split("@")[-1] or "localhost"
        message_id = make_msgid(domain=domain)

        credentials = (await self.vault.get(session, account.credential_ref)) if account.credential_ref else None
        if credentials is None:
            return await self._fail(
                session, campaign, recipient,
                error_code="CONFIGURATION_ERROR",
                error_message="Account credentials are missing or unreadable",
                retryable=False, queue=queue,
            )

        provider = get_provider("EMAIL", account.provider, is_production=self.settings.is_production)
        message = OutboundMessage(
            channel="EMAIL",
            recipient=recipient.address,
            sender_name=account.sender_name,
            sender=account.sender_email,
            reply_to=account.reply_to,
            subject=rendered.subject,
            html=html,
            text=rendered.text,
            headers={
                "Message-ID": message_id,
                "X-QBIT-Campaign": str(campaign.id),
                "X-QBIT-Idempotency": recipient.idempotency_key,
            },
            metadata={
                "campaign_id": str(campaign.id),
                "recipient_id": str(recipient.id),
                "idempotency_key": recipient.idempotency_key,
            },
        )

        recipient.attempts += 1
        try:
            result = await provider.send(account.config or {}, credentials, message)
        except Exception as exc:  # noqa: BLE001 — unknown provider acceptance
            log_with(
                logger, 40, "email_send_exception",
                campaign_id=str(campaign.id), recipient_id=str(recipient.id),
                error=type(exc).__name__,
            )
            return await self._fail(
                session, campaign, recipient,
                error_code=SEND_STATE_UNKNOWN,
                error_message=f"Provider acceptance unknown ({type(exc).__name__}); manual decision required",
                retryable=False, queue=queue,
            )

        if result.success:
            await self.campaigns.record_event(
                session, campaign, recipient, "SENT",
                provider=account.provider,
                provider_message_id=result.provider_message_id,
                payload=result.raw_metadata,
            )
            recipient.provider_message_id = result.provider_message_id
            await session.commit()
            log_with(
                logger, 20, "email_sent",
                campaign_id=str(campaign.id), recipient_id=str(recipient.id),
                sending_account_id=str(account.id), provider=account.provider,
                provider_message_id=result.provider_message_id,
            )
            return RecipientStatus.SENT

        # provider returned a structured failure
        classification = retry_class(result.error_code)
        retryable = (
            result.retryable
            and classification == "TRANSIENT"
            and recipient.attempts < recipient.max_attempts
        )
        if retryable:
            from app.services.marketing.providers.errors import backoff_seconds

            delay = backoff_seconds(
                recipient.attempts,
                base_seconds=self.settings.QBIT_MARKETING_RETRY_BASE_SECONDS,
                max_seconds=self.settings.QBIT_MARKETING_RETRY_MAX_SECONDS,
            )
            recipient.status = RecipientStatus.QUEUED
            recipient.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            recipient.last_error_code = result.error_code
            recipient.last_error = (result.error_message or "")[:500]
            await session.commit()
            await queue.enqueue(str(recipient.id), delay_seconds=delay)
            log_with(
                logger, 20, "email_send_retry_scheduled",
                campaign_id=str(campaign.id), recipient_id=str(recipient.id),
                attempt=recipient.attempts, delay_seconds=delay,
                error_code=result.error_code,
            )
            return "RETRY_SCHEDULED"

        return await self._fail(
            session, campaign, recipient,
            error_code=result.error_code or "UNKNOWN",
            error_message=result.error_message or "Provider rejected the message",
            retryable=False, queue=queue,
        )

    # ----------------------------------------------------------------- helpers
    async def _fail(
        self,
        session: AsyncSession,
        campaign: Campaign,
        recipient: CampaignRecipient,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        queue: MarketingQueue,
    ) -> str:
        recipient.status = RecipientStatus.FAILED
        recipient.last_error_code = error_code
        recipient.last_error = (error_message or "")[:500]
        await self.campaigns.record_event(
            session, campaign, recipient, "FAILED",
            payload={"error_code": error_code, "retryable": retryable},
        )
        await session.commit()
        log_with(
            logger, 40, "email_failed",
            campaign_id=str(campaign.id), recipient_id=str(recipient.id),
            error_code=error_code,
        )
        return RecipientStatus.FAILED

    @staticmethod
    def _personalization(recipient: CampaignRecipient, lead: Lead | None) -> dict:
        if lead is None:
            base = {k: "" for k in template_engine.LEAD_VARIABLES}
            base.update(recipient.variables or {})
            base["email"] = recipient.address
            return base
        return {
            "first_name": lead.first_name or "",
            "last_name": lead.last_name or "",
            "business_name": lead.business_name or "",
            "email": lead.email or recipient.address,
            "phone": lead.phone or "",
            "city": lead.city or "",
            "state": lead.state or "",
            "country": lead.country or "",
            "website": lead.website or "",
        }
