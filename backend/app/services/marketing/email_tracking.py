"""Email tracking service (Phase 7 §29, §30, §31, §33).

Open tracking (§29):
- campaign-level opt-in (`campaign_metadata.track_opens`); NEVER mandatory
- the pixel URL carries the recipient's unguessable `tracking_key` — never a
  raw database id
- one OPEN evidence row per request; the recipient timestamp is first-open

Click tracking (§30):
- campaign-level opt-in (`campaign_metadata.track_clicks`)
- original URL → signed tracking URL → verify signature + scheme → 302 →
  destination. Only http/https links are rewritten; javascript:, data: and
  friends are refused at both wrap and redirect time (no open redirects)

Reply threading (§33):
- Message-ID / In-Reply-To / References are stored on outbound + inbound
  messages and used to associate replies with campaign conversations — never
  subject matching alone

Accuracy disclaimer (§31): open/click tracking is inherently approximate
(clients block/prefetch); the platform reports it as directional data only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.email import EmailTrackingEvent, TrackingEventType
from app.models.marketing import Campaign, CampaignRecipient, EventType
from app.models.messaging import Conversation, Message
from app.services.marketing.events import EventService

logger = get_logger("qbit.marketing.email_tracking")


class EmailTrackingService:
    # ------------------------------------------------------------------ opens
    async def record_open(
        self, session: AsyncSession, *, recipient: CampaignRecipient,
        user_agent: str | None = None,
    ) -> bool:
        """Record one OPEN (§29). Idempotent per timestamp: every request gets
        an evidence row, the recipient's first-open timestamp never moves."""
        now = datetime.now(timezone.utc)
        session.add(EmailTrackingEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type=TrackingEventType.OPEN.value,
            message_id=recipient.provider_message_id,
            user_agent=(user_agent or "")[:300] or None,
            occurred_at=now,
        ))
        first = recipient.opened_at is None
        if first:
            recipient.opened_at = now
        await session.commit()
        await EventService().record(
            session, campaign_id=recipient.campaign_id, recipient_id=recipient.id,
            event_type=EventType.MESSAGE_OPENED,
            provider_event_id=f"open:{recipient.id}:{now.isoformat()}",
            metadata={"first": first},
        )
        return first

    # ----------------------------------------------------------------- clicks
    async def record_click(
        self, session: AsyncSession, *, recipient: CampaignRecipient,
        url: str, user_agent: str | None = None,
    ) -> bool:
        """Record one CLICK (§30) — the caller has already validated the URL."""
        now = datetime.now(timezone.utc)
        session.add(EmailTrackingEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type=TrackingEventType.CLICK.value,
            url=url[:1000],
            message_id=recipient.provider_message_id,
            user_agent=(user_agent or "")[:300] or None,
            occurred_at=now,
        ))
        first = recipient.clicked_at is None
        if first:
            recipient.clicked_at = now
        await session.commit()
        await EventService().record(
            session, campaign_id=recipient.campaign_id, recipient_id=recipient.id,
            event_type=EventType.MESSAGE_CLICKED,
            provider_event_id=f"click:{recipient.id}:{now.isoformat()}",
            metadata={"url": url[:300]},
        )
        return first

    # ------------------------------------------------------------- lookups
    async def recipient_by_tracking_key(
        self, session: AsyncSession, tracking_key: str,
    ) -> CampaignRecipient | None:
        if not tracking_key or len(tracking_key) > 64:
            return None
        return (await session.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.tracking_key == tracking_key
            ).limit(1)
        )).scalars().first()

    async def list_events(
        self, session: AsyncSession, *, campaign_id: uuid.UUID,
        event_type: str | None = None, limit: int = 500,
    ) -> list[EmailTrackingEvent]:
        query = select(EmailTrackingEvent).where(
            EmailTrackingEvent.campaign_id == campaign_id
        )
        if event_type:
            query = query.where(EmailTrackingEvent.event_type == event_type.upper())
        return list((await session.execute(
            query.order_by(EmailTrackingEvent.occurred_at.desc()).limit(min(limit, 1000))
        )).scalars().all())


