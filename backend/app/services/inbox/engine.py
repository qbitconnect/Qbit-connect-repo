"""Conversation engine (Phase 8 §2, §5–§8, §31–§34, §38–§41, §61).

Single ingestion path for ALL channels:

    UnifiedInboundMessage → contact identity → Lead match
                          → Conversation (thread) → Message
                          → unread / reopen / activity

Hard rules enforced here:
- idempotency: a provider_message_id can only ever create ONE message per
  conversation (duplicate webhooks are no-ops, §38–§40)
- lead matching is exact-normalized only; multiple candidate leads set
  MATCH_REVIEW_REQUIRED instead of guessing (§6, scenario 4)
- no lead data is ever invented for unknown contacts (§7)
- delivery status moves forward only — READ never downgrades to DELIVERED
  when events arrive out of order (§41)
- workflow changes (status/priority/assignment/link) append ConversationEvent
  history rows — nothing is silently rewritten (§4, §29, §34)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.marketing import SendingAccount
from app.models.messaging import (
    Conversation,
    ConversationEvent,
    ConversationNote,
    ConversationPriority,
    ConversationStatus,
    MatchStatus,
    Message,
    MessageDirection,
    MessageStatus,
)
from app.models.scrape import Lead
from app.services.inbox.normalizer import UnifiedInboundMessage, utcnow
from app.services.scraping.lead_keys import normalize_phone as normalize_lead_phone

logger = get_logger("qbit.inbox.engine")

#: conversation workflow event vocabulary (§34)
EVENT_CONVERSATION_CREATED = "CONVERSATION_CREATED"
EVENT_MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
EVENT_MESSAGE_SENT = "MESSAGE_SENT"
EVENT_MESSAGE_FAILED = "MESSAGE_FAILED"
EVENT_STATUS_CHANGED = "STATUS_CHANGED"
EVENT_PRIORITY_CHANGED = "PRIORITY_CHANGED"
EVENT_ASSIGNED = "ASSIGNED"
EVENT_UNASSIGNED = "UNASSIGNED"
EVENT_LEAD_LINKED = "LEAD_LINKED"
EVENT_LEAD_UNLINKED = "LEAD_UNLINKED"
EVENT_LEAD_CREATED = "LEAD_CREATED"
EVENT_NOTE_ADDED = "NOTE_ADDED"
EVENT_CONVERSATION_REOPENED = "CONVERSATION_REOPENED"

#: forward-only ladder for OUTBOUND delivery statuses (§41)
_OUTBOUND_LADDER = [
    MessageStatus.SENDING.value,
    MessageStatus.SENT.value,
    MessageStatus.DELIVERED.value,
    MessageStatus.READ.value,
]


def _status_rank(value: str) -> int:
    try:
        return _OUTBOUND_LADDER.index(value)
    except ValueError:
        return -1


class ConversationEngine:
    # ------------------------------------------------------------- ingestion
    async def ingest_inbound(
        self,
        session: AsyncSession,
        msg: UnifiedInboundMessage,
        *,
        account: SendingAccount | None,
        settings=None,
    ) -> tuple[Conversation, Message, bool]:
        """Persist one normalized inbound message exactly once (§38–§40).

        Returns (conversation, message, created). Duplicate provider message
        ids return the EXISTING row with created=False — webhook retries and
        out-of-order redelivery can never duplicate messages.
        """
        conversation = await self.get_or_create_conversation(
            session, channel=msg.channel, account=account, msg=msg,
        )

        # ---- message-level idempotency (§40) --------------------------------
        if msg.provider_message_id:
            existing = (await session.execute(
                select(Message).where(
                    Message.conversation_id == conversation.id,
                    Message.provider_message_id == msg.provider_message_id,
                    Message.direction == MessageDirection.INBOUND.value,
                ).limit(1)
            )).scalars().first()
            if existing is not None:
                return conversation, existing, False

        # ---- lead matching (§6) ---------------------------------------------
        lead, match_status = await self.match_lead_for_conversation(
            session, phone=msg.contact_phone, email=msg.contact_email,
        )
        events: list[tuple[str, dict, dict]] = []
        is_new = conversation.match_status is None and conversation.last_message_at is None
        if conversation.lead_id is None and lead is not None:
            conversation.lead_id = lead.id
            conversation.match_status = match_status
            events.append((EVENT_LEAD_LINKED, {},
                           {"lead_id": str(lead.id), "matched_by": "inbound"}))
        elif conversation.lead_id is None:
            conversation.match_status = match_status or MatchStatus.UNMATCHED.value

        if conversation.lead_id is not None and conversation.status == ConversationStatus.PENDING.value:
            conversation.status = ConversationStatus.OPEN.value

        # ---- reopen-on-reply business rule (§32) -----------------------------
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        if (
            getattr(settings, "QBIT_INBOX_REOPEN_ON_REPLY", True)
            and conversation.status in (ConversationStatus.RESOLVED.value,
                                        ConversationStatus.CLOSED.value)
        ):
            previous = conversation.status
            conversation.status = ConversationStatus.OPEN.value
            conversation.closed_at = None
            events.append((EVENT_CONVERSATION_REOPENED, {"status": previous},
                           {"status": conversation.status, "reason": "inbound_reply"}))

        message = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND.value,
            provider_message_id=msg.provider_message_id,
            message_type=(msg.message_type or "TEXT").upper()[:30],
            body=msg.body,
            subject=msg.subject,
            sender=(msg.sender or "")[:320] or None,
            recipient=(msg.recipient or "")[:320] or None,
            lead_id=conversation.lead_id,
            status=MessageStatus.RECEIVED.value,
            message_metadata=self._inbound_metadata(msg),
            created_at=msg.occurred_at or utcnow(),
        )
        session.add(message)

        now = utcnow()
        if is_new:
            events.insert(0, (EVENT_CONVERSATION_CREATED, {}, {"channel": msg.channel}))
        events.append((EVENT_MESSAGE_RECEIVED, {},
                       {"provider_message_id": msg.provider_message_id,
                        "message_type": message.message_type}))
        for event_type, previous, new in events:
            session.add(ConversationEvent(
                conversation_id=conversation.id,
                event_type=event_type, previous_value=previous, new_value=new,
                created_at=now,
            ))

        conversation.last_message_at = max(
            conversation.last_message_at or message.created_at, message.created_at,
        )
        conversation.last_inbound_at = max(
            conversation.last_inbound_at or message.created_at, message.created_at,
        )
        conversation.unread_count = (conversation.unread_count or 0) + 1
        conversation.updated_at = now
        if msg.subject and conversation.channel == "EMAIL" and not conversation.subject:
            conversation.subject = msg.subject

        await session.flush()
        # Phase 9 §9/§10: conversation + message triggers (best-effort intake)
        await _emit_automation_events_for_inbound(
            session, conversation=conversation, message=message,
            is_new=is_new, reopened=any(e[0] == EVENT_CONVERSATION_REOPENED for e in events),
        )
        await session.commit()
        await session.refresh(message)
        logger.info(
            "inbound_message_ingested",
            extra={"extra_fields": {
                "conversation_id": str(conversation.id),
                "channel": msg.channel,
                "lead_matched": conversation.lead_id is not None,
                "match_status": conversation.match_status,
                "created": True,
            }},
        )
        return conversation, message, True

    async def ingest_campaign_outbound(
        self,
        session: AsyncSession,
        *,
        campaign, recipient, account: SendingAccount | None,
        provider_message_id: str | None,
        subject: str | None, body: str | None,
        message_type: str = "TEMPLATE",
        occurred_at: datetime | None = None,
    ) -> tuple[Conversation, Message] | None:
        """Campaign sends appear in the conversation history (§61).

        Called by the campaign worker AFTER a confirmed provider send. Never
        increments unread (outbound); never invents a lead — the recipient's
        lead is attached when present. Failures here must never break the
        campaign loop (caller wraps in try/except)."""
        channel = (campaign.channel or "").upper()
        if channel not in ("WHATSAPP", "EMAIL"):
            return None
        now = occurred_at or utcnow()
        lead_id = recipient.lead_id
        lead = await session.get(Lead, lead_id) if lead_id else None

        contact_phone = None
        contact_email = None
        external_contact_id = None
        if channel == "WHATSAPP":
            if lead is not None and lead.phone:
                normalized = normalize_lead_phone(lead.phone)
                digits = (normalized or "").lstrip("+")
                contact_phone = f"+{digits}" if digits else None
            else:
                digits = "".join(ch for ch in (recipient.recipient_address or "") if ch.isdigit())
                contact_phone = f"+{digits}" if digits else None
            external_contact_id = contact_phone
        else:
            from app.services.marketing.email_normalization import normalize_email

            ok, normalized, _r = normalize_email(recipient.recipient_address or (lead.email if lead else None) or "")
            if not ok:
                return None
            contact_email = normalized
            external_contact_id = normalized

        conversation = await self._find_conversation(
            session, channel=channel, account_id=account.id if account else None,
            contact_phone=contact_phone, contact_email=contact_email,
        )
        created = False
        if conversation is None:
            conversation = Conversation(
                channel=channel,
                sending_account_id=account.id if account else None,
                lead_id=lead_id,
                contact_phone=contact_phone,
                contact_email=contact_email,
                external_contact_id=external_contact_id,
                status=ConversationStatus.OPEN.value if lead_id else ConversationStatus.PENDING.value,
                match_status=MatchStatus.MATCHED.value if lead_id else MatchStatus.UNMATCHED.value,
                subject=(subject or "")[:300] or None if channel == "EMAIL" else None,
            )
            session.add(conversation)
            await session.flush()
            created = True
            session.add(ConversationEvent(
                conversation_id=conversation.id,
                event_type=EVENT_CONVERSATION_CREATED,
                new_value={"channel": channel, "origin": "campaign"},
            ))
        elif conversation.lead_id is None and lead_id is not None:
            conversation.lead_id = lead_id
            conversation.match_status = MatchStatus.MATCHED.value
            if conversation.status == ConversationStatus.PENDING.value:
                conversation.status = ConversationStatus.OPEN.value
            session.add(ConversationEvent(
                conversation_id=conversation.id, event_type=EVENT_LEAD_LINKED,
                new_value={"lead_id": str(lead_id), "matched_by": "campaign"},
            ))

        message = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND.value,
            provider_message_id=provider_message_id,
            message_type=message_type,
            body=body,
            subject=subject,
            sender=account.display_identifier or account.identifier if account else None,
            recipient=recipient.recipient_address,
            lead_id=lead_id,
            status=MessageStatus.SENT.value,
            message_metadata={
                "origin": "campaign",
                "campaign_id": str(campaign.id),
                "campaign_recipient_id": str(recipient.id),
            },
            created_at=now,
            sent_at=now,
        )
        session.add(message)
        session.add(ConversationEvent(
            conversation_id=conversation.id, event_type=EVENT_MESSAGE_SENT,
            new_value={"provider_message_id": provider_message_id,
                       "origin": "campaign"},
            created_at=now,
        ))
        conversation.last_message_at = now
        conversation.last_outbound_at = now
        conversation.updated_at = now
        if channel == "EMAIL" and subject and not conversation.subject:
            conversation.subject = subject[:300]
        await session.commit()
        _ = created
        return conversation, message

    # ------------------------------------------------------------ delivery
    async def apply_delivery_to_message(
        self,
        session: AsyncSession,
        *,
        provider_message_id: str | None,
        event_type: str,
        occurred_at: datetime | None = None,
    ) -> bool:
        """Mirror a provider delivery event onto the Message row (§17, §41).

        Forward-only: READ never downgrades to DELIVERED; FAILED timestamps
        are first-write. Returns True when a message was updated."""
        if not provider_message_id:
            return False
        message = (await session.execute(
            select(Message).where(
                Message.provider_message_id == provider_message_id,
                Message.direction == MessageDirection.OUTBOUND.value,
            ).limit(1)
        )).scalars().first()
        if message is None:
            return False
        when = occurred_at or utcnow()
        mapping = {
            "MESSAGE_SENT": (MessageStatus.SENT, "sent_at"),
            "MESSAGE_DELIVERED": (MessageStatus.DELIVERED, "delivered_at"),
            "MESSAGE_READ": (MessageStatus.READ, "read_at"),
            "MESSAGE_FAILED": (MessageStatus.FAILED, "failed_at"),
        }
        target = mapping.get(event_type)
        if target is None:
            return False
        status, field_name = target
        current_rank = _status_rank(message.status)
        new_rank = _status_rank(status.value)
        if status is MessageStatus.FAILED:
            if message.status == MessageStatus.FAILED.value:
                return False
            if message.status not in (MessageStatus.SENDING.value, MessageStatus.SENT.value,
                                      MessageStatus.DELIVERED.value, MessageStatus.READ.value):
                return False
            message.status = status.value
            if message.failed_at is None:
                message.failed_at = when
        elif new_rank >= current_rank and message.status != status.value:
            message.status = status.value
        if getattr(message, field_name) is None:
            setattr(message, field_name, when)
        session.add(ConversationEvent(
            conversation_id=message.conversation_id, event_type=event_type,
            new_value={"message_id": str(message.id),
                       "provider_message_id": provider_message_id},
            created_at=utcnow(),
        ))
        await session.commit()
        return True

    # ---------------------------------------------------------- threading
    async def get_or_create_conversation(
        self, session: AsyncSession, *, channel: str,
        account: SendingAccount | None, msg: UnifiedInboundMessage,
    ) -> Conversation:
        """Find the thread for this contact (§5).

        WhatsApp: (sending_account, normalized phone). Email: (sending_account,
        normalized email). Never relies on subject matching and never creates
        duplicate conversations for every message."""
        conversation = await self._find_conversation(
            session, channel=channel,
            account_id=account.id if account else None,
            contact_phone=msg.contact_phone, contact_email=msg.contact_email,
        )
        if conversation is not None:
            # enrich provider display data only (never overwrite with guesses)
            if msg.external_contact_id and not conversation.external_contact_id:
                conversation.external_contact_id = msg.external_contact_id[:300]
            return conversation
        conversation = Conversation(
            channel=channel,
            sending_account_id=account.id if account else None,
            contact_phone=msg.contact_phone,
            contact_email=msg.contact_email,
            external_contact_id=(msg.external_contact_id or "")[:300] or None,
            status=ConversationStatus.PENDING.value,
            subject=msg.subject,
        )
        session.add(conversation)
        await session.flush()
        return conversation

    async def _find_conversation(
        self, session: AsyncSession, *, channel: str,
        account_id: uuid.UUID | None,
        contact_phone: str | None, contact_email: str | None,
    ) -> Conversation | None:
        query = select(Conversation).where(
            Conversation.channel == channel,
            Conversation.sending_account_id == account_id
            if account_id is not None
            else Conversation.sending_account_id.is_(None),
        )
        if contact_email:
            query = query.where(Conversation.contact_email == contact_email)
        elif contact_phone:
            query = query.where(Conversation.contact_phone == contact_phone)
        else:
            return None
        return (await session.execute(
            query.order_by(Conversation.created_at).limit(1)
        )).scalars().first()

    # -------------------------------------------------------- lead matching
    async def match_lead_for_conversation(
        self, session: AsyncSession, *,
        phone: str | None, email: str | None,
    ) -> tuple[Lead | None, str | None]:
        """Exact-normalized lead match (§6).

        0 candidates → (None, UNMATCHED); 1 → (lead, MATCHED);
        >1 → (None, MATCH_REVIEW_REQUIRED) — never silently attach."""
        if email:
            candidates = (await session.execute(
                select(Lead).where(Lead.email_norm == email).limit(2)
            )).scalars().all()
            if len(candidates) > 1:
                return None, MatchStatus.MATCH_REVIEW_REQUIRED.value
            if candidates:
                return candidates[0], MatchStatus.MATCHED.value
            return None, MatchStatus.UNMATCHED.value
        if phone:
            normalized = normalize_lead_phone(phone)
            digits = (normalized or "").lstrip("+")
            if not digits:
                return None, MatchStatus.UNMATCHED.value
            candidates = (await session.execute(
                select(Lead).where(Lead.phone_norm.in_([digits, f"+{digits}"])).limit(2)
            )).scalars().all()
            if len(candidates) > 1:
                return None, MatchStatus.MATCH_REVIEW_REQUIRED.value
            if candidates:
                return candidates[0], MatchStatus.MATCHED.value
            return None, MatchStatus.UNMATCHED.value
        return None, None

    # ------------------------------------------------------------ workflow
    async def change_status(
        self, session: AsyncSession, conversation: Conversation, new_status: str,
        *, actor_user_id: uuid.UUID | None = None,
    ) -> Conversation:
        try:
            status = ConversationStatus(new_status)
        except ValueError as exc:
            raise ValidationError(f"Unknown conversation status '{new_status}'") from exc
        previous = conversation.status
        if previous == status.value:
            return conversation
        conversation.status = status.value
        conversation.closed_at = utcnow() if status is ConversationStatus.CLOSED else None
        conversation.updated_at = utcnow()
        session.add(ConversationEvent(
            conversation_id=conversation.id, event_type=EVENT_STATUS_CHANGED,
            actor_user_id=actor_user_id,
            previous_value={"status": previous}, new_value={"status": status.value},
        ))
        await _emit_automation_conv(
            session, conversation=conversation, event_type="conversation.status_changed",
            payload={"from_status": previous, "to_status": status.value},
        )
        await session.commit()
        return conversation

    async def change_priority(
        self, session: AsyncSession, conversation: Conversation, new_priority: str | None,
        *, actor_user_id: uuid.UUID | None = None,
    ) -> Conversation:
        if new_priority is not None:
            try:
                ConversationPriority(new_priority)
            except ValueError as exc:
                raise ValidationError(f"Unknown priority '{new_priority}'") from exc
        previous = conversation.priority
        target = new_priority or ConversationPriority.NORMAL.value
        if previous == target:
            return conversation
        conversation.priority = target
        conversation.updated_at = utcnow()
        session.add(ConversationEvent(
            conversation_id=conversation.id, event_type=EVENT_PRIORITY_CHANGED,
            actor_user_id=actor_user_id,
            previous_value={"priority": previous}, new_value={"priority": target},
        ))
        await session.commit()
        return conversation

    async def assign_user(
        self, session: AsyncSession, conversation: Conversation,
        assigned_user_id: uuid.UUID | None,
        *, actor_user_id: uuid.UUID | None = None,
    ) -> Conversation:
        """Assign to a user, or unassign with None (§28, §29).

        The full assignment history (previous → new, actor, timestamp) is
        appended to the activity timeline."""
        previous_id = conversation.assigned_user_id
        if assigned_user_id is not None:
            from app.models.user import User

            user = await session.get(User, assigned_user_id)
            if user is None:
                raise NotFoundError("User not found")
        if previous_id == assigned_user_id:
            return conversation
        conversation.assigned_user_id = assigned_user_id
        conversation.updated_at = utcnow()
        session.add(ConversationEvent(
            conversation_id=conversation.id,
            event_type=EVENT_ASSIGNED if assigned_user_id else EVENT_UNASSIGNED,
            actor_user_id=actor_user_id,
            previous_value={"assigned_user_id": str(previous_id) if previous_id else None},
            new_value={"assigned_user_id": str(assigned_user_id) if assigned_user_id else None},
        ))
        if assigned_user_id is not None:
            await _emit_automation_conv(
                session, conversation=conversation, event_type="conversation.assigned",
                payload={"assigned_user_id": str(assigned_user_id),
                         "previous_user_id": str(previous_id) if previous_id else None},
            )
        await session.commit()
        return conversation

    async def link_lead(
        self, session: AsyncSession, conversation: Conversation, lead_id: uuid.UUID,
        *, actor_user_id: uuid.UUID | None = None,
    ) -> Conversation:
        """Link an existing lead — history is preserved, nothing duplicated (§8)."""
        lead = await session.get(Lead, lead_id)
        if lead is None:
            raise NotFoundError("Lead not found")
        previous_id = conversation.lead_id
        conversation.lead_id = lead.id
        conversation.match_status = MatchStatus.MATCHED.value
        if conversation.status == ConversationStatus.PENDING.value:
            conversation.status = ConversationStatus.OPEN.value
        conversation.updated_at = utcnow()
        session.add(ConversationEvent(
            conversation_id=conversation.id, event_type=EVENT_LEAD_LINKED,
            actor_user_id=actor_user_id,
            previous_value={"lead_id": str(previous_id) if previous_id else None},
            new_value={"lead_id": str(lead.id), "via": "manual_link"},
        ))
        # backfill lead id on messages so the timeline stays consistent
        from sqlalchemy import update

        await session.execute(
            update(Message)
            .where(Message.conversation_id == conversation.id, Message.lead_id.is_(None))
            .values(lead_id=lead.id)
        )
        await session.commit()
        return conversation

    async def unlink_lead(
        self, session: AsyncSession, conversation: Conversation,
        *, actor_user_id: uuid.UUID | None = None,
    ) -> Conversation:
        previous_id = conversation.lead_id
        if previous_id is None:
            return conversation
        conversation.lead_id = None
        conversation.match_status = MatchStatus.UNMATCHED.value
        if conversation.status == ConversationStatus.OPEN.value:
            conversation.status = ConversationStatus.PENDING.value
        conversation.updated_at = utcnow()
        session.add(ConversationEvent(
            conversation_id=conversation.id, event_type=EVENT_LEAD_UNLINKED,
            actor_user_id=actor_user_id,
            previous_value={"lead_id": str(previous_id)},
        ))
        await session.commit()
        return conversation

    async def create_lead_from_conversation(
        self, session: AsyncSession, conversation: Conversation,
        *, actor_user_id: uuid.UUID | None = None,
        display_name: str | None = None, commit: bool = True,
    ) -> Lead:
        """Create a lead from an unresolved contact (§7).

        ONLY provider-received data is used: the normalized phone/email and,
        when the provider supplied one, the display name. Company, address,
        industry etc. are NEVER fabricated."""
        if conversation.lead_id is not None:
            raise ConflictError("Conversation is already linked to a lead")

        from app.services.leads.service import LeadWorkspaceService

        contact_name = (display_name or conversation.external_contact_id or "").strip() or None
        identifier = conversation.contact_email or conversation.contact_phone or ""
        payload: dict = {}
        if contact_name:
            payload["contact_name"] = contact_name[:300]
        else:
            # the contact identifier itself is the only honest label available
            payload["business_name"] = identifier[:300] or "Unknown contact"
        if conversation.contact_email:
            payload["email"] = conversation.contact_email
        if conversation.contact_phone:
            payload["phone"] = conversation.contact_phone

        lead = await LeadWorkspaceService().create_lead(
            session, payload,
            user_id=actor_user_id,
            source="inbox",
            source_type="inbox",
            commit=commit,
        )
        conversation.lead_id = lead.id
        conversation.match_status = MatchStatus.MATCHED.value
        if conversation.status == ConversationStatus.PENDING.value:
            conversation.status = ConversationStatus.OPEN.value
        conversation.updated_at = utcnow()
        session.add(ConversationEvent(
            conversation_id=conversation.id, event_type=EVENT_LEAD_CREATED,
            actor_user_id=actor_user_id,
            new_value={"lead_id": str(lead.id), "via": "inbox_create"},
        ))
        if commit:
            await session.commit()
        return lead

    async def add_note(
        self, session: AsyncSession, conversation: Conversation, content: str,
        *, user_id: uuid.UUID | None = None,
    ) -> ConversationNote:
        """Internal team note — visible to the team, NEVER sent to the customer (§27)."""
        text = (content or "").strip()
        if not text:
            raise ValidationError("Note content is required")
        if len(text) > 10000:
            raise ValidationError("Note is too long (max 10000 characters)")
        note = ConversationNote(
            conversation_id=conversation.id, user_id=user_id, content=text,
        )
        session.add(note)
        session.add(ConversationEvent(
            conversation_id=conversation.id, event_type=EVENT_NOTE_ADDED,
            actor_user_id=user_id,
        ))
        await session.commit()
        await session.refresh(note)
        return note

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _inbound_metadata(msg: UnifiedInboundMessage) -> dict:
        """Bounded metadata: threading headers + provider-derived extras (§5, §42).

        The EMAIL threading-header contract (Phase 7 §32/§33) always carries
        the full header set (None when absent) — downstream tests and the UI
        rely on stable keys."""
        metadata = dict(msg.metadata or {})
        metadata.setdefault("message_id", msg.message_id_header)
        metadata.setdefault("in_reply_to", msg.in_reply_to)
        metadata.setdefault("references", msg.references)
        if msg.subject:
            metadata.setdefault("subject", msg.subject)
        if msg.recipient:
            metadata.setdefault("to", msg.recipient)
        return metadata

    @staticmethod
    def within_whatsapp_window(
        conversation: Conversation, *, hours: int, now: datetime | None = None,
    ) -> bool:
        """WhatsApp 24h customer-service window check (§22).

        Free-text replies are allowed only while the window is open; outside
        it, providers require an approved template. This models the
        provider's published rule — it is never bypassed."""
        last_inbound = conversation.last_inbound_at
        if last_inbound is None:
            return False
        reference = now or datetime.now(timezone.utc)
        if last_inbound.tzinfo is None:
            last_inbound = last_inbound.replace(tzinfo=timezone.utc)
        return reference - last_inbound <= timedelta(hours=hours)


