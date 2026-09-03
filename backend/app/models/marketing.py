"""Phase 5 marketing foundation tables (models only — schema logic lives in
services/marketing).

Tables:
- Campaign            reusable campaign definition + lifecycle status
- CampaignRecipient   per-lead snapshot row created at launch (reproducibility)
- CampaignEvent       immutable event stream (never overwrite history)
- CampaignTemplate    safe {{variable}} templates, per channel
- SendingAccount      multi-sender abstraction (secrets never serialized)
- SuppressionEntry    global do-not-contact list (EMAIL/PHONE/LEAD/CHANNEL)
- OptOutRecord        unsubscribe/opt-out evidence (feeds suppression checks)
- CampaignQueueItem   DB-backed send queue with idempotency key + retries

Design rules (Phase 5 brief):
- additive-only vs Phase 1-4; PortableJSON so SQLite tests and PG prod match
- provider-specific details stay in metadata columns, never hard-coded columns
- status vocabularies are platform-level (never provider-specific states)
- secrets are NEVER stored in plaintext columns here; providers keep them in
  their own configuration storage and must not leak them through to_public_dict
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
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
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    SMS = "SMS"


class CampaignStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    SCHEDULED = "SCHEDULED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class RecipientStatus(str, enum.Enum):
    PENDING = "PENDING"
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    REPLIED = "REPLIED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class TemplateStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class TemplateOrigin(str, enum.Enum):
    LOCAL = "LOCAL"          # authored inside QBIT Connect
    PROVIDER = "PROVIDER"    # synchronized from the provider (e.g. WhatsApp WABA)


class ProviderTemplateStatus(str, enum.Enum):
    """Provider-side template approval states (WhatsApp Business Platform, §8).

    Platform status stays DRAFT/ACTIVE/ARCHIVED; provider_status mirrors the
    provider's approval lifecycle and gates campaign usage (§10)."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class AccountStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ERROR = "ERROR"
    SUSPENDED = "SUSPENDED"
    PENDING = "PENDING"
    DISCONNECTED = "DISCONNECTED"


