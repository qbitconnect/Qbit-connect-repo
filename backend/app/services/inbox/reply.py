"""Outbound inbox replies (Phase 8 §21–§26).

Flow (§24):

    Reply → ReplyService → Message(SENDING) + inbox_outbox row
          → worker OutboxService → Channel Provider → Provider Events
          → Conversation/Message status updates

Rules:
- replies leave through the SAME provider abstraction campaigns use — the
  HTTP handler only enqueues (§24)
- idempotency: (conversation_id, client_message_id) gates duplicate sends —
  double-clicked Send creates ONE message (§25)
- WhatsApp rules (§22) are enforced honestly: free-text replies only inside
  the provider's 24h customer-service window; outside it an approved
  template is required (TEMPLATE_REQUIRED) — restrictions are never bypassed
- suppression is re-checked at queue time AND at send time
- failed replies offer Retry (§26): the SAME message row is re-queued — no
  duplicate provider requests unless the first attempt is confirmed failed
"""

from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, QBITError, ValidationError
from app.core.logging import get_logger
from app.models.marketing import (
    AccountHealth,
    AccountStatus,
    CampaignTemplate,
    SendingAccount,
)
from app.models.messaging import (
    Conversation,
    InboxOutboxItem,
    Message,
    MessageDirection,
    MessageStatus,
    OutboxStatus,
)
from app.models.scrape import Lead
from app.services.inbox.engine import ConversationEngine
from app.services.inbox.normalizer import utcnow

logger = get_logger("qbit.inbox.reply")


class TemplateRequiredError(QBITError):
    """409 — WhatsApp free-text reply outside the customer-service window (§22)."""

    status_code = 409
    code = "TEMPLATE_REQUIRED"
    message = (
        "The WhatsApp customer-service window has closed for this contact. "
        "Send an approved template to re-open the conversation."
    )