async def _emit_automation_conv(session, *, conversation, event_type: str,
                                payload: dict | None = None) -> None:
    """Best-effort automation intake for conversation events (Phase 9 §9)."""
    try:
        from app.automation.services.event_dispatcher import emit_system_event

        await emit_system_event(
            session, event_type=event_type, entity_type="conversation",
            entity_id=conversation.id,
            payload={"refs": {
                "conversation_id": str(conversation.id),
                "lead_id": str(conversation.lead_id) if conversation.lead_id else None,
            }, **(payload or {})},
        )
    except Exception:  # noqa: BLE001 — never break inbox flow
        pass


async def _emit_automation_events_for_inbound(session, *, conversation, message,
                                              is_new: bool, reopened: bool) -> None:
    """Best-effort automation intake for one ingested inbound message (§9/§10):
    CONVERSATION_CREATED / CONVERSATION_REOPENED / INBOUND_MESSAGE / MESSAGE_RECEIVED."""
    try:
        from app.automation.services.event_dispatcher import emit_system_event

        base_refs = {
            "conversation_id": str(conversation.id),
            "lead_id": str(conversation.lead_id) if conversation.lead_id else None,
            "message_id": str(message.id),
        }
        if is_new:
            await emit_system_event(
                session, event_type="conversation.created", entity_type="conversation",
                entity_id=conversation.id, payload={"refs": base_refs},
            )
        if reopened:
            await emit_system_event(
                session, event_type="conversation.reopened", entity_type="conversation",
                entity_id=conversation.id, payload={"refs": base_refs},
            )
        # INBOUND_MESSAGE and MESSAGE_RECEIVED share the message refs
        for event_type in ("conversation.inbound_message", "message.received"):
            await emit_system_event(
                session, event_type=event_type, entity_type="message",
                entity_id=message.id, payload={"refs": base_refs},
                event_id=f"{event_type}:{message.id}",
            )
    except Exception:  # noqa: BLE001 — never break inbox flow
        pass
