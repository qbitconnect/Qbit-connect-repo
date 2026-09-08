"""Phase 11 — Team / Admin / Enterprise models.

Everything here is ADDITIVE: existing tables are extended with nullable columns
(migration 0007 backfills the default organization) so an existing installation
keeps working unchanged after upgrade.

Tenancy model:
    Organization 1─N OrganizationMember N User
    Organization 1─N Team 1─N TeamMember N User
    Tenant-scoped rows carry organization_id (+ optional team_id/owner_id).

Security model:
    Invitation   — one-time, hashed-token signup into an organization
    UserSession  — server-side session registry keyed by JWT `jti` (revocable)
    ApiKey       — hashed bearer credentials with allowlisted scopes
    Notification — in-app notifications (email/push channels come later)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, timestamp_columns, uuid_pk

PortableJSON = JSON().with_variant(JSONB(), "postgresql")


def utc_aware(dt: datetime | None) -> datetime | None:
    """Treat naive datetimes (SQLite storage) as UTC for safe comparisons."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- enums ---------------------------------------------------------------------


class OrganizationStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    ARCHIVED = "ARCHIVED"


class MemberStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    # INVITED is tracked in the invitations table; members become ACTIVE on accept.
    REMOVED = "REMOVED"


class VisibilityScope(str, enum.Enum):
    """Resource visibility scopes (Phase 11 §9). Enforced backend-side only."""

    ALL = "ALL"
    TEAM = "TEAM"
    ASSIGNED_ONLY = "ASSIGNED_ONLY"
    OWNED_ONLY = "OWNED_ONLY"


class UserStatus(str, enum.Enum):
    INVITED = "INVITED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DEACTIVATED = "DEACTIVATED"


class InvitationStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class ConnectionAccessScope(str, enum.Enum):
    ORGANIZATION = "ORGANIZATION"
    TEAM = "TEAM"
    RESTRICTED = "RESTRICTED"


class NotificationType(str, enum.Enum):
    INVITATION = "INVITATION"
    ASSIGNMENT = "ASSIGNMENT"
    CAMPAIGN_COMPLETED = "CAMPAIGN_COMPLETED"
    CAMPAIGN_FAILED = "CAMPAIGN_FAILED"
    SCRAPE_COMPLETED = "SCRAPE_COMPLETED"
    SCRAPE_FAILED = "SCRAPE_FAILED"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"
    CONNECTION_HEALTH = "CONNECTION_HEALTH"
    SECURITY = "SECURITY"


