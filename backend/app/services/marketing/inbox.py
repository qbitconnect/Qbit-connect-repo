"""Inbox foundation (Phase 6 §22–§24).

Phase 6 establishes the BACKEND event model for inbound WhatsApp messages:

    Webhook → Normalized Message → Lead Match → Conversation → (Inbox UI later)

An inbound message is matched by sending_account + normalized phone. When a
real Lead matches, it is attached to the conversation; when none matches, the
conversation stays lead-less (PENDING) as an unresolved contact — personal
information is never invented and duplicate leads are never silently created
(§24). The Inbox UI itself is a later phase; this module only persists
normalized, queryable data.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.marketing import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    EventType,
    RecipientStatus,
    SendingAccount,
)
from app.models.messaging import (
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageStatus,
)
from app.models.scrape import Lead
from app.services.marketing.state import apply_event, can_transition
from app.services.scraping.lead_keys import normalize_phone as normalize_lead_phone

logger = get_logger("qbit.marketing.inbox")


class InboxService:
    # --------------------------------------------------------- conversations
    async def get_or_create_conversation(
        self, session: AsyncSession, *,
        account: SendingAccount | None,
        contact_phone: str | None,
        external_contact_id: str | None = None,
        channel: str = "WHATSAPP",
    ) -> Conversation:
        """Match by (sending_account, normalized phone) — never duplicates."""
        key = (contact_phone or "").strip() or (external_contact_id or "").strip()
        query = select(Conversation).where(
            Conversation.channel == channel,
            Conversation.sending_account_id == (account.id if account else None),
            Conversation.contact_phone == (contact_phone or None),
        )
        existing = (await session.execute(
            query.order_by(Conversation.created_at).limit(1)
        )).scalars().first()
        if existing is not None:
            return existing
        conversation = Conversation(
            channel=channel,
            sending_account_id=account.id if account else None,
            contact_phone=(contact_phone or None),
            external_contact_id=external_contact_id,
            status=ConversationStatus.PENDING,  # unresolved until a Lead matches
        )
        session.add(conversation)
        await session.flush()
        return conversation

    # ------------------------------------------------------------ lead match
    async def match_lead(
        self, session: AsyncSession, *, contact_phone: str | None,
    ) -> Lead | None:
        """Match a Lead by normalized phone — exact normalized comparison
        only (no fuzzy guessing, no silent lead creation, §24)."""
        if not contact_phone:
            return None
        normalized = normalize_lead_phone(contact_phone)
        if not normalized:
            return None
        digits = normalized.lstrip("+")
        variants = {digits, f"+{digits}"}
        rows = await session.execute(
            select(Lead).where(Lead.phone_norm.in_(list(variants))).limit(2)
        )
        leads = list(rows.scalars().all())
        return leads[0] if leads else None

    # ------------------------------------------------------- inbound messages
    async def record_inbound_message(
        self, session: AsyncSession, *,
        account: SendingAccount | None,
        contact_phone: str | None,
        external_contact_id: str | None,
        provider_message_id: str | None,
        message_type: str,
        body: str | None,
        occurred_at: datetime | None = None,
        metadata: dict | None = None,
    ) -> tuple[Conversation, Message]:
        """Persist one inbound message (§23) + attach a Lead when one matches."""
        conversation = await self.get_or_create_conversation(
            session, account=account, contact_phone=contact_phone,
            external_contact_id=external_contact_id,
        )

        # (re)attach a real lead whenever we can match one — never fabricate
        if conversation.lead_id is None:
            lead = await self.match_lead(session, contact_phone=contact_phone)
            if lead is not None:
                conversation.lead_id = lead.id
        if conversation.lead_id is not None and conversation.status == ConversationStatus.PENDING:
            conversation.status = ConversationStatus.OPEN

        message = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND.value,
            provider_message_id=provider_message_id,
            message_type=(message_type or "TEXT").upper()[:30],
            body=body,
            status=MessageStatus.RECEIVED.value,
            metadata=metadata or {},
            created_at=occurred_at or datetime.now(timezone.utc),
        )
        session.add(message)
        conversation.last_message_at = message.created_at
        conversation.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(message)
        logger.info(
            "inbound_message_recorded",
            extra={"extra_fields": {
                "conversation_id": str(conversation.id),
                "sending_account_id": str(account.id) if account else None,
                "lead_matched": conversation.lead_id is not None,
                "message_type": message.message_type,
            }},
        )
        return conversation, message

    # ------------------------------------------------- campaign reply linkage
    async def link_reply_to_campaign(
        self, session: AsyncSession, *,
        account: SendingAccount | None,
        contact_phone: str | None,
        occurred_at: datetime | None = None,
    ) -> CampaignRecipient | None:
        """When the contact was a campaign recipient (sent/delivered/read),
        record REPLIED on that recipient + a MESSAGE_REPLIED campaign event.

        Replies that cannot be tied to a campaign are simply stored in the
        conversation — no event is fabricated."""
        if not contact_phone:
            return None
        normalized = normalize_lead_phone(contact_phone)
        digits = (normalized or "").lstrip("+")
        lead_id = None
        if digits:
            lead = (await session.execute(
                select(Lead.id).where(Lead.phone_norm.in_([digits, f"+{digits}"])).limit(1)
            )).scalar_one_or_none()
            lead_id = lead

        candidates = (await session.execute(
            select(CampaignRecipient)
            .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
            .where(
                CampaignRecipient.status.in_([
                    RecipientStatus.SENT.value,
                    RecipientStatus.DELIVERED.value,
                    RecipientStatus.READ.value,
                ]),
                Campaign.sending_account_id == (account.id if account else None),
            )
            .order_by(CampaignRecipient.sent_at.desc().nullslast(), CampaignRecipient.created_at.desc())
            .limit(50)
        )).scalars().all()

        recipient = None
        if lead_id is not None:
            recipient = next((r for r in candidates if r.lead_id == lead_id), None)
        if recipient is None and candidates:
            # fall back to the recipient address match (phone may differ in
            # formatting from the lead row)
            wanted = digits
            for candidate in candidates:
                if "".join(ch for ch in candidate.recipient_address if ch.isdigit()).lstrip("+").endswith(wanted[-9:]):
                    recipient = candidate
                    break
        if recipient is None:
            return None

        changed = apply_event(
            recipient, EventType.MESSAGE_REPLIED,
            timestamp=occurred_at or datetime.now(timezone.utc),
        )
        if changed or recipient.status == RecipientStatus.REPLIED.value:
            await session.commit()
            from app.services.marketing.events import EventService
            await EventService().record(
                session, campaign_id=recipient.campaign_id, recipient_id=recipient.id,
                event_type=EventType.MESSAGE_REPLIED,
                provider=account.provider if account else None,
                metadata={"linked_by": "inbound_message"},
            )
            logger.info(
                "campaign_reply_linked",
                extra={"extra_fields": {
                    "campaign_id": str(recipient.campaign_id),
                    "recipient_id": str(recipient.id),
                }},
            )
            return recipient
        return None
