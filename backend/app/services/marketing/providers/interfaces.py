"""Channel interfaces — Email / SMS (Phase 5 §2, §38, §39, §40).

WhatsApp is NO LONGER an interface stub: the real WhatsApp Business Cloud API
adapter lives in providers/whatsapp/ (Phase 6). These remaining stubs fail
honestly with `ProviderNotConfigured` until their phases add approved,
provider-supported integrations (transactional email: Phase 7, SMS: later).

They are registered so campaigns/sending accounts can REFERENCE the channels
and validation can report "Provider not configured" instead of crashing.
"""

from __future__ import annotations

import re

from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    ProviderNotConfigured,
    SendResult,
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
#: E.164-ish: optional +, 8-15 digits
PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")


def _require_config(config: dict, provider_id: str) -> None:
    """Interfaces have no credentials wired in Phase 5: any send attempt fails
    honestly instead of pretending success (brief §53)."""
    if not config or not config.get("configured"):
        raise ProviderNotConfigured(provider_id)


class EmailProvider(BaseMarketingProvider):
    """Email interface (SMTP or transactional API adapter arrives later).
    No credentials are hard-coded into the campaign engine (brief §38)."""

    provider_id = "email"
    channel = "EMAIL"
    interface_only = True

    async def validate_configuration(self, config: dict) -> list[str]:
        if not config or not config.get("configured"):
            return ["Provider not configured (email adapter arrives in a later phase)"]
        problems: list[str] = []
        if not str(config.get("from_address", "")).strip():
            problems.append("from_address is required")
        if not str(config.get("credential_ref", "")).strip():
            problems.append("credential reference is required (store secrets outside the database)")
        return problems

    async def validate_recipient(self, address: str) -> bool:
        return bool(EMAIL_RE.match((address or "").strip()))

    async def validate_message(self, *, subject: str | None, body: str) -> list[str]:
        problems: list[str] = []
        if not (subject or "").strip():
            problems.append("Email requires a subject")
        if len(body) > 200_000:
            problems.append("Email body exceeds 200,000 characters")
        return problems

    async def send(self, *, account_config: dict, recipient_address: str,
                   subject: str | None, body: str, idempotency_key: str,
                   metadata: dict | None = None, credentials: dict | None = None,
                   template: dict | None = None) -> SendResult:
        _require_config(account_config, self.provider_id)
        return SendResult.failure(
            "Email provider integration is not implemented in this phase",
            code="PROVIDER_NOT_IMPLEMENTED",
        )


class SMSProvider(BaseMarketingProvider):
    """SMS interface only (brief §40) — no real SMS provider in Phase 5."""

    provider_id = "sms"
    channel = "SMS"
    interface_only = True

    async def validate_configuration(self, config: dict) -> list[str]:
        if not config or not config.get("configured"):
            return ["Provider not configured (SMS integration arrives in a later phase)"]
        return ["SMS provider integration is not available yet"]

    async def validate_recipient(self, address: str) -> bool:
        return bool(PHONE_RE.match((address or "").strip()))

    async def validate_message(self, *, subject: str | None, body: str) -> list[str]:
        problems: list[str] = []
        if subject:
            problems.append("SMS messages do not use a subject")
        if len(body) > 1600:
            problems.append("SMS message exceeds 1600 characters")
        return problems

    async def send(self, *, account_config: dict, recipient_address: str,
                   subject: str | None, body: str, idempotency_key: str,
                   metadata: dict | None = None, credentials: dict | None = None,
                   template: dict | None = None) -> SendResult:
        _require_config(account_config, self.provider_id)
        return SendResult.failure(
            "SMS provider integration is not implemented in this phase",
            code="PROVIDER_NOT_IMPLEMENTED",
        )
