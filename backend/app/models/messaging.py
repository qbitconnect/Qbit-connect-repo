"""Phase 6 messaging foundation tables.

Tables:
- ProviderCredentials  encrypted-at-rest provider secrets (Fernet ciphertext
                       only — plaintext secrets NEVER exist in a column)
- ProviderEvent        raw webhook events, idempotent by
                       UNIQUE (provider, provider_event_id) — the dedup layer
                       that keeps delivery events from ever double-counting
- Conversation         inbound/outbound thread with one contact per sending
                       account (Inbox foundation, Phase 6 §23)
- Message              one message inside a conversation (INBOUND/OUTBOUND)

Design rules:
- additive-only vs earlier phases; PortableJSON so SQLite tests == PG prod
- no provider secrets in any metadata column (redaction applied upstream)
- conversations match leads by (sending_account_id, contact_phone); a lead_id
  is attached ONLY when a real Lead row matches — unresolved contacts stay
  lead-less instead of inventing personal information (§24)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, timestamp_columns, uuid_pk
from app.models.scrape import PortableJSON


class CredentialKeyVersion(int, enum.Enum):
    V1 = 1


class ProviderEventCategory(str, enum.Enum):
    DELIVERY = "DELIVERY"   # sent/delivered/read/failed status callbacks
    INBOUND = "INBOUND"     # customer-initiated messages
    OTHER = "OTHER"


class ConversationStatus(str, enum.Enum):
    PENDING = "PENDING"     # unresolved contact — no lead matched yet
    OPEN = "OPEN"
    WAITING = "WAITING"     # waiting for the customer (Phase 8 §31)
    RESOLVED = "RESOLVED"   # issue handled (Phase 8 §31)
    CLOSED = "CLOSED"


class ConversationPriority(str, enum.Enum):
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class MatchStatus(str, enum.Enum):
    """Lead-matching outcome for a conversation (Phase 8 §6).

    MATCH_REVIEW_REQUIRED: several leads share this contact identity — the
    conversation is NEVER silently attached to a possibly-wrong lead."""

    MATCHED = "MATCHED"
    UNMATCHED = "UNMATCHED"
    MATCH_REVIEW_REQUIRED = "MATCH_REVIEW_REQUIRED"


class MessageDirection(str, enum.Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageStatus(str, enum.Enum):
    SENDING = "SENDING"     # queued/accepted locally, not yet confirmed (§26)
    RECEIVED = "RECEIVED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


#: forward-only delivery status order for out-of-order webhook safety (§41)
MESSAGE_STATUS_ORDER = [
    MessageStatus.SENDING.value,
    MessageStatus.SENT.value,
    MessageStatus.RECEIVED.value,  # inbound terminal state, not part of outbound ladder
    MessageStatus.DELIVERED.value,
    MessageStatus.READ.value,
    MessageStatus.FAILED.value,
]


class ProviderCredentials(Base):
    """Encrypted provider secrets.

    `ciphertext` is a Fernet-encrypted JSON blob (key derived from
    QBIT_SECRET_KEY via HKDF-SHA256 — see app/core/crypto.py). Plaintext
    secrets are accepted only as write-only API input, never stored raw,
    never returned by to_public_dict, never logged.
    """

    __tablename__ = "provider_credentials"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: unique vault name; SendingAccount.credential_ref points here
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_version: Mapped[int] = mapped_column(nullable=False, default=CredentialKeyVersion.V1)
    #: NON-secret hints only (e.g. {"token_tail": "…abcd", "updated_via": "ui"})
    hints: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        """Display-safe representation — NEVER includes ciphertext or secrets."""
        return {
            "id": str(self.id),
            "name": self.name,
            "provider": self.provider,
            "key_version": self.key_version,
            "hints": {k: v for k, v in (self.hints or {}).items() if not self._is_secret_hint(k)},
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "last_rotated_at": self.last_rotated_at.isoformat() if self.last_rotated_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @staticmethod
    def _is_secret_hint(key: str) -> bool:
        from app.core.logging import SECRET_KEYS

        return str(key).lower() in SECRET_KEYS


class ProviderEvent(Base):
    """One webhook event exactly once (§20).

    The UNIQUE constraint is the idempotency gate: a provider re-delivering
    the same event id can never create a second row, so delivered/read counts
    can never be double-counted downstream.
    """

    __tablename__ = "provider_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_provider_event_dedupe"),
        Index("ix_provider_events_account_received", "sending_account_id", "received_at"),
        Index("ix_provider_events_message", "provider_message_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    #: provider's unique event/message id; synthetic fallback for payloads
    #: without one (dedupe via sha256 of the normalized payload, hex-trimmed)
    provider_event_id: Mapped[str] = mapped_column(String(300), nullable=False)
    sending_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sending_accounts.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(20), nullable=False, default=ProviderEventCategory.OTHER)
    event_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: normalized, sanitized event body used by downstream processors
    normalized: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: sanitized raw payload subset (secrets stripped) for forensics
    raw_metadata: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now().astimezone(),
    )

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "provider": self.provider,
            "provider_event_id": self.provider_event_id,
            "sending_account_id": str(self.sending_account_id) if self.sending_account_id else None,
            "category": self.category,
            "event_type": self.event_type,
            "provider_message_id": self.provider_message_id,
            "normalized": self.normalized or {},
            "received_at": self.received_at.isoformat() if self.received_at else None,
        }


class Conversation(Base):
    """Thread with one contact over one sending account (§23)."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_account_phone", "sending_account_id", "contact_phone"),
        Index("ix_conversations_lead", "lead_id"),
        Index("ix_conversations_last_message", "last_message_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    sending_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sending_accounts.id", ondelete="SET NULL"), nullable=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    #: provider-side contact identity (e.g. WhatsApp wa_id) — NOT a guess
    external_contact_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: normalized E.164 phone used for lead matching (stable key)
    contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # --- Phase 7: inbound-email conversations (§32) --------------------------
    #: normalized email address used for lead matching on the EMAIL channel
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ConversationStatus.PENDING)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- Phase 8: unified inbox workspace fields (§2) ------------------------
    #: thread subject (EMAIL threads; WhatsApp stays None)
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ConversationPriority.NORMAL
    )
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: RESERVED for a future team model — no team table exists yet (audit §9.1)
    assigned_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unread_count: Mapped[int] = mapped_column(nullable=False, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: lead-matching outcome (MATCHED / UNMATCHED / MATCH_REVIEW_REQUIRED, §6)
    match_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "channel": self.channel,
            "sending_account_id": str(self.sending_account_id) if self.sending_account_id else None,
            "lead_id": str(self.lead_id) if self.lead_id else None,
            "external_contact_id": self.external_contact_id,
            "contact_phone": self.contact_phone,
            "contact_email": self.contact_email,
            "status": self.status,
            "subject": self.subject,
            "priority": self.priority,
            "assigned_user_id": str(self.assigned_user_id) if self.assigned_user_id else None,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "last_inbound_at": self.last_inbound_at.isoformat() if self.last_inbound_at else None,
            "last_outbound_at": self.last_outbound_at.isoformat() if self.last_outbound_at else None,
            "unread_count": self.unread_count or 0,
            "match_status": self.match_status,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Message(Base):
    """One message inside a conversation (§23, extended in Phase 8 §3)."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_provider_message", "provider_message_id"),
        Index("ix_messages_external", "conversation_id", "external_message_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    message_type: Mapped[str] = mapped_column(String(30), nullable=False, default="TEXT")
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=MessageStatus.RECEIVED)
    #: DB column is `metadata` (spec §23); attribute differs because
    #: `metadata` is reserved by SQLAlchemy Declarative
    message_metadata: Mapped[dict] = mapped_column("metadata", PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now().astimezone(),
    )
    # --- Phase 8: display + delivery-timeline fields (§3, §16, §17) ----------
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    #: caller-supplied idempotency id for OUTBOUND replies (client_message_id)
    external_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sender: Mapped[str | None] = mapped_column(String(320), nullable=True)
    recipient: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "direction": self.direction,
            "provider_message_id": self.provider_message_id,
            "external_message_id": self.external_message_id,
            "message_type": self.message_type,
            "body": self.body,
            "subject": self.subject,
            "sender": self.sender,
            "recipient": self.recipient,
            "status": self.status,
            "metadata": self.message_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "failed_at": self.failed_at.isoformat() if self.failed_at else None,
        }


class ConversationNote(Base):
    """Internal team note (Phase 8 §27) — NEVER sent to the customer."""

    __tablename__ = "conversation_notes"
    __table_args__ = (
        Index("ix_conversation_notes_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConversationEvent(Base):
    """Append-only conversation activity timeline (Phase 8 §29, §34).

    Assignment history (previous/new assignee), status/priority changes,
    lead link/unlink, note-added markers and message lifecycle markers all
    land here — history is never overwritten."""

    __tablename__ = "conversation_events"
    __table_args__ = (
        Index("ix_conversation_events_conversation", "conversation_id", "created_at"),
        Index("ix_conversation_events_type", "event_type"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: previous value snapshot (e.g. {"assigned_user_id": ...} for §29)
    previous_value: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: new value snapshot
    new_value: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    message_metadata: Mapped[dict] = mapped_column(
        "metadata", PortableJSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now().astimezone(),
    )

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "event_type": self.event_type,
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "previous_value": self.previous_value or {},
            "new_value": self.new_value or {},
            "metadata": self.message_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class OutboxStatus(str, enum.Enum):
    WAITING = "WAITING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InboxOutboxItem(Base):
    """Outbound reply queue row (Phase 8 §24, §25).

    Replies are NEVER sent from HTTP handlers: the API enqueues a row here
    and the worker loop delivers through the SAME provider abstraction used
    by campaigns. UNIQUE(idempotency_key) makes double-clicks harmless."""

    __tablename__ = "inbox_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_inbox_outbox_idempotency"),
        Index("ix_inbox_outbox_claim", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    sending_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sending_accounts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=OutboxStatus.WAITING)
    #: conversation_id + client_message_id — the duplicate-send gate (§25)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now().astimezone(),
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]
