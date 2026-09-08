"""Phase 11 request/response schemas — teams, invitations, api keys, etc."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import PageMeta as ListMeta


class OrgOut(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    timezone: str
    locale: str
    settings: dict[str, Any] = {}
    created_at: datetime | None = None


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class TeamMemberIn(BaseModel):
    user_id: str
    is_lead: bool = False


class TeamMemberOut(BaseModel):
    user_id: str
    email: str | None = None
    full_name: str | None = None
    is_lead: bool
    joined_at: datetime | None = None


class TeamOut(BaseModel):
    id: str
    organization_id: str
    name: str
    slug: str
    description: str | None = None
    is_active: bool
    member_count: int = 0
    created_at: datetime | None = None


class TeamListOut(BaseModel):
    success: bool = True
    data: list[TeamOut]
    meta: ListMeta


class TeamActionOut(BaseModel):
    success: bool = True
    data: TeamOut


class TeamMembersOut(BaseModel):
    success: bool = True
    data: list[TeamMemberOut]


class InvitationCreate(BaseModel):
    email: EmailStr
    role_codes: list[str] = Field(default_factory=list)
    team_id: str | None = None


class InvitationOut(BaseModel):
    id: str
    organization_id: str
    email: str
    role_codes: list[str] = []
    team_id: str | None = None
    status: str
    expires_at: datetime | None = None
    invited_by: str | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime | None = None


class InvitationCreatedOut(BaseModel):
    """The plaintext invite token is returned EXACTLY ONCE here."""

    success: bool = True
    data: InvitationOut
    invite_token: str
    invite_url: str | None = None


class InvitationListOut(BaseModel):
    success: bool = True
    data: list[InvitationOut]
    meta: ListMeta


class InvitationAcceptIn(BaseModel):
    token: str = Field(min_length=16, max_length=200)
    password: str = Field(min_length=10, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyOut(BaseModel):
    id: str
    organization_id: str
    name: str
    prefix: str
    scopes: list[str] = []
    created_by: str | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    is_live: bool
    created_at: datetime | None = None


class ApiKeyCreatedOut(BaseModel):
    """Plaintext key is returned EXACTLY ONCE at creation."""

    success: bool = True
    data: ApiKeyOut
    api_key: str


class ApiKeyListOut(BaseModel):
    success: bool = True
    data: list[ApiKeyOut]
    meta: ListMeta


class SessionOut(BaseModel):
    id: str
    user_id: str
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    is_live: bool


class SessionListOut(BaseModel):
    success: bool = True
    data: list[SessionOut]
    meta: ListMeta


class RevokeIn(BaseModel):
    reason: str | None = Field(default=None, max_length=100)


class NotificationOut(BaseModel):
    id: str
    type: str
    title: str
    body: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    read_at: datetime | None = None
    created_at: datetime | None = None


class NotificationListOut(BaseModel):
    success: bool = True
    data: list[NotificationOut]
    meta: ListMeta


class AssignmentIn(BaseModel):
    assigned_user_id: str | None = None
    assigned_team_id: str | None = None
    reason: str | None = Field(default=None, max_length=500)


class BulkAssignmentIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    assigned_user_id: str | None = None
    assigned_team_id: str | None = None
    reason: str | None = Field(default=None, max_length=500)


class AssignmentOut(BaseModel):
    success: bool = True
    data: dict[str, Any]


class BulkAssignmentOut(BaseModel):
    success: bool = True
    data: dict[str, Any]  # {"updated": n, "skipped": n, "history": [...]}


class AuditSearchOut(BaseModel):
    success: bool = True
    data: list[dict[str, Any]]
    meta: ListMeta


class OrgSettingsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    locale: str | None = Field(default=None, max_length=20)
    visibility_defaults: dict[str, str] | None = None


class SecuritySettingsOut(BaseModel):
    invitation_expiry_hours: int
    session_ttl_minutes: int
    password_min_length: int
    login_rate_limit_per_min: int


class SecuritySettingsUpdate(BaseModel):
    invitation_expiry_hours: int | None = Field(default=None, ge=1, le=720)
    session_ttl_minutes: int | None = Field(default=None, ge=5, le=43200)


class AdminOverviewOut(BaseModel):
    success: bool = True
    data: dict[str, Any]


class UserActivityOut(BaseModel):
    success: bool = True
    data: dict[str, Any]


class PreferencesIn(BaseModel):
    timezone: str | None = Field(default=None, max_length=64)
    locale: str | None = Field(default=None, max_length=20)
    preferences: dict[str, Any] | None = None


class ConversationOut(BaseModel):
    id: str
    channel: str
    status: str
    lead_id: str | None = None
    sending_account_id: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    assigned_user_id: str | None = None
    assigned_team_id: str | None = None
    last_message_at: datetime | None = None
    created_at: datetime | None = None


class ConversationListOut(BaseModel):
    success: bool = True
    data: list[ConversationOut]
    meta: ListMeta


# --- Phase 11: inbox messages + reply ---------------------------------------------


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    direction: str
    message_type: str
    body: str | None = None
    status: str
    provider_message_id: str | None = None
    metadata: dict[str, Any] = {}
    created_at: datetime | None = None


class MessageListOut(BaseModel):
    success: bool = True
    data: list[MessageOut]
    meta: ListMeta


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class ReplyOut(BaseModel):
    success: bool = True
    data: MessageOut
