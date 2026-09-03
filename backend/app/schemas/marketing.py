"""Marketing API schemas (Phase 7 §45, §46, §47)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ActionOut(BaseModel):
    """Unified action envelope: {success, data}."""

    model_config = ConfigDict(populate_by_name=True)
    success: bool = True
    data: dict


class ListOut(BaseModel):
    success: bool = True
    data: dict


# ---------------------------------------------------------------- accounts
class EmailAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: mock_email is TEST ONLY — the service layer + registry refuse it in production
    provider: str = Field(pattern="^(smtp|email_api|mock_email)$")
    sender_name: str | None = Field(default=None, max_length=200)
    sender_email: str
    reply_to: str | None = None
    config: dict = Field(default_factory=dict)
    credentials: dict = Field(default_factory=dict)


class WhatsAppAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: mock_whatsapp is TEST ONLY — the service layer + registry refuse it in production
    provider: str = Field(default="whatsapp_cloud", pattern="^(whatsapp_cloud|mock_whatsapp)$")
    phone_number_id: str
    business_account_id: str | None = None
    config: dict = Field(default_factory=dict)
    credentials: dict = Field(default_factory=dict)


class EmailAccountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    sender_name: str | None = None
    sender_email: str | None = None
    reply_to: str | None = None
    config: dict | None = None
    credentials: dict | None = None


class WhatsAppAccountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    phone_number_id: str | None = None
    business_account_id: str | None = None
    config: dict | None = None
    credentials: dict | None = None


class AccountStatusOut(BaseModel):
    success: bool = True
    data: dict


# ---------------------------------------------------------------- templates
class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    channel: str = Field(pattern="^(EMAIL|WHATSAPP)$")
    subject: str | None = Field(default=None, max_length=500)
    html_body: str | None = None
    text_body: str | None = None
    body: str | None = None
    language: str = "en"
    category: str | None = None
    variables: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class TemplateUpdate(BaseModel):
    name: str | None = None
    status: str | None = Field(default=None, pattern="^(DRAFT|ACTIVE|ARCHIVED)$")
    subject: str | None = None
    html_body: str | None = None
    text_body: str | None = None
    body: str | None = None
    language: str | None = None
    category: str | None = None
    variables: list[str] | None = None
    metadata: dict | None = None


class TemplatePreviewRequest(BaseModel):
    values: dict = Field(default_factory=dict)


# ---------------------------------------------------------------- campaigns
class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    channel: str = Field(pattern="^(EMAIL|WHATSAPP)$")
    template_id: str | None = None
    sending_account_id: str | None = None
    audience: dict = Field(default_factory=dict)
    schedule_at: str | None = None
    rate_config: dict = Field(default_factory=dict)
    track_opens: bool = False
    track_clicks: bool = False


class CampaignUpdate(BaseModel):
    name: str | None = None
    template_id: str | None = None
    sending_account_id: str | None = None
    audience: dict | None = None
    schedule_at: str | None = None
    rate_config: dict | None = None
    track_opens: bool | None = None
    track_clicks: bool | None = None


# --------------------------------------------------------------- suppression
class SuppressionCreate(BaseModel):
    channel: str = Field(pattern="^(EMAIL|WHATSAPP)$")
    address: str
    reason: str = Field(pattern="^(UNSUBSCRIBED|HARD_BOUNCE|COMPLAINT|MANUAL|INVALID)$")
    notes: str | None = None
