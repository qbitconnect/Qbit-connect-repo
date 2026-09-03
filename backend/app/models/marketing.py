"""Marketing engine models (Phase 7 — multi-channel: EMAIL + WHATSAPP).

Tables (all ADDITIVE vs Phase 0–4; nothing existing is altered destructively):

- SecretVaultEntry          encrypted-at-rest provider credentials (Fernet)
- SendingAccount            multi-channel, multi-account sender registry
- MarketingTemplate         channel templates (subject/HTML/text/variables)
- Campaign                  channel-agnostic campaign aggregate
- CampaignRecipient         per-recipient state machine + idempotency
- CampaignEvent             normalized delivery/tracking event stream
- Suppression               cross-channel do-not-contact registry
- MarketingConsent          explicit opt-in evidence (never fabricated)
- UnsubscribeToken          hashed, unguessable opt-out tokens
- EmailTrackingEvent        optional open/click tracking records
- ProviderEvent             raw webhook store + provider_event_id idempotency
- Conversation / Message    inbox & reply-tracking foundation

Design rules (Phase 7 spec §18, §49, §59):
- CampaignRecipient already carries delivery timestamps — messages/conversations
  never duplicate campaign delivery data.
- Secrets NEVER live in these rows: sending_accounts.credential_ref points at
  the vault; API responses mask credentials (spec §6).
- Recipient status transitions are forward-only (spec: no READ→QUEUED …);
  retries are an explicit FAILED→QUEUED hop while attempts remain.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, timestamp_columns, uuid_pk
from app.models.scrape import PortableJSON


class Channel(str, enum.Enum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    SMS = "SMS"


class AccountStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ERROR = "ERROR"
    DISCONNECTED = "DISCONNECTED"
    SUSPENDED = "SUSPENDED"


class HealthStatus(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class TemplateStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ProviderTemplateStatus(str, enum.Enum):
    """Provider-side approval state (WhatsApp: PENDING/APPROVED/…; email: n/a)."""

    UNSYNCED = "UNSYNCED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class CampaignStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RecipientStatus(str, enum.Enum):
    PENDING = "PENDING"
    ELIGIBLE = "ELIGIBLE"
    SKIPPED = "SKIPPED"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"
    BOUNCED = "BOUNCED"
    COMPLAINED = "COMPLAINED"


#: Forward-only state machine (Phase 7: never move backwards, e.g. READ→QUEUED).
RECIPIENT_TRANSITIONS: dict[str, set[str]] = {
    RecipientStatus.PENDING: {RecipientStatus.ELIGIBLE, RecipientStatus.SKIPPED},
    RecipientStatus.ELIGIBLE: {RecipientStatus.QUEUED, RecipientStatus.SKIPPED},
    RecipientStatus.SKIPPED: set(),
    RecipientStatus.QUEUED: {RecipientStatus.SENDING, RecipientStatus.SKIPPED},
    RecipientStatus.SENDING: {RecipientStatus.SENT, RecipientStatus.FAILED},
    RecipientStatus.SENT: {
        RecipientStatus.DELIVERED,
        RecipientStatus.BOUNCED,
        RecipientStatus.COMPLAINED,  # synchronous complaint (provider feedback)
        RecipientStatus.FAILED,
    },
    RecipientStatus.DELIVERED: {
        RecipientStatus.READ,
        RecipientStatus.COMPLAINED,
        RecipientStatus.BOUNCED,
    },
    RecipientStatus.READ: {RecipientStatus.COMPLAINED},
    # FAILED may retry to QUEUED while attempts remain — the only retry hop.
    RecipientStatus.FAILED: {RecipientStatus.QUEUED},
    RecipientStatus.BOUNCED: set(),
    RecipientStatus.COMPLAINED: set(),
}

#: Events that must never regress a recipient (duplicate webhook protection).
EVENT_RANK: dict[str, int] = {
    RecipientStatus.PENDING: 0,
    RecipientStatus.ELIGIBLE: 1,
    RecipientStatus.QUEUED: 2,
    RecipientStatus.SENDING: 3,
    RecipientStatus.SENT: 4,
    RecipientStatus.DELIVERED: 5,
    RecipientStatus.READ: 6,
    RecipientStatus.FAILED: 4,
    RecipientStatus.BOUNCED: 5,
    RecipientStatus.COMPLAINED: 7,
    RecipientStatus.SKIPPED: 1,
}


def can_transition(current: str, new: str) -> bool:
    """Forward-only check; FAILED→QUEUED is the only upward retry hop."""
    if current == new:
        return False
    allowed = RECIPIENT_TRANSITIONS.get(current, set())
    if new in allowed:
        return True
    return False


def event_advances(current: str, new: str) -> bool:
    """True when applying ``new`` would move the state machine forward.
    Duplicate/out-of-order webhooks (same or lower rank) are ignored."""
    cur_rank = EVENT_RANK.get(current, 0)
    new_rank = EVENT_RANK.get(new, 0)
    if new_rank < cur_rank:
        return False
    return can_transition(current, new) or current == new


class EventType(str, enum.Enum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    BOUNCED = "BOUNCED"
    COMPLAINED = "COMPLAINED"
    FAILED = "FAILED"
    OPENED = "OPENED"
    CLICKED = "CLICKED"
    REPLIED = "REPLIED"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    SKIPPED = "SKIPPED"


class SuppressionReason(str, enum.Enum):
    UNSUBSCRIBED = "UNSUBSCRIBED"
    HARD_BOUNCE = "HARD_BOUNCE"
    COMPLAINT = "COMPLAINT"
    MANUAL = "MANUAL"
    INVALID = "INVALID"


class ConsentStatus(str, enum.Enum):
    OPTED_IN = "OPTED_IN"
    OPTED_OUT = "OPTED_OUT"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------- vault
class SecretVaultEntry(Base):
    """Encrypted-at-rest provider credentials (spec §4–§6).

    Values are Fernet-encrypted with a key derived from QBIT_SECRET_KEY.
    The plaintext NEVER leaves `secrets.py`; this table only stores ciphertext
    plus a non-secret reference label for the owning account.
    """

    __tablename__ = "secret_vault"
    __table_args__ = (UniqueConstraint("ref", name="uq_secret_vault_ref"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]


# ----------------------------------------------------------- sending accounts
class SendingAccount(Base):
    """Multi-channel sender registry (spec §2, §7).

    EMAIL accounts carry sender_name/sender_email/reply_to; WHATSAPP accounts
    carry phone_number_id/business_account_id. Credentials always live in the
    vault behind `credential_ref`. Validation failure never yields ACTIVE.
    """

    __tablename__ = "sending_accounts"
    __table_args__ = (
        Index("ix_sending_accounts_channel_status", "channel", "status"),
        Index("ix_sending_accounts_sender_email", "sender_email"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=AccountStatus.PENDING)
    health_status: Mapped[str] = mapped_column(String(30), nullable=False, default=HealthStatus.UNKNOWN)
    # --- email fields -------------------------------------------------------
    sender_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sender_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    reply_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # --- whatsapp fields ----------------------------------------------------
    phone_number_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # --- shared -------------------------------------------------------------
    #: non-secret configuration only (host/port/security for SMTP, base URLs…)
    config: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: provider capability flags: supports_templates/media/inbound/webhooks…
    capabilities: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: vault reference — never the secret itself
    credential_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        """API-safe projection (spec §6): no secrets, vault ref masked."""
        return {
            "id": str(self.id),
            "name": self.name,
            "channel": self.channel,
            "provider": self.provider,
            "status": self.status,
            "health_status": self.health_status,
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "reply_to": self.reply_to,
            "phone_number_id": self.phone_number_id,
            "business_account_id": self.business_account_id,
            "config": self.config or {},
            "capabilities": self.capabilities or {},
            "credential_ref": "••••" + (self.credential_ref or "")[-4:] if self.credential_ref else None,
            "status_message": self.status_message,
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "last_health_message": self.last_health_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ----------------------------------------------------------------- templates
class MarketingTemplate(Base):
    """Channel template (spec §10–§12).

    EMAIL: subject + html_body + text_body, {{variable}} placeholders from a
    whitelist. WHATSAPP: body + provider_template_id, provider approval state
    is mirrored in provider_status — campaigns may only use APPROVED templates.
    """

    __tablename__ = "marketing_templates"
    __table_args__ = (
        Index("ix_marketing_templates_channel_status", "channel", "status"),
        Index("ix_marketing_templates_provider_template", "provider_template_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=TemplateStatus.DRAFT)
    provider_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=ProviderTemplateStatus.UNSYNCED
    )
    provider_template_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="en")
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # --- email --------------------------------------------------------------
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    html_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- whatsapp -----------------------------------------------------------
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- shared -------------------------------------------------------------
    variables: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "channel": self.channel,
            "status": self.status,
            "provider_status": self.provider_status,
            "provider_template_id": self.provider_template_id,
            "language": self.language,
            "category": self.category,
            "subject": self.subject,
            "has_html": bool(self.html_body),
            "has_text": bool(self.text_body),
            "body": self.body,
            "variables": self.variables or [],
            "metadata": self.metadata_json or {},
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ----------------------------------------------------------------- campaigns
class Campaign(Base):
    """Channel-agnostic campaign aggregate (spec §17, §34, §41, §42).

    Counters are maintained from real CampaignEvents only — no estimates.
    ``message_version`` participates in the send idempotency key.
    """

    __tablename__ = "campaigns"
    __table_args__ = (
        Index("ix_campaigns_channel_status", "channel", "status"),
        Index("ix_campaigns_created_at", "created_at"),
        Index("ix_campaigns_template", "template_id"),
        Index("ix_campaigns_account", "sending_account_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=CampaignStatus.DRAFT)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("marketing_templates.id"), nullable=True
    )
    sending_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sending_accounts.id"), nullable=True
    )
    #: audience filter spec applied to leads (same grammar as saved views)
    audience: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    schedule_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: operational rate control (spec §42) — throttling only
    rate_config: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    track_opens: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    track_clicks: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    message_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # --- counters (real events only) ----------------------------------------
    audience_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounced_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    complained_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replied_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unsubscribed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # --- lifecycle ----------------------------------------------------------
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_result: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "channel": self.channel,
            "status": self.status,
            "template_id": str(self.template_id) if self.template_id else None,
            "sending_account_id": str(self.sending_account_id) if self.sending_account_id else None,
            "audience": self.audience or {},
            "schedule_at": self.schedule_at.isoformat() if self.schedule_at else None,
            "rate_config": self.rate_config or {},
            "track_opens": bool(self.track_opens),
            "track_clicks": bool(self.track_clicks),
            "message_version": self.message_version,
            "counters": {
                "audience_total": self.audience_total,
                "eligible": self.eligible_count,
                "skipped": self.skipped_count,
                "queued": self.queued_count,
                "sent": self.sent_count,
                "delivered": self.delivered_count,
                "bounced": self.bounced_count,
                "complained": self.complained_count,
                "opened": self.opened_count,
                "clicked": self.clicked_count,
                "replied": self.replied_count,
                "unsubscribed": self.unsubscribed_count,
                "failed": self.failed_count,
            },
            "error_code": self.error_code,
            "error": self.error,
            "validation_result": self.validation_result or {},
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CampaignRecipient(Base):
    """Per-recipient delivery state (spec §18–§20).

    ``idempotency_key`` = campaign_id + recipient_id + message_version and is
    UNIQUE — worker restarts / queue duplication can never double-send.
    """

    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_recipient_idempotency"),
        Index("ix_recipients_campaign_status", "campaign_id", "status"),
        Index("ix_recipients_campaign_address", "campaign_id", "address_norm"),
        Index("ix_recipients_provider_message", "provider_message_id"),
        Index("ix_recipients_next_retry", "next_retry_at"),
        Index("ix_recipients_lead", "lead_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    address_norm: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=RecipientStatus.PENDING)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    message_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: personalized variable snapshot (spec §18) — never secrets
    variables: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # --- delivery timestamps (spec §18) --------------------------------------
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    complained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "lead_id": str(self.lead_id) if self.lead_id else None,
            "address": self.address,
            "address_norm": self.address_norm,
            "status": self.status,
            "reason": self.reason,
            "message_version": self.message_version,
            "provider_message_id": self.provider_message_id,
            "attempts": self.attempts,
            "last_error_code": self.last_error_code,
            "last_error": self.last_error,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "clicked_at": self.clicked_at.isoformat() if self.clicked_at else None,
            "bounced_at": self.bounced_at.isoformat() if self.bounced_at else None,
            "complained_at": self.complained_at.isoformat() if self.complained_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CampaignEvent(Base):
    """Normalized event stream (spec §26). One row per real observed event."""

    __tablename__ = "campaign_events"
    __table_args__ = (
        Index("ix_campaign_events_campaign_created", "campaign_id", "created_at"),
        Index("ix_campaign_events_recipient", "recipient_id"),
        Index("ix_campaign_events_type", "event_type"),
        Index("ix_campaign_events_provider_message", "provider_message_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaign_recipients.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payload: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = timestamp_columns()[0]


# --------------------------------------------------------------- suppression
class Suppression(Base):
    """Cross-channel do-not-contact registry (spec §14, §25, §47).

    address_norm is the dedup/suppression key. Unsubscribes, hard bounces and
    complaints create rows here; eligibility consults it before queueing.
    """

    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint("channel", "address_norm", name="uq_suppression_channel_address"),
        Index("ix_suppressions_channel_reason", "channel", "reason"),
        Index("ix_suppressions_lead", "lead_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    address_norm: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "channel": self.channel,
            "address": self.address,
            "address_norm": self.address_norm,
            "reason": self.reason,
            "source": self.source,
            "lead_id": str(self.lead_id) if self.lead_id else None,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class MarketingConsent(Base):
    """Explicit opt-in evidence per (channel, address) — spec §15.

    A scraped address is NOT consent. Absence of a row means UNKNOWN, never
    implied opt-in. Consent is written only from real evidence (imports with
    consent flags, user actions); it is never fabricated by the system.
    """

    __tablename__ = "marketing_consents"
    __table_args__ = (
        UniqueConstraint("channel", "address_norm", name="uq_consent_channel_address"),
        Index("ix_consents_lead", "lead_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    address_norm: Mapped[str] = mapped_column(String(320), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    opt_in_status: Mapped[str] = mapped_column(String(30), nullable=False, default=ConsentStatus.UNKNOWN)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    evidence: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]


# --------------------------------------------------------- unsubscribe tokens
class UnsubscribeToken(Base):
    """Hashed opt-out tokens (spec §13, §51).

    Only the SHA-256 hash is stored — the raw token is shown once at
    generation time inside email bodies. No lead/campaign ids are encoded in
    the token itself (unguessable random 32 bytes).
    """

    __tablename__ = "unsubscribe_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_unsubscribe_token_hash"),
        Index("ix_unsubscribe_tokens_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="EMAIL")
    lead_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    address_norm: Mapped[str | None] = mapped_column(String(320), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]


class EmailTrackingEvent(Base):
    """Optional open/click tracking (spec §29–§31). Never mandatory."""

    __tablename__ = "email_tracking_events"
    __table_args__ = (
        Index("ix_tracking_events_recipient", "recipient_id", "event_type"),
        Index("ix_tracking_events_campaign", "campaign_id", "event_type"),
        Index("ix_tracking_events_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaign_recipients.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)  # OPEN | CLICK
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]


# ------------------------------------------------------------- webhook store
class ProviderEvent(Base):
    """Raw provider webhook events + idempotency (spec §27, §28).

    (channel, provider, provider_event_id) is UNIQUE — duplicate webhooks are
    recorded as DUPLICATE and never re-apply to recipients/analytics.
    """

    __tablename__ = "provider_events"
    __table_args__ = (
        UniqueConstraint("channel", "provider", "provider_event_id", name="uq_provider_event_once"),
        Index("ix_provider_events_status", "status"),
        Index("ix_provider_events_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(300), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RECEIVED")
    payload: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime] = timestamp_columns()[0]
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ------------------------------------------------------------ inbox foundation
class Conversation(Base):
    """Inbox foundation (spec §32): one conversation per (channel, account,
    external contact). Replies attach here — never a fake inbox."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "channel", "sending_account_id", "external_address_norm",
            name="uq_conversation_channel_contact",
        ),
        Index("ix_conversations_last_message", "last_message_at"),
        Index("ix_conversations_lead", "lead_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    sending_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sending_accounts.id"), nullable=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    #: normalized address of the external contact (email/phone)
    external_address: Mapped[str] = mapped_column(String(320), nullable=False)
    external_address_norm: Mapped[str] = mapped_column(String(320), nullable=False)
    external_contact_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: display name from inbound mail — stored as-is, never merged into leads silently
    external_contact_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]


class Message(Base):
    """Inbound/outbound message rows (spec §18, §32, §33).

    For email replies, the threading headers (Message-ID / In-Reply-To /
    References) are preserved verbatim in ``headers`` — threading relies on
    those, never on subject matching alone.
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_provider_message", "provider_message_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # INBOUND|OUTBOUND
    provider_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    message_type: Mapped[str] = mapped_column(String(30), nullable=False, default="TEXT")
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="RECEIVED")
    metadata_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "direction": self.direction,
            "provider_message_id": self.provider_message_id,
            "message_type": self.message_type,
            "subject": self.subject,
            "body_text": self.body_text,
            "status": self.status,
            "headers": self.headers or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