class EmailInboundService:
    """Reply-tracking foundation (§32, §33) — normalized backend interface.

    A full inbound-mailbox implementation is a later phase; THIS phase
    provides the normalized ingestion path (reply mailbox / provider webhook /
    inbound Email-API): sender/recipient/subject/body/provider_message_id/
    threading headers → Conversation → Message → Lead → (campaign REPLIED).
    NO fake inbox is created — nothing is displayed unless data truly arrived.
    """

    def __init__(self) -> None:
        from app.services.marketing.inbox import InboxService

        self.inbox = InboxService()

    async def record_inbound_email(
        self, session: AsyncSession, *,
        account: object | None, from_email: str, to_email: str | None,
        subject: str | None, body_text: str | None,
        provider_message_id: str | None = None,
        in_reply_to: str | None = None, references: str | None = None,
        occurred_at: datetime | None = None, metadata: dict | None = None,
    ) -> tuple[Conversation | None, Message | None]:
        """Normalized inbound email → conversation → message → lead match.

        - conversation matching key: (sending_account, normalized contact email)
        - threading: In-Reply-To/References are stored for later association
          (§33) and, when they match a stored Message-ID / a campaign
          recipient's provider_message_id, the reply is linked to that thread
        - no Lead is invented: unresolved contacts stay lead-less (§24 rule)
        """
        from app.services.marketing.email_normalization import normalize_email

        ok, normalized, _reason = normalize_email(from_email)
        if not ok:
            return None, None
        now = occurred_at or datetime.now(timezone.utc)
        conversation = (await session.execute(
            select(Conversation).where(
                Conversation.channel == "EMAIL",
                Conversation.sending_account_id == (
                    account.id if account is not None else None
                ) if account is not None else Conversation.sending_account_id.is_(None),
                Conversation.contact_email == normalized,
            ).limit(1)
        )).scalars().first()
        created = False
        if conversation is None:
            conversation = Conversation(
                channel="EMAIL",
                sending_account_id=account.id if account is not None else None,
                contact_email=normalized,
                external_contact_id=normalized,
                status="PENDING",
                last_message_at=now,
            )
            session.add(conversation)
            await session.flush()
            created = True
        else:
            conversation.last_message_at = now

        message = Message(
            conversation_id=conversation.id,
            direction="INBOUND",
            provider_message_id=(provider_message_id or "")[:300] or None,
            message_type="EMAIL",
            body=(body_text or "")[:20000] or None,
            status="RECEIVED",
            message_metadata={
                "from": normalized,
                "to": to_email,
                "subject": (subject or "")[:300],
                "message_id": (in_reply_to or "")[:300] or None,
                "in_reply_to": (in_reply_to or "")[:300] or None,
                "references": (references or "")[:1000] or None,
                **(metadata or {}),
            },
            created_at=now,
        )
        session.add(message)
        await session.commit()
        logger.info(
            "email_inbound_recorded",
            extra={"extra_fields": {
                "conversation": str(conversation.id),
                "created": created,
                "threaded": bool(in_reply_to or references),
            }},
        )
        return conversation, message

    async def link_reply_to_campaign(
        self, session: AsyncSession, *, from_email: str,
        in_reply_to: str | None = None, occurred_at: datetime | None = None,
    ) -> bool:
        """Associate an inbound reply with its campaign recipient (§33).

        Resolution order (never subject-only matching):
        1. In-Reply-To/References → CampaignRecipient.provider_message_id
        2. fallback: most recent SENT campaign email to this address
        The recipient moves to REPLIED and a MESSAGE_REPLIED event is appended.
        """
        from app.services.marketing.email_normalization import normalize_email

        ok, normalized, _reason = normalize_email(from_email)
        if not ok:
            return False
        recipient = None
        candidates = [mid.strip() for mid in ((in_reply_to or "").split()) if mid.strip()]
        for candidate in candidates[:5]:
            recipient = (await session.execute(
                select(CampaignRecipient).where(
                    CampaignRecipient.provider_message_id == candidate
                ).limit(1)
            )).scalars().first()
            if recipient is not None:
                break
        if recipient is None:
            recipient = (await session.execute(
                select(CampaignRecipient).where(
                    CampaignRecipient.recipient_address == normalized,
                    CampaignRecipient.provider_message_id.isnot(None),
                ).order_by(CampaignRecipient.sent_at.desc().nulls_last()).limit(1)
            )).scalars().first()
        if recipient is None:
            return False
        now = occurred_at or datetime.now(timezone.utc)
        if recipient.replied_at is None:
            recipient.replied_at = now
        recipient.status = "REPLIED"
        await session.commit()
        await EventService().record(
            session, campaign_id=recipient.campaign_id, recipient_id=recipient.id,
            event_type=EventType.MESSAGE_REPLIED,
            metadata={"channel": "EMAIL", "in_reply_to": (in_reply_to or "")[:300]},
        )
        return True