class AccountHealth(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class QueueStatus(str, enum.Enum):
    WAITING = "WAITING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY = "RETRY"
    CANCELLED = "CANCELLED"


class SuppressionType(str, enum.Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    LEAD = "LEAD"
    CHANNEL = "CHANNEL"


class SuppressionReason(str, enum.Enum):
    UNSUBSCRIBED = "UNSUBSCRIBED"
    BOUNCED = "BOUNCED"
    COMPLAINT = "COMPLAINT"
    BLOCKED = "BLOCKED"
    MANUAL = "MANUAL"
    PROVIDER_RESTRICTION = "PROVIDER_RESTRICTION"


#: campaign event types (brief §8) — the record is immutable, append-only
class EventType:
    CAMPAIGN_CREATED = "CAMPAIGN_CREATED"
    CAMPAIGN_VALIDATED = "CAMPAIGN_VALIDATED"
    CAMPAIGN_QUEUED = "CAMPAIGN_QUEUED"
    CAMPAIGN_STARTED = "CAMPAIGN_STARTED"
    CAMPAIGN_PAUSED = "CAMPAIGN_PAUSED"
    CAMPAIGN_RESUMED = "CAMPAIGN_RESUMED"
    CAMPAIGN_CANCELLED = "CAMPAIGN_CANCELLED"
    CAMPAIGN_COMPLETED = "CAMPAIGN_COMPLETED"
    CAMPAIGN_FAILED = "CAMPAIGN_FAILED"
    RECIPIENT_ADDED = "RECIPIENT_ADDED"
    RECIPIENT_SKIPPED = "RECIPIENT_SKIPPED"
    MESSAGE_QUEUED = "MESSAGE_QUEUED"
    MESSAGE_SENT = "MESSAGE_SENT"
    MESSAGE_DELIVERED = "MESSAGE_DELIVERED"
    MESSAGE_READ = "MESSAGE_READ"
    MESSAGE_REPLIED = "MESSAGE_REPLIED"
    MESSAGE_FAILED = "MESSAGE_FAILED"
    # --- Phase 7: email delivery-event vocabulary (§24–§26, §29, §30) -------
    MESSAGE_BOUNCED = "MESSAGE_BOUNCED"          # hard/soft bounce (metadata)
    MESSAGE_COMPLAINED = "MESSAGE_COMPLAINED"    # spam complaint
    MESSAGE_OPENED = "MESSAGE_OPENED"            # tracking pixel (opt-in)
    MESSAGE_CLICKED = "MESSAGE_CLICKED"          # link click (opt-in)
    MESSAGE_UNSUBSCRIBED = "MESSAGE_UNSUBSCRIBED"  # real unsubscribe link used

    ALL = (
        CAMPAIGN_CREATED, CAMPAIGN_VALIDATED, CAMPAIGN_QUEUED, CAMPAIGN_STARTED,
        CAMPAIGN_PAUSED, CAMPAIGN_RESUMED, CAMPAIGN_CANCELLED, CAMPAIGN_COMPLETED,
        CAMPAIGN_FAILED, RECIPIENT_ADDED, RECIPIENT_SKIPPED, MESSAGE_QUEUED,
        MESSAGE_SENT, MESSAGE_DELIVERED, MESSAGE_READ, MESSAGE_REPLIED,
        MESSAGE_FAILED, MESSAGE_BOUNCED, MESSAGE_COMPLAINED, MESSAGE_OPENED,
        MESSAGE_CLICKED, MESSAGE_UNSUBSCRIBED,
    )


class CampaignTemplate(Base):
    __tablename__ = "campaign_templates"
    __table_args__ = (
        Index("ix_campaign_templates_channel_status", "channel", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TemplateStatus.DRAFT)
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="en")
    #: declared template variables, e.g. ["first_name", "business_name"]
    variables: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)
    # --- Phase 6: provider (WhatsApp) template fields -----------------------
    #: LOCAL = authored in QBIT; PROVIDER = synced from the provider (§9)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default=TemplateOrigin.LOCAL)
    #: provider-side template id (stays available even after renames, §9)
    provider_template_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: provider approval status: PENDING/APPROVED/REJECTED/PAUSED/DISABLED (§8)
    provider_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: provider category, e.g. MARKETING / UTILITY / AUTHENTICATION
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    #: normalized provider components (header/body placeholder spec)
    components: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    #: sending account this provider template belongs to (multi-account, §3)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sending_accounts.id", ondelete="SET NULL"), nullable=True
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    @property
    def is_provider_approved(self) -> bool:
        return (
            self.origin == TemplateOrigin.PROVIDER.value
            and self.provider_status == ProviderTemplateStatus.APPROVED.value
        )

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "channel": self.channel,
            "subject": self.subject,
            "body": self.body,
            "status": self.status,
            "language": self.language,
            "variables": self.variables or [],
            "origin": self.origin,
            "provider_template_id": self.provider_template_id,
            "provider_status": self.provider_status,
            "category": self.category,
            "components": self.components or {},
            "account_id": str(self.account_id) if self.account_id else None,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "rejected_reason": self.rejected_reason,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SendingAccount(Base):
    """One sender identity (e.g. a WhatsApp Business number, an email account).

    `config_metadata` holds NON-SECRET provider configuration only. Credentials
    (API keys/tokens) live in the encrypted provider_credentials vault and are
    referenced by name in `credential_ref` — never embedded in any column.
    to_public_dict() only exposes display-safe fields — never raw config.
    """

    __tablename__ = "sending_accounts"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    #: provider id inside the provider registry, e.g. "mock", "whatsapp_cloud"
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    identifier: Mapped[str] = mapped_column(String(300), nullable=False)
    display_identifier: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: encrypted credential reference (provider_credentials.name) — never a secret itself
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: provider-side identifiers (NON-secret, queryable; Phase 6 §3 multi-account fields)
    phone_number_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=AccountStatus.PENDING)
    capabilities: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    config_metadata: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    health_status: Mapped[str] = mapped_column(String(20), nullable=False, default=AccountHealth.UNKNOWN)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "channel": self.channel,
            "provider": self.provider,
            "identifier": self.identifier,
            "display_identifier": self.display_identifier or self.identifier,
            "phone_number_id": self.phone_number_id,
            "business_account_id": self.business_account_id,
            "has_credentials": bool(self.credential_ref),
            "status": self.status,
            "capabilities": self.capabilities or {},
            "health_status": self.health_status,
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        Index("ix_campaigns_status_created", "status", "created_at"),
        Index("ix_campaigns_channel", "channel"),
        Index("ix_campaigns_scheduled_at", "scheduled_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CampaignStatus.DRAFT)
    #: audience definition JSON — {type: saved_view|filters|tags|selected, ...}
    audience_definition: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaign_templates.id"), nullable=True
    )
    sending_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sending_accounts.id"), nullable=True
    )
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False, default="SEND_NOW")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: last validation report cache (read-only for the UI)
    validation_report: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    # --- Phase 7: campaign-level settings (§31, §37) -------------------------
    #: track_opens / track_clicks / append_unsubscribe_footer / company fields
    campaign_metadata: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "channel": self.channel,
            "status": self.status,
            "audience_definition": self.audience_definition or {},
            "template_id": str(self.template_id) if self.template_id else None,
            "sending_account_id": str(self.sending_account_id) if self.sending_account_id else None,
            "schedule_type": self.schedule_type,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "timezone": self.timezone,
            "validation_report": self.validation_report or {},
            "created_by": str(self.created_by) if self.created_by else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CampaignRecipient(Base):
    """Immutable-at-launch audience snapshot row (brief §7, §12)."""

    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id", name="uq_campaign_recipient_lead"),
        Index("ix_campaign_recipients_campaign_status", "campaign_id", "status"),
        Index("ix_campaign_recipients_lead", "lead_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    recipient_address: Mapped[str] = mapped_column(String(320), nullable=False)
    recipient_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=RecipientStatus.PENDING)
    eligibility_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- Phase 7: email-specific timestamps + tracking key (§18, §29, §30) ---
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    complained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: unguessable per-recipient tracking key (tracking URLs carry this key,
    #: never a raw database id — §29)
    tracking_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "lead_id": str(self.lead_id) if self.lead_id else None,
            "recipient_address": self.recipient_address,
            "recipient_name": self.recipient_name,
            "status": self.status,
            "eligibility_status": self.eligibility_status,
            "skip_reason": self.skip_reason,
            "provider_message_id": self.provider_message_id,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "replied_at": self.replied_at.isoformat() if self.replied_at else None,
            "failed_at": self.failed_at.isoformat() if self.failed_at else None,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "clicked_at": self.clicked_at.isoformat() if self.clicked_at else None,
            "bounced_at": self.bounced_at.isoformat() if self.bounced_at else None,
            "complained_at": self.complained_at.isoformat() if self.complained_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CampaignEvent(Base):
    """Append-only event stream (brief §8). Never updated, never deleted."""

    __tablename__ = "campaign_events"
    __table_args__ = (
        Index("ix_campaign_events_campaign_created", "campaign_id", "created_at"),
        Index("ix_campaign_events_recipient", "recipient_id"),
        Index("ix_campaign_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaign_recipients.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payload_metadata: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "recipient_id": str(self.recipient_id) if self.recipient_id else None,
            "event_type": self.event_type,
            "provider": self.provider,
            "provider_event_id": self.provider_event_id,
            "metadata": self.payload_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SuppressionEntry(Base):
    """Global suppression list (brief §14).

    `channel_key` normalizes NULL channel to '' so the uniqueness constraint
    works identically on PostgreSQL and SQLite (NULLs never conflict in SQL
    unique constraints, so a raw NULL column would allow duplicate rows).
    """

    __tablename__ = "suppression_entries"
    __table_args__ = (
        UniqueConstraint("type", "address", "channel_key", name="uq_suppression_entry"),
        Index("ix_suppression_address", "address"),
        Index("ix_suppression_channel", "channel_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # EMAIL/PHONE/LEAD/CHANNEL
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    channel_key: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "type": self.type,
            "address": self.address,
            "channel": self.channel,
            "reason": self.reason,
            "source": self.source,
            "lead_id": str(self.lead_id) if self.lead_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class OptOutRecord(Base):
    """Unsubscribe / opt-out evidence (brief §15). Never silently re-enabled."""

    __tablename__ = "opt_out_records"
    __table_args__ = (
        UniqueConstraint("channel_key", "address", name="uq_opt_out_address_channel"),
        Index("ix_opt_out_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    channel_key: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(40), nullable=False, default="UNSUBSCRIBED")
    source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id) if self.lead_id else None,
            "address": self.address,
            "channel": self.channel,
            "reason": self.reason,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CampaignQueueItem(Base):
    """DB-backed send queue row (brief §17, §18).

    Idempotency key (brief §20): UNIQUE (campaign_id, recipient_id,
    message_version) — a worker restart or duplicate delivery can never create
    a second send for the same recipient/message version.
    """

    __tablename__ = "campaign_queue"
    __table_args__ = (
        UniqueConstraint("campaign_id", "recipient_id", "message_version", name="uq_queue_idempotency"),
        Index("ix_campaign_queue_status_available", "status", "available_at"),
        Index("ix_campaign_queue_campaign", "campaign_id"),
        Index("ix_campaign_queue_account_status", "sending_account_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("campaign_recipients.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    sending_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sending_accounts.id", ondelete="SET NULL"), nullable=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=QueueStatus.WAITING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: bumped when a campaign is re-launched after failure — changes the
    #: idempotency key deliberately, never silently
    message_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "recipient_id": str(self.recipient_id),
            "channel": self.channel,
            "sending_account_id": str(self.sending_account_id) if self.sending_account_id else None,
            "priority": self.priority,
            "status": self.status,
            "attempts": self.attempts,
            "message_version": self.message_version,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
