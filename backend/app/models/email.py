"""Phase 7 email tables.

Tables:
- EmailTrackingEvent     open/click evidence (§29, §30) — one row per event,
                         keyed to the campaign recipient, never exposing
                         internal ids in the URLs that produced the event
- EmailUnsubscribeToken  one-time opt-out tokens (§13, §50, §51) — the RAW
                         token never touches the database; only its SHA-256
                         hash is stored, so a database leak cannot unsubscribe
                         anyone or forge links

Design rules:
- additive-only vs earlier phases; PortableJSON so SQLite tests == PG prod
- no secrets, no raw recipient email beyond the address itself (needed to
  resolve the opt-out), no tracking of anything the operator did not enable
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid

from app.db.base import Base, timestamp_columns, uuid_pk
from app.models.scrape import PortableJSON
from sqlalchemy.orm import Mapped, mapped_column


class TrackingEventType(str, enum.Enum):
    OPEN = "OPEN"
    CLICK = "CLICK"


class EmailTrackingEvent(Base):
    """One email open/click event (§29, §30). Append-only, never updated."""

    __tablename__ = "email_tracking_events"
    __table_args__ = (
        Index("ix_email_tracking_recipient_type", "recipient_id", "event_type"),
        Index("ix_email_tracking_message", "message_id"),
        Index("ix_email_tracking_created", "created_at"),
        Index("ix_email_tracking_campaign_created", "campaign_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaign_recipients.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)  # OPEN / CLICK
    #: for CLICK events: the original destination (http/https only, §30)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: the CampaignRecipient.provider_message_id of the email that was opened
    message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now().astimezone(),
    )
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "recipient_id": str(self.recipient_id),
            "event_type": self.event_type,
            "url": self.url,
            "message_id": self.message_id,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class EmailUnsubscribeToken(Base):
    """One-time unsubscribe token (§13, §50, §51).

    The database stores ONLY the SHA-256 hash of the token. The raw token
    lives exclusively inside the email link; it encodes nothing predictable
    (no lead_id, no email, no campaign_id) and is unguessable
    (secrets.token_urlsafe(32)).
    """

    __tablename__ = "email_unsubscribe_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_email_unsub_token_hash"),
        Index("ix_email_unsub_created", "created_at"),
        Index("ix_email_unsub_recipient", "recipient_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    #: SHA-256 hex digest of the raw token (§51 — hash, never the token)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaign_recipients.id", ondelete="SET NULL"),
        nullable=True,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    #: normalized email address the opt-out applies to
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="EMAIL")
    created_at: Mapped[datetime] = timestamp_columns()[0]
    #: set when the link is used; reuse shows the same confirmation (idempotent)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_public_dict(self) -> dict:
        """Display-safe — NEVER includes the token hash."""
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id) if self.campaign_id else None,
            "recipient_id": str(self.recipient_id) if self.recipient_id else None,
            "address_masked": self._mask(self.address),
            "channel": self.channel,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "consumed_at": self.consumed_at.isoformat() if self.consumed_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @staticmethod
    def _mask(address: str) -> str:
        local, _, domain = address.partition("@")
        if not domain:
            return "•••"
        head = local[:2] if len(local) >= 2 else local
        return f"{head}•••@{domain}"
