"""Campaign send worker (Phase 5 §17, §21, §23, §45).

Runs inside the existing worker process next to the scrape and data-job loops
(isolation rule §15). Each cycle:

  1. flip due SCHEDULED campaigns → QUEUED, process QUEUED → launch pipeline
  2. claim a bounded batch of WAITING queue items (rate-gated per account)
  3. render the template per recipient (safe substitution)
  4. re-check eligibility right before send (suppression can change mid-run)
  5. provider.send(idempotency_key) → events + recipient status transitions
  6. complete RUNNING campaigns whose queue drained

A provider failure NEVER crashes the loop; failures are classified and the
queue item retries with exponential backoff or fails honestly (§19).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import QBITError
from app.core.logging import get_logger
from app.models.marketing import (
    Campaign,
    CampaignQueueItem,
    CampaignRecipient,
    CampaignStatus,
    CampaignTemplate,
    EventType,
    QueueStatus,
    RecipientStatus,
    SendingAccount,
)
from app.services.marketing.campaign import CampaignService
from app.services.marketing.connections import resolve_account_credentials
from app.services.marketing.connections_email import resolve_email_credentials
from app.services.marketing.eligibility import EligibilityService
from app.services.marketing.email_send import prepare_email_message
from app.services.marketing.events import EventService
from app.services.marketing.providers import MarketingProviderRegistry
from app.services.marketing.providers.base import ErrorClass, ProviderError
from app.services.marketing.providers.whatsapp import WhatsAppProvider
from app.services.marketing.queue import QueueService
from app.services.marketing.template import render_from_lead
from app.models.scrape import Lead

logger = get_logger("qbit.marketing.worker")


async def _apply_event_to_recipient(
    session: AsyncSession, recipient: CampaignRecipient, normalized: dict,
) -> int:
    """Apply a normalized provider event to a recipient (§37).

    Event history is never overwritten: each distinct event appends a row and
    moves the recipient status forward only (never backwards).
    Returns 1 if applied, 0 if the event was unknown/unapplicable.
    """
    from app.services.marketing.campaign import CampaignService  # noqa: F401 (cycle-safe)

    event_type = normalized.get("event_type")
    now = datetime.now(timezone.utc)
    mapping = {
        EventType.MESSAGE_DELIVERED: (RecipientStatus.DELIVERED, "delivered_at"),
        EventType.MESSAGE_READ: (RecipientStatus.READ, "read_at"),
        EventType.MESSAGE_REPLIED: (RecipientStatus.REPLIED, "replied_at"),
        EventType.MESSAGE_FAILED: (RecipientStatus.FAILED, "failed_at"),
    }
    if event_type not in mapping:
        return 0
    status, timestamp_field = mapping[event_type]
    if getattr(recipient, timestamp_field) is None:
        setattr(recipient, timestamp_field, now)
    # forward-only status movement
    order = list(RecipientStatus)
    current_idx = order.index(recipient.status) if recipient.status in order else -1
    if order.index(status) > current_idx:
        recipient.status = status
    await session.commit()
    events = EventService()
    await events.record(
        session, campaign_id=recipient.campaign_id, recipient_id=recipient.id,
        event_type=event_type, provider=normalized.get("provider"),
        provider_event_id=normalized.get("provider_message_id"),
        metadata=normalized.get("metadata") or {},
    )
    return 1


class CampaignWorker:
    def __init__(
        self, settings: Settings, provider_registry: MarketingProviderRegistry, *,
        owner: str = "campaign-worker",
    ) -> None:
        self.settings = settings
        self.registry = provider_registry
        self.owner = owner
        self.campaigns = CampaignService()
        self.queue = QueueService(settings)
        self.events = EventService()
        self.eligibility = EligibilityService()

    # ------------------------------------------------------------------ cycle
    async def process_cycle(self, session: AsyncSession) -> int:
        """One worker cycle: schedules → launches → sends. Returns actions."""
        actions = 0
        actions += await self._process_schedules(session)
        actions += await self._process_queued_launches(session)
        actions += await self._send_batch(session)
        actions += await self._complete_finished(session)
        return actions

    async def _process_schedules(self, session: AsyncSession) -> int:
        due = await self.campaigns.process_due_schedules(session)
        return len(due)

    async def _process_queued_launches(self, session: AsyncSession) -> int:
        queued = (await session.execute(
            select(Campaign).where(Campaign.status == CampaignStatus.QUEUED)
            .order_by(Campaign.created_at).limit(3)
        )).scalars().all()
        for campaign in queued:
            try:
                await self.campaigns.process_launch(
                    session, campaign, actor_id=campaign.created_by,
                    provider_registry=self.registry,
                    batch_size=self.settings.QBIT_MARKETING_SNAPSHOT_BATCH_SIZE,
                    max_audience=self.settings.QBIT_MARKETING_MAX_AUDIENCE,
                )
            except QBITError as exc:
                logger.error(
                    "Campaign launch failed — marking FAILED",
                    extra={"extra_fields": {"campaign_id": str(campaign.id),
                                            "error": exc.message}},
                )
                campaign.status = CampaignStatus.FAILED
                campaign.completed_at = datetime.now(timezone.utc)
                await session.commit()
                await self.events.record(
                    session, campaign_id=campaign.id,
                    event_type=EventType.CAMPAIGN_FAILED, metadata={"error": exc.message},
                )
        return len(queued)

    # ------------------------------------------------------------------ sends
    async def _send_batch(self, session: AsyncSession) -> int:
        items = await self.queue.claim_batch(
            session, owner=self.owner,
            batch_size=self.settings.QBIT_MARKETING_QUEUE_BATCH_SIZE,
        )
        for item in items:
            try:
                await self._send_item(session, item)
            except Exception:  # noqa: BLE001 — one bad message never stops the queue
                logger.exception(
                    "Queue item crashed at worker level",
                    extra={"extra_fields": {"queue_item_id": str(item.id)}},
                )
                item.status = QueueStatus.FAILED
                item.last_error = "worker-level crash"
                item.locked_at = None
                await session.commit()
        return len(items)

    async def _send_item(self, session: AsyncSession, item: CampaignQueueItem) -> None:
        campaign = await session.get(Campaign, item.campaign_id)
        if campaign is None:
            item.status = QueueStatus.CANCELLED
            item.completed_at = datetime.now(timezone.utc)
            await session.commit()
            return
        # pause (§23): stop adding new work; release the item back to WAITING
        if campaign.status in (CampaignStatus.PAUSED, CampaignStatus.CANCELLED,
                               CampaignStatus.FAILED):
            if campaign.status == CampaignStatus.PAUSED:
                item.status = QueueStatus.WAITING
                item.locked_at = None
                item.lease_owner = None
                item.attempts = max(0, item.attempts - 1)
                await session.commit()
            else:
                item.status = QueueStatus.CANCELLED
                item.completed_at = datetime.now(timezone.utc)
                await session.commit()
            return

        recipient = await session.get(CampaignRecipient, item.recipient_id)
        if recipient is None:
            item.status = QueueStatus.CANCELLED
            item.completed_at = datetime.now(timezone.utc)
            await session.commit()
            return
        account = (
            await session.get(SendingAccount, item.sending_account_id)
            if item.sending_account_id else None
        )
        # Phase 6 §29: an UNHEALTHY account must never receive provider sends
        if account is not None and (account.health_status or "") == "UNHEALTHY":
            await self._fail_item(
                session, item, recipient,
                "SENDING_ACCOUNT_UNHEALTHY", ErrorClass.CONFIGURATION,
                campaign, account,
                self.registry.get(account.provider) if account else None,
                error_code="SENDING_ACCOUNT_UNHEALTHY",
            )
            return
        provider = self.registry.get(account.provider) if account else None

        # rate gate (§21) — conservative, per account
        if account is not None:
            allowed, hint = await self.queue.rate_gate(
                session, account=account, settings=self.settings,
            )
            if not allowed:
                item.status = QueueStatus.WAITING
                item.locked_at = None
                item.lease_owner = None
                item.available_at = datetime.now(timezone.utc)
                item.attempts = max(0, item.attempts - 1)
                await session.commit()
                return

        # re-check suppression right before send (list can change mid-run)
        from app.services.marketing.suppression import SuppressionService
        lead = await session.get(Lead, recipient.lead_id) if recipient.lead_id else None
        if lead is not None:
            suppressed, reason = await SuppressionService().is_suppressed(
                session, channel=campaign.channel,
                email=lead.email, phone=lead.phone, lead_id=lead.id,
            )
            if suppressed:
                recipient.status = RecipientStatus.SKIPPED
                recipient.skip_reason = reason or "SUPPRESSED"
                item.status = QueueStatus.CANCELLED
                item.completed_at = datetime.now(timezone.utc)
                await session.commit()
                await self.events.record(
                    session, campaign_id=campaign.id, recipient_id=recipient.id,
                    event_type=EventType.RECIPIENT_SKIPPED,
                    metadata={"reason": recipient.skip_reason, "stage": "pre-send"},
                )
                return

        # render template (safe substitution only)
        template = (
            await session.get(CampaignTemplate, campaign.template_id)
            if campaign.template_id else None
        )
        if template is None:
            await self._fail_item(session, item, recipient,
                                  "Template missing", ErrorClass.CONFIGURATION,
                                  campaign, account, provider)
            return

        # Phase 7 §10/§12/§29: the EMAIL branch composes the final message
        # (subject + sanitized HTML + plain text + REAL unsubscribe link +
        # opt-in tracking). Providers receive an honest, ready-to-send payload.
        template_payload = None
        subject = None
        body = None
        if campaign.channel == "EMAIL":
            prepared = await prepare_email_message(
                session, campaign=campaign, recipient=recipient, lead=lead,
                template=template, account=account, settings=self.settings,
            )
            if not prepared.get("ok"):
                recipient.status = RecipientStatus.SKIPPED
                recipient.skip_reason = prepared.get("skip_reason") or "PREPARE_FAILED"
                item.status = QueueStatus.CANCELLED
                item.completed_at = datetime.now(timezone.utc)
                await session.commit()
                await self.events.record(
                    session, campaign_id=campaign.id, recipient_id=recipient.id,
                    event_type=EventType.RECIPIENT_SKIPPED,
                    metadata={"reason": recipient.skip_reason,
                              "missing": prepared.get("missing_variables")},
                )
                return
            subject = prepared["subject"]
            body = prepared["body"]
            template_payload = prepared["template"]
        else:
            subject = render_from_lead(template.subject, lead) if (template.subject and lead) else template.subject
            body = render_from_lead(template.body, lead) if lead else template.body

        # Phase 6 §14/§28: WhatsApp adapter sends provider-template payloads.
        # A recipient whose lead lacks a required variable value is skipped
        # honestly instead of sending an incomplete template.
        if (
            campaign.channel == "WHATSAPP"
            and isinstance(provider, WhatsAppProvider)
            and template.origin == "PROVIDER"
            and lead is not None
        ):
            template_payload, missing_vars = provider.build_template_payload(template, lead)
            if template_payload is None:
                recipient.status = RecipientStatus.SKIPPED
                recipient.skip_reason = "MISSING_TEMPLATE_VARIABLE"
                item.status = QueueStatus.CANCELLED
                item.completed_at = datetime.now(timezone.utc)
                await session.commit()
                await self.events.record(
                    session, campaign_id=campaign.id, recipient_id=recipient.id,
                    event_type=EventType.RECIPIENT_SKIPPED,
                    metadata={"reason": "MISSING_TEMPLATE_VARIABLE",
                              "variables": missing_vars},
                )
                return

        if provider is None or account is None:
            await self._fail_item(
                session, item, recipient,
                "Provider not configured", ErrorClass.CONFIGURATION,
                campaign, account, provider,
            )
            return

        recipient.status = RecipientStatus.SENDING
        await session.commit()

        idempotency_key = f"{item.campaign_id}:{item.recipient_id}:{item.message_version}"
        # Phase 6 §4 + Phase 7 §6: resolve credentials for THIS call only
        # (vault → env); the payload is consumed by the provider client,
        # never logged or stored
        if account.channel == "EMAIL":
            from app.services.marketing.connections_email import email_account_config_for

            credentials = await resolve_email_credentials(session, account, self.settings)
            account_config = email_account_config_for(account, self.settings)
        else:
            credentials = await resolve_account_credentials(session, account, self.settings)
            account_config = account.config_metadata or {}
        try:
            result = await provider.send(
                account_config=account_config,
                recipient_address=recipient.recipient_address,
                subject=subject, body=body,
                idempotency_key=idempotency_key,
                credentials=credentials,
                template=template_payload,
            )
        except ProviderError as exc:
            await self._fail_item(session, item, recipient, str(exc),
                                  exc.error_class, campaign, account, provider)
            return

        now = datetime.now(timezone.utc)
        if result.ok:
            item.status = QueueStatus.COMPLETED
            item.completed_at = now
            item.last_error = None
            item.locked_at = None
            item.lease_owner = None
            recipient.status = RecipientStatus.SENT
            recipient.sent_at = now
            if result.provider_message_id:
                recipient.provider_message_id = result.provider_message_id
            await session.commit()
            await self.events.record(
                session, campaign_id=campaign.id, recipient_id=recipient.id,
                event_type=EventType.MESSAGE_SENT, provider=provider.provider_id,
                provider_event_id=result.provider_message_id,
                metadata={
                    "mock": result.metadata.get("mock", False),
                    "provider_status": result.metadata.get("provider_status"),
                },
            )
            # Phase 8 §61: campaign sends appear in the conversation history —
            # a failure here must never break the campaign loop
            try:
                from app.services.inbox.engine import ConversationEngine

                await ConversationEngine().ingest_campaign_outbound(
                    session, campaign=campaign, recipient=recipient,
                    account=account,
                    provider_message_id=result.provider_message_id,
                    subject=subject, body=body,
                    message_type="EMAIL" if campaign.channel == "EMAIL" else "TEMPLATE",
                )
            except Exception:  # noqa: BLE001 — inbox linkage is best-effort
                logger.exception(
                    "Campaign→conversation linkage failed",
                    extra={"extra_fields": {
                        "campaign_id": str(campaign.id),
                        "recipient_id": str(recipient.id),
                    }},
                )
                await session.rollback()
            logger.info(
                "recipient_sent",
                extra={"extra_fields": {"campaign_id": str(campaign.id),
                                        "recipient_id": str(recipient.id),
                                        "provider": provider.provider_id,
                                        "sending_account_id": str(account.id)}},
            )
        else:
            await self._fail_item(
                session, item, recipient,
                result.error or "Provider send failed",
                ErrorClass(result.error_class.value) if isinstance(result.error_class, ErrorClass) else result.error_class,
                campaign, account, provider, error_code=result.error_code,
                provider_retry_after=result.metadata.get("retry_after_seconds"),
            )

    async def _fail_item(
        self, session: AsyncSession, item: CampaignQueueItem,
        recipient: CampaignRecipient, error: str, error_class: ErrorClass,
        campaign: Campaign, account: SendingAccount | None,
        provider, error_code: str | None = None,
        provider_retry_after: float | None = None,
    ) -> None:
        status = await self.queue.fail(
            session, item, error=error,
            error_class=error_class.value, settings=self.settings,
            provider_retry_after=provider_retry_after,
        )
        if status == QueueStatus.FAILED:
            recipient.status = RecipientStatus.FAILED
            recipient.failed_at = datetime.now(timezone.utc)
            await session.commit()
        await self.events.record(
            session, campaign_id=campaign.id, recipient_id=recipient.id,
            event_type=EventType.MESSAGE_FAILED, provider=provider.provider_id if provider else None,
            metadata={
                "error": error[:300], "error_class": error_class.value,
                "queue_status": status, "code": error_code,
            },
        )
        logger.info(
            "recipient_failed",
            extra={"extra_fields": {"campaign_id": str(campaign.id),
                                    "recipient_id": str(recipient.id),
                                    "error_class": error_class.value,
                                    "queue_status": status}},
        )

    async def _complete_finished(self, session: AsyncSession) -> int:
        """RUNNING campaigns whose queue fully drained → COMPLETED (§24)."""
        running = (await session.execute(
            select(Campaign).where(Campaign.status == CampaignStatus.RUNNING)
            .order_by(Campaign.created_at).limit(10)
        )).scalars().all()
        completed = 0
        for campaign in running:
            pending = await session.scalar(
                select(func.count()).select_from(CampaignQueueItem).where(
                    CampaignQueueItem.campaign_id == campaign.id,
                    CampaignQueueItem.status.in_([
                        QueueStatus.WAITING, QueueStatus.RETRY, QueueStatus.PROCESSING,
                    ]),
                )
            ) or 0
            if pending:
                continue
            queued_count = await session.scalar(
                select(func.count()).select_from(CampaignQueueItem).where(
                    CampaignQueueItem.campaign_id == campaign.id,
                )
            ) or 0
            if queued_count == 0:
                continue  # never launched any work — leave for launch pipeline
            campaign.status = CampaignStatus.COMPLETED
            campaign.completed_at = datetime.now(timezone.utc)
            await session.commit()
            await self.events.record(
                session, campaign_id=campaign.id,
                event_type=EventType.CAMPAIGN_COMPLETED,
            )
            logger.info(
                "campaign_completed",
                extra={"extra_fields": {"campaign_id": str(campaign.id)}},
            )
            completed += 1
        return completed
