"""Marketing provider abstraction (Phase 5 §3).

A provider is the ONLY component that talks to an external messaging
platform. CampaignService never imports a concrete provider — it resolves
whatever provider the SendingAccount references through the registry.

Contract:
    BaseMarketingProvider
      +-- WhatsAppProvider   (interface — real integration is a later phase)
      +-- EmailProvider      (interface)
      +-- SMSProvider        (interface)
      +-- MockProvider       (MOCK / TEST ONLY — never auto-registered)

Rules enforced here:
- providers never log or return secrets
- every failure is classified TRANSIENT (retryable) or PERMANENT (never retry)
- unconfigured providers fail honestly (ProviderNotConfigured) — the UI then
  shows "Provider not configured" and blocks launch (brief §26, §35)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class ErrorClass(str, enum.Enum):
    TRANSIENT = "TRANSIENT"    # network timeouts, provider 5xx — may retry
    PERMANENT = "PERMANENT"    # invalid recipient, rejected template — never retry
    CONFIGURATION = "CONFIGURATION"  # account/provider misconfiguration


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass = ErrorClass.TRANSIENT,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.code = code or "PROVIDER_ERROR"


class ProviderNotConfigured(ProviderError):
    def __init__(self, provider_id: str) -> None:
        super().__init__(
            f"Provider '{provider_id}' is not configured",
            error_class=ErrorClass.CONFIGURATION,
            code="PROVIDER_NOT_CONFIGURED",
        )


@dataclass
class SendResult:
    """Outcome of one provider send attempt (no secrets may appear here)."""

    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    error_code: str | None = None
    error_class: ErrorClass = ErrorClass.TRANSIENT
    #: raw provider status string, normalized by the provider itself
    status: str = "UNKNOWN"
    metadata: dict = field(default_factory=dict)

    @classmethod
    def success(cls, provider_message_id: str | None, *, status: str = "SENT",
                metadata: dict | None = None) -> "SendResult":
        return cls(ok=True, provider_message_id=provider_message_id,
                   status=status, metadata=metadata or {})

    @classmethod
    def failure(cls, message: str, *, code: str = "PROVIDER_ERROR",
                error_class: ErrorClass = ErrorClass.TRANSIENT) -> "SendResult":
        return cls(ok=False, error=message[:500], error_code=code,
                   error_class=error_class, status="FAILED")


class BaseMarketingProvider:
    """Abstract channel provider. Stateless by design — all account context
    is passed per call so one provider class serves many sending accounts."""

    provider_id: str = "base"
    channel: str = "UNKNOWN"
    #: providers that are interfaces only (no real integration yet) set True
    interface_only: bool = False
    #: MOCK / TEST ONLY providers set True — gated out of production registry
    test_only: bool = False

    async def validate_configuration(self, config: dict) -> list[str]:
        """Return a list of configuration problems (empty = valid).
        Must never raise for user input; never echo secret values."""
        raise NotImplementedError

    async def validate_recipient(self, address: str) -> bool:
        """Structural validation of a recipient address for this channel."""
        raise NotImplementedError

    async def validate_message(
        self, *, subject: str | None, body: str
    ) -> list[str]:
        """Channel rules (length, formatting). Returns problem list."""
        raise NotImplementedError

    async def send(
        self,
        *,
        account_config: dict,
        recipient_address: str,
        subject: str | None,
        body: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> SendResult:
        """Send one message. MUST be idempotent for the same idempotency_key
        where the provider supports it; must never raise for provider-side
        failures — return SendResult.failure instead."""
        raise NotImplementedError

    async def get_status(
        self, *, account_config: dict, provider_message_id: str
    ) -> dict:
        """Fetch delivery status for a message (best effort)."""
        return {"status": "UNKNOWN"}

    async def handle_event(self, payload: dict) -> dict:
        """Normalize a provider webhook/event payload into a campaign event:
        {event_type, provider_message_id, metadata}. Never log secrets."""
        raise NotImplementedError

    async def health_check(self, account_config: dict) -> dict:
        """Lightweight account health probe."""
        return {
            "health": "UNKNOWN",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
