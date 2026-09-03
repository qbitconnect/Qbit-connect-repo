"""Marketing API request schemas (Phase 5 §29).

Response envelopes follow the platform convention: {"success": true, "data": ...}.
These models validate WRITE payloads only — reads use model.to_public_dict().
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AudienceDefinition(BaseModel):
    type: str = Field(description="saved_view | filters | tags | selected")
    saved_view_id: str | None = None
    filters: dict[str, Any] | list[Any] | None = None
    tags: list[str] | None = None
    match: str | None = None
    lead_ids: list[str] | None = None
    statuses: list[str] | None = None


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    channel: str = Field(min_length=1, max_length=20)
    audience_definition: dict[str, Any] | None = None
    template_id: str | None = None
    sending_account_id: str | None = None
    schedule_type: str = "SEND_NOW"
    scheduled_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    # Phase 7 §31: campaign-level settings accepted at creation too
    campaign_metadata: dict[str, Any] | None = None


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    audience_definition: dict[str, Any] | None = None
    template_id: str | None = None
    sending_account_id: str | None = None
    schedule_type: str | None = None
    scheduled_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    # Phase 7 §31: campaign-level settings (tracking is opt-in per campaign)
    campaign_metadata: dict[str, Any] | None = None


class CampaignMetadataUpdate(BaseModel):
    """Phase 7 campaign settings (§31, §12, §37). Secret-like keys rejected."""

    model_config = {"extra": "forbid"}

    track_opens: bool | None = None
    track_clicks: bool | None = None
    append_unsubscribe_footer: bool | None = None
    company_name: str | None = Field(default=None, max_length=200)
    company_address: str | None = Field(default=None, max_length=500)


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    channel: str = Field(min_length=1, max_length=20)
    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1)
    language: str = "en"
    status: str = "DRAFT"
    variables: list[str] | None = None
    # Phase 7 §10: plain-text fallback for EMAIL templates (optional —
    # derived from the HTML body when omitted)
    text_body: str | None = Field(default=None, max_length=200_000)


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    subject: str | None = None
    body: str | None = None
    language: str | None = None
    status: str | None = None
    variables: list[str] | None = None
    text_body: str | None = Field(default=None, max_length=200_000)


class TemplatePreviewRequest(BaseModel):
    lead_id: str | None = None
    sample: dict[str, Any] | None = None


class SendingAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    channel: str = Field(min_length=1, max_length=20)
    provider: str = Field(min_length=1, max_length=50)
    identifier: str = Field(min_length=1, max_length=300)
    display_identifier: str | None = Field(default=None, max_length=300)
    capabilities: dict[str, Any] | None = None
    #: NON-SECRET configuration only (e.g. from_address, phone_number_id,
    #: rate_policy). Secret-like keys are rejected — the credential vault is a
    #: later phase (architecture doc 17).
    config_metadata: dict[str, Any] | None = None


class SendingAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    display_identifier: str | None = None
    status: str | None = None
    capabilities: dict[str, Any] | None = None
    config_metadata: dict[str, Any] | None = None


class SuppressionCreate(BaseModel):
    type: str = Field(min_length=1, max_length=20)
    address: str = Field(min_length=1, max_length=320)
    reason: str = "MANUAL"
    channel: str | None = None
    source: str | None = Field(default=None, max_length=200)
    lead_id: str | None = None


class OptOutCreate(BaseModel):
    channel: str = Field(min_length=1, max_length=20)
    address: str = Field(min_length=1, max_length=320)
    reason: str = "UNSUBSCRIBED"
    source: str | None = Field(default=None, max_length=200)
    lead_id: str | None = None


class ProviderEventRequest(BaseModel):
    """Generic provider event ingestion (§37) — normalized then applied."""

    provider: str = Field(min_length=1, max_length=50)
    event: str = Field(min_length=1, max_length=50)
    provider_message_id: str | None = None
    metadata: dict[str, Any] | None = None
