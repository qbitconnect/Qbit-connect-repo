"""Marketing provider abstraction (Phase 7 §1).

BaseMarketingProvider is the ONLY integration surface between the campaign
engine and any delivery vendor. CampaignService never contains SMTP/API or
WhatsApp code (spec §1, §17).

        BaseMarketingProvider
                |
                +-- WhatsAppProvider      (WhatsApp Cloud API — official)
                +-- SMTPProvider          (RFC 5321 SMTP with TLS/STARTTLS)
                +-- GenericEmailAPIProvider (vendor HTTP API, pluggable)
                +-- MockEmailProvider / MockWhatsAppProvider  (TEST ONLY)

Compliance (spec §64): adapters use official/provider-supported transports
only — no inbox automation, no restriction/rate-limit evasion, no spam-filter
manipulation. Rate limiting from providers is honored (backoff), never bypassed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    code: str | None = None       # machine-readable failure code
    message: str | None = None    # honest, user-presentable message


@dataclass(frozen=True)
class SendResult:
    success: bool
    provider_message_id: str | None = None
    provider_status: str | None = None
    error_code: str | None = None       # normalized category (spec §23)
    error_message: str | None = None    # sanitized message (no secrets)
    retryable: bool = False             # TRANSIENT classification
    raw_metadata: dict = field(default_factory=dict)  # sanitized, no secrets


@dataclass(frozen=True)
class HealthOutcome:
    healthy: bool
    degraded: bool = False
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class NormalizedEvent:
    """Provider event in platform vocabulary (spec §26)."""

    provider_event_id: str
    event_type: str            # SENT|DELIVERED|BOUNCED|COMPLAINED|FAILED|OPENED|CLICKED|REPLIED
    provider_message_id: str | None
    recipient: str | None
    timestamp: datetime | None = None
    hard_bounce: bool = False
    reason: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundMessage:
    """Provider-agnostic message envelope for a single recipient (spec §40:
    individual recipient delivery — never bulk CC)."""

    channel: str                     # EMAIL | WHATSAPP
    recipient: str                   # email or E.164 phone
    sender_name: str | None = None
    sender: str | None = None        # from address (email) / phone id (wa)
    reply_to: str | None = None
    subject: str | None = None
    html: str | None = None
    text: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_vars: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)   # Message-ID / In-Reply-To / References
    metadata: dict = field(default_factory=dict)  # campaign_id, recipient_id, idempotency_key

    def secrets(self) -> list[str]:
        """Helper for tests: outbound messages must never carry secrets."""
        return []


class BaseMarketingProvider:
    """Contract every channel provider implements (spec §1)."""

    channel: str = "UNKNOWN"
    provider_id: str = "unknown"
    #: TEST-ONLY adapters must set True; the registry refuses them in production.
    is_mock: bool = False

    async def validate_configuration(
        self, config: dict, credentials: dict
    ) -> ValidationOutcome:  # pragma: no cover - interface
        raise NotImplementedError

    async def validate_sender(self, config: dict, credentials: dict, sender: dict) -> ValidationOutcome:
        raise NotImplementedError

    async def validate_recipient(self, recipient: str) -> ValidationOutcome:
        raise NotImplementedError

    async def validate_message(self, message: OutboundMessage) -> ValidationOutcome:
        raise NotImplementedError

    async def send(
        self, config: dict, credentials: dict, message: OutboundMessage
    ) -> SendResult:
        raise NotImplementedError

    async def get_status(
        self, config: dict, credentials: dict, provider_message_id: str
    ) -> SendResult:
        raise NotImplementedError

    async def handle_event(
        self, payload: bytes | dict, headers: dict[str, str], credentials: dict
    ) -> list[NormalizedEvent]:
        """Parse + normalize an already-authenticated webhook payload."""
        raise NotImplementedError

    async def health_check(self, config: dict, credentials: dict) -> HealthOutcome:
        raise NotImplementedError

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _sanitized(raw: dict, *, forbidden_keys: set[str] | None = None) -> dict:
        """Strip credential-like keys from provider raw payloads before storage."""
        forbidden = forbidden_keys or {
            "password", "api_key", "apikey", "token", "access_token",
            "secret", "authorization", "smtp_password", "credentials",
        }
        out: dict = {}
        for k, v in (raw or {}).items():
            if k.lower() in forbidden:
                out[k] = "••••"
            elif isinstance(v, dict):
                out[k] = BaseMarketingProvider._sanitized(v, forbidden_keys=forbidden)
            else:
                out[k] = v
        return out
