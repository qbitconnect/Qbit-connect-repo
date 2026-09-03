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
    CLOSED = "CLOSED"


class MessageDirection(str, enum.Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


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
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Message(Base):
    """One message inside a conversation (§23)."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_provider_message", "provider_message_id"),
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

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "direction": self.direction,
            "provider_message_id": self.provider_message_id,
            "message_type": self.message_type,
            "body": self.body,
            "status": self.status,
            "metadata": self.message_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