class ReplyService:
    def __init__(self, engine: ConversationEngine | None = None) -> None:
        self.engine = engine or ConversationEngine()

    # ------------------------------------------------------------------ queue
    async def queue_reply(
        self, session: AsyncSession, conversation: Conversation, *,
        user_id: uuid.UUID | None, body: str | None,
        subject: str | None = None,
        client_message_id: str | None = None,
        template_id: uuid.UUID | None = None,
        settings: Settings | None = None,
        provider_registry=None,
        commit: bool = True,
    ) -> tuple[Message, bool]:
        """Validate + enqueue one reply. Returns (message, created).

        created=False means the SAME client_message_id was already accepted —
        the existing message is returned untouched (§25)."""
        settings = settings or _require_settings()
        text = (body or "").strip()
        template = None

        # ---- channel + account checks (§22, §59) ----------------------------
        account = (
            await session.get(SendingAccount, conversation.sending_account_id)
            if conversation.sending_account_id else None
        )
        if account is None:
            raise ValidationError(
                "This conversation has no sending account — replies cannot be sent"
            )
        if (account.health_status or "") == AccountHealth.UNHEALTHY.value:
            raise ConflictError("SENDING_ACCOUNT_UNHEALTHY")
        if account.status not in (AccountStatus.ACTIVE.value, AccountStatus.PENDING.value):
            raise ConflictError(
                f"Sending account is {account.status} — replies are unavailable"
            )
        if provider_registry is None:
            from app.services.marketing.providers import build_provider_registry

            provider_registry = build_provider_registry(settings)
        try:
            provider = provider_registry.get(account.provider)
        except KeyError as exc:
            raise ConflictError(
                f"Provider '{account.provider}' is not available for this account"
            ) from exc

        # ---- idempotency (§25) -----------------------------------------------
        if not client_message_id:
            raise ValidationError("client_message_id is required for reply idempotency")
        client_message_id = client_message_id.strip()[:300]
        existing = (await session.execute(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.external_message_id == client_message_id,
            ).limit(1)
        )).scalars().first()
        if existing is not None:
            return existing, False

        # ---- recipient + content checks --------------------------------------
        if conversation.channel == "WHATSAPP":
            recipient_address = conversation.contact_phone or ""
        else:
            recipient_address = conversation.contact_email or ""
        if not recipient_address:
            raise ValidationError(
                "The contact has no address for this channel — reply is impossible"
            )

        # ---- suppression gate (§22 eligibility) --------------------------------
        from app.services.marketing.suppression import SuppressionService

        lead = await session.get(Lead, conversation.lead_id) if conversation.lead_id else None
        suppressed, reason = await SuppressionService().is_suppressed(
            session, channel=conversation.channel,
            email=conversation.contact_email or (lead.email if lead else None),
            phone=conversation.contact_phone or (lead.phone if lead else None),
            lead_id=conversation.lead_id,
        )
        if suppressed:
            raise ConflictError(f"RECIPIENT_SUPPRESSED:{reason or 'SUPPRESSED'}")

        # ---- channel-specific rules (§21–§23) ---------------------------------
        template_payload = None
        sender = account.display_identifier or account.identifier
        if conversation.channel == "WHATSAPP":
            if template_id is not None:
                template = await _load_template(
                    session, template_id, account_id=account.id,
                )
                # provider-approved templates only (§22; Phase 6 gate reused)
                problems = await provider.validate_send_requirements(
                    template=template, account_config=account.config_metadata or {},
                )
                if problems:
                    raise ValidationError("; ".join(problems))
                if lead is None:
                    raise ValidationError(
                        "Template variables require a linked lead — link a lead first"
                    )
                template_payload, missing = _build_template_payload(provider, template, lead)
                if template_payload is None:
                    raise ValidationError(
                        "Missing template variables: " + ", ".join(missing)
                    )
                message_type = "TEMPLATE"
            else:
                window_hours = settings.QBIT_INBOX_WHATSAPP_WINDOW_HOURS
                if not self.engine.within_whatsapp_window(
                    conversation, hours=window_hours,
                ):
                    raise TemplateRequiredError()
                message_type = "TEXT"
            if not text:
                raise ValidationError("Reply body is required")
        else:  # EMAIL (§23)
            if not text and not subject:
                raise ValidationError("Reply body or subject is required")
            message_type = "EMAIL"
            if subject is None or not subject.strip():
                base = conversation.subject or ""
                subject = ("Re: " + base) if base else "(no subject)"
            subject = subject.strip()[:300]

        message = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND.value,
            external_message_id=client_message_id,
            message_type=message_type,
            body=text[:20000] or None,
            subject=subject if conversation.channel == "EMAIL" else None,
            sender=(sender or "")[:320] or None,
            recipient=recipient_address[:320],
            lead_id=conversation.lead_id,
            status=MessageStatus.SENDING.value,
            message_metadata=await self._reply_metadata(
                session, conversation, account, template, user_id,
            ),
        )
        session.add(message)
        await session.flush()

        outbox = InboxOutboxItem(
            conversation_id=conversation.id,
            message_id=message.id,
            channel=conversation.channel,
            sending_account_id=account.id,
            status=OutboxStatus.WAITING.value,
            idempotency_key=f"inbox:{conversation.id}:{client_message_id}",
        )
        session.add(outbox)
        conversation.updated_at = utcnow()
        if commit:
            await session.commit()
            await session.refresh(message)
        logger.info(
            "reply_queued",
            extra={"extra_fields": {
                "conversation_id": str(conversation.id),
                "message_id": str(message.id),
                "channel": conversation.channel,
                "message_type": message_type,
            }},
        )
        return message, True

    # ------------------------------------------------------------------ retry
    async def retry_failed(
        self, session: AsyncSession, *, conversation: Conversation,
        message: Message, settings: Settings | None = None,
    ) -> Message:
        """Retry one FAILED reply (§26) — same message row, never a duplicate."""
        if message.conversation_id != conversation.id:
            raise NotFoundError("Message not found in this conversation")
        if message.direction != MessageDirection.OUTBOUND.value:
            raise ValidationError("Only outbound replies can be retried")
        if message.status != MessageStatus.FAILED.value:
            raise ConflictError(
                "Only confirmed-failed messages can be retried "
                "(§26 — no duplicate provider requests)"
            )
        settings = settings or _require_settings()
        client_message_id = message.external_message_id or str(message.id)
        outbox = (await session.execute(
            select(InboxOutboxItem).where(
                InboxOutboxItem.conversation_id == conversation.id,
                InboxOutboxItem.idempotency_key
                == f"inbox:{conversation.id}:{client_message_id}",
            ).order_by(InboxOutboxItem.created_at.desc()).limit(1)
        )).scalars().first()
        now = utcnow()
        message.status = MessageStatus.SENDING.value
        message.failed_at = None
        conversation.updated_at = now
        if outbox is not None:
            outbox.status = OutboxStatus.WAITING.value
            outbox.available_at = now
            outbox.locked_at = None
            outbox.lease_owner = None
            outbox.last_error = None
            outbox.error_code = None
            outbox.completed_at = None
            outbox.updated_at = now
        else:
            session.add(InboxOutboxItem(
                conversation_id=conversation.id, message_id=message.id,
                channel=conversation.channel,
                sending_account_id=conversation.sending_account_id,
                idempotency_key=f"inbox:{conversation.id}:{client_message_id}:r{now.timestamp():.0f}",
            ))
        await session.commit()
        return message

    # ---------------------------------------------------------------- helpers
    @staticmethod
    async def _reply_metadata(
        session: AsyncSession, conversation: Conversation, account, template, user_id,
    ) -> dict:
        """Threading + provenance metadata for the outbound message (§23, §5)."""
        metadata: dict = {"origin": "inbox", "account_id": str(account.id)}
        if user_id:
            metadata["sent_by"] = str(user_id)
        if template is not None:
            metadata["template_id"] = str(template.id)
            metadata["template_name"] = template.name
        if conversation.channel == "EMAIL":
            # In-Reply-To/References: the last known Message-ID in this thread
            rows = await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc())
                .limit(20)
            )
            last_with_id = None
            for candidate in rows.scalars():
                header = (candidate.message_metadata or {}).get("message_id") \
                    or candidate.provider_message_id
                if header:
                    last_with_id = (header, (candidate.message_metadata or {}).get("references"))
                    break
            if last_with_id:
                in_reply_to, references = last_with_id
                metadata["in_reply_to"] = in_reply_to
                merged = {ref for ref in ((references or "").split() + [in_reply_to]) if ref}
                metadata["references"] = " ".join(list(merged)[:10])[:1000]
        return metadata


def _require_settings() -> Settings:
    from app.core.config import get_settings

    return get_settings()


async def _load_template(
    session: AsyncSession, template_id: uuid.UUID, *, account_id: uuid.UUID,
) -> CampaignTemplate:
    template = await session.get(CampaignTemplate, template_id)
    if template is None:
        raise NotFoundError("Template not found")
    if template.account_id is not None and template.account_id != account_id:
        raise ValidationError("Template does not belong to this sending account")
    return template


def _build_template_payload(provider, template, lead) -> tuple[dict | None, list[str]]:
    """Adapter around WhatsAppProvider.build_template_payload that tolerates
    lead=None when the template has NO variables (honest rendering)."""
    placeholders = ((template.components or {}).get("placeholders") or {})
    needs_lead = bool(
        placeholders.get("body") or placeholders.get("header")
    ) or bool(template.variables or [])
    if needs_lead and lead is None:
        return None, list(template.variables or [])
    try:
        payload, missing = provider.build_template_payload(template, lead)
    except AttributeError:
        return None, ["template rendering failed"]
    return payload, missing
