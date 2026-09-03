"""MOCK provider — MOCK / TEST ONLY (Phase 5 §36).

- Clearly marked as a mock everywhere it appears (API responses expose
  provider_id "mock" and the UI labels it "MOCK / TEST ONLY").
- It can NEVER be registered automatically in production: the registry gates
  it behind QBIT_ENV == "test" or the explicit QBIT_MARKETING_ALLOW_MOCK_PROVIDER
  flag, and refuses when QBIT_ENV == "production".
- The UI is forbidden from claiming a real message was sent when only the mock
  provider was used — campaign payloads include provider_id so the UI can
  label results honestly.
- Deterministic behavior for tests: addresses containing "fail" fail
  PERMANENT, addresses containing "flaky" fail TRANSIENT, everything else
  succeeds with a synthetic provider message id.
"""

from __future__ import annotations

import re

from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    ErrorClass,
    SendResult,
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")


class MockProvider(BaseMarketingProvider):
    provider_id = "mock"
    channel = "MOCK"
    test_only = True

    async def validate_configuration(self, config: dict) -> list[str]:
        return []  # the mock is always "configured"

    async def validate_recipient(self, address: str) -> bool:
        value = (address or "").strip()
        return bool(EMAIL_RE.match(value) or PHONE_RE.match(value))

    async def validate_message(self, *, subject: str | None, body: str) -> list[str]:
        if not (body or "").strip():
            return ["Message body must not be empty"]
        return []

    async def send(self, *, account_config: dict, recipient_address: str,
                   subject: str | None, body: str, idempotency_key: str,
                   metadata: dict | None = None, credentials: dict | None = None,
                   template: dict | None = None) -> SendResult:
        address = (recipient_address or "").lower()
        if "flaky" in address:
            return SendResult.failure(
                "Mock transient failure (flaky recipient)",
                code="MOCK_TRANSIENT",
                error_class=ErrorClass.TRANSIENT,
            )
        if "fail" in address:
            return SendResult.failure(
                "Mock permanent failure (invalid recipient)",
                code="MOCK_PERMANENT",
                error_class=ErrorClass.PERMANENT,
            )
        return SendResult.success(
            f"mock-{idempotency_key[:18]}",
            status="SENT",
            metadata={"mock": True},
        )

    async def handle_event(self, payload: dict) -> dict:
        event_type = str(payload.get("event_type") or "MESSAGE_SENT").upper()
        return {
            "event_type": event_type,
            "provider_message_id": payload.get("provider_message_id"),
            "metadata": {"mock": True, **(payload.get("metadata") or {})},
        }

    async def health_check(self, account_config: dict) -> dict:
        return {"health": "HEALTHY", "provider": "mock", "test_only": True}
