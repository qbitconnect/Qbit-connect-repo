"""MOCK / TEST ONLY providers — NEVER for production messaging.

The registry refuses these adapters whenever QBIT_ENV == "production"
(defence in depth on top of the config validator). They exist so the full
campaign → queue → worker → provider → event pipeline can be tested without
touching any real recipient (spec §55).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    HealthOutcome,
    NormalizedEvent,
    OutboundMessage,
    SendResult,
    ValidationOutcome,
)


class MockEmailProvider(BaseMarketingProvider):
    """Scriptable in-memory email provider (MOCK / TEST ONLY).

    Behaviour matrix via ``credentials["mode"]`` (or message metadata override
    ``metadata["mock_mode"]``):
        success            → accept, return id
        transient_failure  → CONNECTION_ERROR, retryable
        permanent_failure  → INVALID_RECIPIENT, not retryable
        auth_failure       → AUTHENTICATION_ERROR
        unavailable        → PROVIDER_UNAVAILABLE, retryable
    Every accepted send is recorded in ``sent`` for assertions; ``events_to_emit``
    lets tests fabricate delivery/bounce/complaint/open/click webhooks.
    """

    channel = "EMAIL"
    provider_id = "mock_email"
    is_mock = True

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.events_to_emit: list[dict] = []
        self.emitted_events: list[NormalizedEvent] = []

    async def validate_configuration(self, config: dict, credentials: dict) -> ValidationOutcome:
        if not (config or {}).get("mock_ready"):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "mock_ready flag required (TEST ONLY)")
        return ValidationOutcome(True)

    async def validate_sender(self, config, credentials, sender) -> ValidationOutcome:
        if not (sender or {}).get("sender_email"):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "sender_email required")
        return ValidationOutcome(True)

    async def validate_recipient(self, recipient: str) -> ValidationOutcome:
        if not recipient or "@" not in recipient:
            return ValidationOutcome(False, "INVALID_RECIPIENT", "invalid test recipient")
        return ValidationOutcome(True)

    async def validate_message(self, message: OutboundMessage) -> ValidationOutcome:
        if not (message.text or message.html):
            return ValidationOutcome(False, "MESSAGE_REJECTED", "empty body")
        return ValidationOutcome(True)

    async def send(self, config: dict, credentials: dict, message: OutboundMessage) -> SendResult:
        mode = (message.metadata or {}).get("mock_mode") or (credentials or {}).get("mode", "success")
        if mode == "transient_failure":
            return SendResult(False, error_code="CONNECTION_ERROR", error_message="mock transient", retryable=True)
        if mode == "permanent_failure":
            return SendResult(False, error_code="INVALID_RECIPIENT", error_message="mock permanent", retryable=False)
        if mode == "auth_failure":
            return SendResult(False, error_code="AUTHENTICATION_ERROR", error_message="mock auth", retryable=False)
        if mode == "unavailable":
            return SendResult(False, error_code="PROVIDER_UNAVAILABLE", error_message="mock down", retryable=True)
        if mode == "rate_limited":
            return SendResult(False, error_code="RATE_LIMITED", error_message="mock 429", retryable=True)
        provider_id = f"mock-{uuid.uuid4().hex}"
        self.sent.append(message)
        return SendResult(
            True,
            provider_message_id=provider_id,
            provider_status="SENT",
            raw_metadata={"transport": "mock", "mode": mode},
        )

    async def get_status(self, config, credentials, provider_message_id: str) -> SendResult:
        return SendResult(True, provider_message_id=provider_message_id, provider_status="SENT")

    async def handle_event(self, payload, headers, credentials) -> list[NormalizedEvent]:
        data = payload if isinstance(payload, dict) else json.loads(payload or b"{}")
        events: list[NormalizedEvent] = []
        for raw in data.get("events", []):
            etype = {
                "delivered": "DELIVERED", "bounce": "BOUNCED", "complaint": "COMPLAINED",
                "open": "OPENED", "click": "CLICKED", "reply": "REPLIED", "fail": "FAILED",
            }.get(str(raw.get("type", "")).lower())
            if not etype:
                continue
            event = NormalizedEvent(
                provider_event_id=str(raw.get("id") or uuid.uuid4().hex),
                event_type=etype,
                provider_message_id=raw.get("message_id"),
                recipient=raw.get("recipient"),
                timestamp=datetime.fromtimestamp(
                    float(raw.get("timestamp", time.time())), tz=timezone.utc
                ),
                hard_bounce=bool(raw.get("hard", False)) and etype == "BOUNCED",
                reason=raw.get("reason"),
                payload=raw.get("meta") or {},
            )
            events.append(event)
            self.emitted_events.append(event)
        return events

    async def health_check(self, config: dict, credentials: dict) -> HealthOutcome:
        if (credentials or {}).get("mode") == "unavailable":
            return HealthOutcome(False, degraded=True, code="PROVIDER_UNAVAILABLE", message="mock down")
        return HealthOutcome(True)


class MockWhatsAppProvider(MockEmailProvider):
    channel = "WHATSAPP"
    provider_id = "mock_whatsapp"
    is_mock = True

    async def validate_recipient(self, recipient: str) -> ValidationOutcome:
        from app.services.marketing.normalization import normalize_phone

        if not normalize_phone(recipient).valid:
            return ValidationOutcome(False, "INVALID_RECIPIENT", "invalid test phone")
        return ValidationOutcome(True)
