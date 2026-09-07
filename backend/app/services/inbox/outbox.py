"""Reply outbox worker (Phase 8 §24, §26).

Runs inside the existing worker process (own loop, isolated like the campaign
loop). Each cycle:

    claim WAITING outbox rows (lease) → resolve credentials (vault → env)
    → provider send through the campaign provider registry
    → message SENT + event, or classified failure with backoff (§26)

A provider failure NEVER crashes the loop. TRANSIENT errors retry with
exponential backoff until QBIT_INBOX_OUTBOX_MAX_ATTEMPTS, then fail honestly.
PERMANENT/CONFIGURATION errors fail immediately — retrying those can never
succeed (and retrying WhatsApp window violations would be restriction
bypass, which the platform refuses to do).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.marketing import SendingAccount
from app.models.messaging import (
    Conversation,
    InboxOutboxItem,
    Message,
    MessageStatus,
    OutboxStatus,
)
from app.services.inbox.engine import (
    EVENT_MESSAGE_FAILED,
    EVENT_MESSAGE_SENT,
    ConversationEngine,
)
from app.services.inbox.normalizer import utcnow
from app.services.marketing.providers.base import ErrorClass, ProviderError

logger = get_logger("qbit.inbox.outbox")


class OutboxService:
    def __init__(
        self, settings: Settings, provider_registry, *,
        owner: str = "inbox-outbox",
    ) -> None:
        self.settings = settings
        self.registry = provider_registry
        self.owner = owner
        self.engine = ConversationEngine()

    # ------------------------------------------------------------------ cycle
    async def process_cycle(self, session: AsyncSession) -> int:
        """Claim + deliver one batch. Returns the number of items processed."""
        items = await self._claim_batch(session)
        for item in items:
            try:
                await self._deliver(session, item)
            except Exception:  # noqa: BLE001 — one bad reply never stops the queue
                logger.exception(
                    "Outbox item crashed at worker level",
                    extra={"extra_fields": {"outbox_id": str(item.id)}},
                )
                item.status = OutboxStatus.FAILED.value
                item.last_error = "worker-level crash"
                item.locked_at = None
                await session.commit()
        return len(items)

    async def _claim_batch(self, session: AsyncSession) -> list[InboxOutboxItem]:
        now = utcnow()
        batch_size = self.settings.QBIT_INBOX_OUTBOX_BATCH_SIZE
        rows = (await session.execute(
            select(InboxOutboxItem)
            .where(
                InboxOutboxItem.status.in_([
                    OutboxStatus.WAITING.value, OutboxStatus.PROCESSING.value,
                ]),
                InboxOutboxItem.available_at <= now,
            )
            .order_by(InboxOutboxItem.available_at)
            .limit(batch_size)
        )).scalars().all()
        claimed: list[InboxOutboxItem] = []
        for item in rows:
            # optimistic lease — stale PROCESSING rows (crashed worker) are
            # reclaimable because available_at moved forward on the lease
            if item.status == OutboxStatus.PROCESSING.value and item.locked_at is not None:
                lease_age = (now - item.locked_at).total_seconds()
                if lease_age < max(self.settings.QBIT_WORKER_LEASE_SECONDS, 60):
                    continue
            item.status = OutboxStatus.PROCESSING.value
            item.locked_at = now
            item.lease_owner = self.owner
            item.attempts = (item.attempts or 0) + 1
            claimed.append(item)
        if claimed:
            await session.commit()
        return claimed

    # --------------------------------------------------------------- delivery
    async def _deliver(self, session: AsyncSession, item: InboxOutboxItem) -> None:
        message = await session.get(Message, item.message_id) if item.message_id else None
        conversation = await session.get(Conversation, item.conversation_id)
        if message is None or conversation is None:
            item.status = OutboxStatus.FAILED.value
            item.last_error = "message/conversation no longer exists"
            item.completed_at = utcnow()
            item.locked_at = None
            await session.commit()
            return
        # a user may have retried → message already confirmed SENT elsewhere
        if message.status not in (MessageStatus.SENDING.value,):
            item.status = OutboxStatus.COMPLETED.value
            item.completed_at = utcnow()
            item.locked_at = None
            await session.commit()
            return

        account = (
            await session.get(SendingAccount, item.sending_account_id)
            if item.sending_account_id else None
        )
        if account is None:
            await self._fail(session, item, message, conversation,
                             "Sending account no longer exists",
                             ErrorClass.CONFIGURATION, None)
            return

        if account.channel == "EMAIL":
            from app.services.marketing.connections_email import (
                email_account_config_for,
                resolve_email_credentials,
            )

            credentials = await resolve_email_credentials(session, account, self.settings)
            account_config = email_account_config_for(account, self.settings)
        else:
            from app.services.marketing.connections import resolve_account_credentials

            credentials = await resolve_account_credentials(session, account, self.settings)
            account_config = account.config_metadata or {}

        provider = self.registry.get(account.provider)
        template_payload = None
        subject = message.subject
        body = message.body or ""
        if message.message_type == "TEMPLATE":
            template_payload = (message.message_metadata or {}).get("template_payload")
            if template_payload is None:
                # rebuild from the stored template reference
                template_payload = await self._rebuild_template_payload(
                    session, conversation, message, account_config, credentials,
                )
            if template_payload is None:
                await self._fail(session, item, message, conversation,
                                 "Template payload could not be rebuilt",
                                 ErrorClass.CONFIGURATION, provider)
                return

        idempotency_key = item.idempotency_key
        try:
            if (
                conversation.channel == "WHATSAPP"
                and message.message_type == "TEXT"
                and hasattr(provider, "send_session_text")
            ):
                result = await provider.send_session_text(
                    account_config=account_config,
                    recipient_address=message.recipient or conversation.contact_phone or "",
                    body=body,
                    idempotency_key=idempotency_key,
                    credentials=credentials,
                )
            else:
                result = await provider.send(
                    account_config=account_config,
                    recipient_address=message.recipient or conversation.contact_email or "",
                    subject=subject, body=body,
                    idempotency_key=idempotency_key,
                    credentials=credentials,
                    template=template_payload,
                )
        except ProviderError as exc:
            await self._fail(session, item, message, conversation, str(exc),
                             exc.error_class, provider, error_code=exc.code)
            return

        now = utcnow()
        if result.ok:
            item.status = OutboxStatus.COMPLETED.value
            item.completed_at = now
            item.last_error = None
            item.error_code = None
            item.locked_at = None
            item.lease_owner = None
            message.status = MessageStatus.SENT.value
            message.sent_at = now
            if result.provider_message_id:
                message.provider_message_id = result.provider_message_id
            conversation.last_outbound_at = now
            conversation.last_message_at = now
            conversation.updated_at = now
            session.add(self._event(conversation.id, EVENT_MESSAGE_SENT, {
                "message_id": str(message.id),
                "provider_message_id": result.provider_message_id,
            }))
            await session.commit()
            logger.info(
                "reply_sent",
                extra={"extra_fields": {
                    "conversation_id": str(conversation.id),
                    "message_id": str(message.id),
                    "channel": conversation.channel,
                }},
            )
            return

        error_class = result.error_class
        await self._fail(
            session, item, message, conversation,
            result.error or "Provider send failed", error_class, provider,
            error_code=result.error_code,
            retry_after=(result.metadata or {}).get("retry_after_seconds"),
        )

    async def _rebuild_template_payload(
        self, session: AsyncSession, conversation: Conversation,
        message: Message, account_config: dict, credentials: dict | None,
    ) -> dict | None:
        """Rebuild the WhatsApp template payload at delivery time (variables
        are resolved from the CURRENT lead — the snapshot stays honest)."""
        template_id = (message.message_metadata or {}).get("template_id")
        if not template_id:
            return None
        from app.models.marketing import CampaignTemplate
        from app.models.scrape import Lead

        template = await session.get(CampaignTemplate, uuid.UUID(str(template_id)))
        if template is None:
            return None
        provider = self.registry.get("whatsapp_cloud")
        lead = await session.get(Lead, conversation.lead_id) if conversation.lead_id else None
        payload, _missing = provider.build_template_payload(template, lead) \
            if lead is not None else (None, ["lead required"])
        return payload

    # ------------------------------------------------------------------ fails
    async def _fail(
        self, session: AsyncSession, item: InboxOutboxItem, message: Message,
        conversation: Conversation, error: str, error_class: ErrorClass,
        provider, error_code: str | None = None, retry_after: float | None = None,
    ) -> None:
        now = utcnow()
        max_attempts = self.settings.QBIT_INBOX_OUTBOX_MAX_ATTEMPTS
        retryable = error_class is ErrorClass.TRANSIENT and item.attempts < max_attempts

        if retryable:
            # exponential backoff honoring provider retry-after when present (§19)
            delay = float(retry_after) if retry_after else min(2 ** max(item.attempts - 1, 0) * 30, 900)
            item.status = OutboxStatus.WAITING.value
            item.available_at = now + timedelta(seconds=delay)
            item.last_error = error[:500]
            item.error_code = error_code
            item.locked_at = None
            item.lease_owner = None
            message.status = MessageStatus.SENDING.value  # still in flight
            await session.commit()
            logger.info(
                "reply_retry_scheduled",
                extra={"extra_fields": {
                    "outbox_id": str(item.id), "attempt": item.attempts,
                    "delay_seconds": delay, "error_class": error_class.value,
                }},
            )
            return

        item.status = OutboxStatus.FAILED.value
        item.last_error = error[:500]
        item.error_code = error_code or error_class.value
        item.completed_at = now
        item.locked_at = None
        item.lease_owner = None
        message.status = MessageStatus.FAILED.value
        if message.failed_at is None:
            message.failed_at = now
        conversation.updated_at = now
        session.add(self._event(conversation.id, EVENT_MESSAGE_FAILED, {
            "message_id": str(message.id),
            "error": error[:300],
            "error_class": error_class.value,
            "code": error_code,
        }))
        await session.commit()
        logger.info(
            "reply_failed",
            extra={"extra_fields": {
                "outbox_id": str(item.id), "error_class": error_class.value,
                "attempts": item.attempts, "code": error_code,
            }},
        )

    @staticmethod
    def _event(conversation_id, event_type, new_value):
        from app.models.messaging import ConversationEvent

        return ConversationEvent(
            conversation_id=conversation_id, event_type=event_type,
            new_value=new_value,
        )
