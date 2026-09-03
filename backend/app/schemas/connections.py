"""Connections API request schemas (Phase 6 §2, §36).

Credentials are WRITE-ONLY: accepted on create/rotate, never returned, never
logged, never echoed in error payloads. Responses use model.to_public_dict()
which exposes masked display fields only.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WhatsAppCredentials(BaseModel):
    """Provider secrets (write-only). Stored ENCRYPTED in the vault (§4)."""

    model_config = {"extra": "forbid"}

    access_token: str = Field(min_length=8, max_length=1024,
                              description="WhatsApp Business access token")
    app_secret: str | None = Field(default=None, max_length=256,
                                   description="Per-account webhook app secret (optional)")


class WhatsAppConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    provider: str | None = Field(default=None, max_length=50,
                                 description="Defaults to WHATSAPP_PROVIDER env setting")
    phone_number_id: str | None = Field(default=None, max_length=100)
    business_account_id: str | None = Field(default=None, max_length=100)
    credentials: WhatsAppCredentials | None = None
    capabilities: dict[str, Any] | None = None


class WhatsAppConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    phone_number_id: str | None = Field(default=None, max_length=100)
    business_account_id: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=20)
    capabilities: dict[str, Any] | None = None
    credentials: WhatsAppCredentials | None = Field(
        default=None, description="Presenting credentials ROTATES them (write-only)"
    )
    config_metadata: dict[str, Any] | None = None


class WhatsAppConnectionValidateRequest(BaseModel):
    """Reserved for future options (e.g. skip optional checks)."""

    model_config = {"extra": "forbid"}
