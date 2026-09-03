"""Inbox / reply-tracking foundation (Phase 7 §32, §33).

Normalized backend interface for inbound messages:
- conversations are unique per (channel, sending_account, external contact)
- inbound email replies are matched to leads via the NORMALIZED address; a
  missing lead creates an unresolved conversation row — data is never
  fabricated and leads are never silently created (§32, Phase 6 rule)
- threading uses Message-ID / In-Reply-To / References headers (§33), never
  subject matching alone
- outbound sends are registered on the conversation when a reply arrives
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    Conversation,
    Message,
    Suppression,
)
from app.services.marketing.normalization import normalize_email, normalize_phone


class ConversationService:
    async def get_or_create(
        self,
        session: AsyncSession,
        *,
        channel: str,
        sending_account_id: uuid.UUID | None,
        external_address: str,
        external_contact_name: str | None = None,
        subject: str | None = None,
    ) -> Conversation:
        channel = channel.upper()
        if channel == "EMAIL":
            normalized = normalize_email(external_address).normalized or external_address.strip().lower()
        else:
            normalized = normalize_phone(external_address).normalized or external_address.strip()

        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.channel == channel,
                Conversation.sending_account_id == sending_account_id,
                Conversation.external_address_norm == normalized,
            )
        )
        if conversation is not None:
            if external_contact_name and not conversation.external_contact_name:
                conversation.external_contact_name = external_contact_name[:300]
            return conversation

        lead_id = await self._match_lead(session, channel, normalized)
        conversation = Conversation(
            channel=channel,
            sending_account_id=sending_account_id,
            lead_id=lead_id,
            external_address=external_address.strip(),
            external_address_norm=normalized,
            external_contact_name=external_contact_name[:300] if external_contact_name else None,
            subject=subject[:500] if subject else None,
        )
        session.add(conversation)
        await session.flush()
        return conversation

    async def _match_lead(
        self, session: AsyncSession, channel: str, normalized: str
    ) -> uuid.UUID | None:
        from app.models.scrape import Lead

        column = Lead.email_norm if channel == "EMAIL" else Lead.phone_norm
        lead_id = await session.scalar(select(Lead.id).where(column == normalized).limit(1))
        return lead_id

    # ------------------------------------------------------------ outbound log
    async def register_outbound(
        self,
        session: AsyncSession,
        *,
        campaign: Campaign,
        recipient: CampaignRecipient,
        account_id: uuid.UUID | None,
        subject: str | None,
        text: str | None,
        provider_message_id: str | None,
    ) -> None:
        """Record the outbound side of a conversation (called on first reply
        matching or eagerly for campaign sends — used for threading)."""
        conversation = await self.get_or_create(
            session,
            channel=campaign.channel,
            sending_account_id=account_id,
            external_address=recipient.address,
        )
        session.add(
            Message(
                conversation_id=conversation.id,
                direction="OUTBOUND",
                provider_message_id=provider_message_id,
                message_type="TEXT" if campaign.channel != "EMAIL" else "EMAIL",
                subject=subject,
                body_text=text,
                status="SENT",
                metadata_json={"campaign_id": str(campaign.id)},
            )
        )
        conversation.last_message_at = datetime.now(timezone.utc)

    # -------------------------------------------------------------- inbound
    async def ingest_reply(self, session: AsyncSession, *, campaign: Campaign, event) -> uuid.UUID | None:
        """Attach a REPLIED provider event to a conversation. Returns the
        conversation id when a conversation exists/was created."""
        text = (event.payload or {}).get("text")
        sender = (event.payload or {}).get("from")
        if not sender:
            return None
        conversation = await self.get_or_create(
            session,
            channel=campaign.channel,
            sending_account_id=campaign.sending_account_id,
            external_address=str(sender),
        )
        session.add(
            Message(
                conversation_id=conversation.id,
                direction="INBOUND",
                provider_message_id=event.provider_message_id,
                message_type="TEXT",
                body_text=text,
                status="RECEIVED",
                metadata_json={"campaign_id": str(campaign.id)},
            )
        )
        now = datetime.now(timezone.utc)
        conversation.last_message_at = now
        conversation.last_inbound_at = now
        conversation.unread_count += 1
        return conversation.id

    async def ingest_inbound_email(
        self,
        session: AsyncSession,
        *,
        sending_account_id: uuid.UUID | None,
        from_email: str,
        from_name: str | None,
        subject: str | None,
        text: str | None,
        html: str | None,
        headers: dict,
        provider_message_id: str | None = None,
    ) -> tuple[Conversation, Message]:
        """Normalized inbound email interface (§32). Full mailbox ingestion is
        a future phase; providers/webhooks call this single entrypoint."""
        conversation = await self.get_or_create(
            session,
            channel="EMAIL",
            sending_account_id=sending_account_id,
            external_address=from_email,
            external_contact_name=from_name,
            subject=subject,
        )
        message = Message(
            conversation_id=conversation.id,
            direction="INBOUND",
            provider_message_id=provider_message_id,
            message_type="EMAIL",
            subject=subject,
            body_text=text,
            body_html=html,
            headers={
                k: str(v)[:1000]
                for k, v in (headers or {}).items()
                if k.lower() in ("message-id", "in-reply-to", "references", "date")
            },
            status="RECEIVED",
        )
        session.add(message)
        now = datetime.now(timezone.utc)
        conversation.last_message_at = now
        conversation.last_inbound_at = now
        conversation.unread_count += 1
        await session.flush()
        return conversation, message

    async def list_conversations(
        self,
        session: AsyncSession,
        *,
        channel: str | None = None,
        limit: int = 50,
    ) -> list[Conversation]:
        query = select(Conversation).order_by(Conversation.last_message_at.desc().nullslast())
        if channel:
            query = query.where(Conversation.channel == channel.upper())
        query = query.limit(min(limit, 200))
        return list((await session.scalars(query)).all())