# --- organization --------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OrganizationStatus.ACTIVE, index=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    locale: Mapped[str] = mapped_column(String(20), nullable=False, default="en")
    #: per-role visibility defaults, e.g. {"OPERATOR": "ASSIGNED_ONLY"}.
    #: Empty/missing keys fall back to role defaults; shipped defaults preserve
    #: pre-Phase-11 behavior (ALL) until an admin tightens them.
    settings_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "status": self.status,
            "timezone": self.timezone,
            "locale": self.locale,
            "settings": self.settings_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        Index("ix_org_members_org_user", "organization_id", "user_id", unique=True),
        Index("ix_org_members_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MemberStatus.ACTIVE, index=True
    )
    #: per-member visibility override; NULL = use role default / org default
    visibility_scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        Index("ix_teams_org", "organization_id"),
        Index("ix_teams_org_name", "organization_id", "name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: TEAM visibility for resources shared with the team
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    updated_at: Mapped[datetime] = timestamp_columns()[1]


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        Index("ix_team_members_team_user", "team_id", "user_id", unique=True),
        Index("ix_team_members_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    is_lead: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = timestamp_columns()[0]


# --- invitations ---------------------------------------------------------------


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        Index("ix_invitations_org", "organization_id"),
        Index("ix_invitations_email", "email"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: role CODES (global roles) to grant on acceptance
    role_codes: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    #: SHA-256 of the one-time token — the plaintext token is NEVER stored
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    @property
    def status(self) -> str:
        if self.revoked_at is not None:
            return InvitationStatus.REVOKED.value
        if self.accepted_at is not None:
            return InvitationStatus.ACCEPTED.value
        if self.expires_at is not None and utc_aware(self.expires_at) <= utc_now():
            return InvitationStatus.EXPIRED.value
        return InvitationStatus.PENDING.value

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "organization_id": str(self.organization_id),
            "email": self.email,
            "role_codes": self.role_codes or [],
            "team_id": str(self.team_id) if self.team_id else None,
            "status": self.status,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "invited_by": str(self.invited_by) if self.invited_by else None,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
    # NOTE: token_hash is deliberately NOT in to_public_dict.


# --- sessions ------------------------------------------------------------------


class UserSession(Base):
    """Server-side session registry for issued access tokens (revocable).

    `get_current_user` validates the JWT `jti` against this table. Tokens issued
    before Phase 11 have no row — they are governed by
    `users.tokens_revoked_before` (iat check), so existing users keep working.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_sessions_last_seen", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and utc_aware(self.expires_at) <= utc_now():
            return False
        return True

    def to_public_dict(self) -> dict:
        # Never exposes the jti itself.
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "ip_address": self.ip_address,
            "user_agent": (self.user_agent or "")[:200],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_reason": self.revoked_reason,
            "is_live": self.is_live,
        }


# --- API keys -------------------------------------------------------------------

API_KEY_SCOPES: tuple[str, ...] = (
    "leads.read",
    "leads.write",
    "campaigns.read",
    "campaigns.write",
    "analytics.read",
    "scraping.run",
    "files.read",
    "connections.read",
)

API_KEY_PREFIX = "qbit"


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_org", "organization_id"),
        Index("ix_api_keys_prefix", "prefix", unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: short public prefix used for lookup ("qbit_ab12cd34"); secret never stored
    prefix: Mapped[str] = mapped_column(String(40), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: allowlisted scopes (API_KEY_SCOPES) — never auto-admin
    scopes: Mapped[list] = mapped_column(PortableJSON, nullable=False, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and utc_aware(self.expires_at) <= utc_now():
            return False
        return True

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "organization_id": str(self.organization_id),
            "name": self.name,
            "prefix": self.prefix,
            "scopes": self.scopes or [],
            "created_by": str(self.created_by) if self.created_by else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "is_live": self.is_live,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# --- notifications & preferences -------------------------------------------------


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_user_unread", "user_id", "read_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "type": self.type,
            "title": self.title,
            "body": self.body,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserPreference(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(20), nullable=True)
    preferences_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = timestamp_columns()[1]


# --- assignment history -----------------------------------------------------------


class LeadAssignmentHistory(Base):
    __tablename__ = "lead_assignment_history"
    __table_args__ = (
        Index("ix_lead_assign_history_lead", "lead_id", "created_at"),
        Index("ix_lead_assign_history_user", "assigned_user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    previous_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    previous_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id),
            "previous_user_id": str(self.previous_user_id) if self.previous_user_id else None,
            "previous_team_id": str(self.previous_team_id) if self.previous_team_id else None,
            "assigned_user_id": str(self.assigned_user_id) if self.assigned_user_id else None,
            "assigned_team_id": str(self.assigned_team_id) if self.assigned_team_id else None,
            "changed_by": str(self.changed_by) if self.changed_by else None,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ConversationAssignmentHistory(Base):
    __tablename__ = "conversation_assignment_history"
    __table_args__ = (
        Index("ix_conv_assign_history_conv", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    previous_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    previous_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = timestamp_columns()[0]

    def to_public_dict(self) -> dict:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "previous_user_id": str(self.previous_user_id) if self.previous_user_id else None,
            "previous_team_id": str(self.previous_team_id) if self.previous_team_id else None,
            "assigned_user_id": str(self.assigned_user_id) if self.assigned_user_id else None,
            "assigned_team_id": str(self.assigned_team_id) if self.assigned_team_id else None,
            "changed_by": str(self.changed_by) if self.changed_by else None,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
