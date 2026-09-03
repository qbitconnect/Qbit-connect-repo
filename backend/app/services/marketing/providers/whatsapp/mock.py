"""WhatsApp MOCK provider — MOCK / TEST ONLY (Phase 6 §41).

Simulates the WhatsApp Business Cloud API deterministically for automated
tests and isolated staging drills. It can NEVER be registered outside test
environments (the registry gates it exactly like the generic mock provider)
and it must never appear in production send paths.

Simulated behaviors (deterministic, digit/template-name driven so they work
with structurally valid E.164 recipients):
- recipient digits contain "00000000"   → INVALID_RECIPIENT (not on WhatsApp)
- recipient digits contain "9999"       → RATE_LIMITED (TRANSIENT, retry-after 90s)
- recipient digits contain "555000"     → PROVIDER_UNAVAILABLE (TRANSIENT)
- access token starts with "EAAG-expired" → AUTHENTICATION_ERROR (PERMANENT)
- template name contains "unapproved"   → TEMPLATE_NOT_APPROVED (PERMANENT)
- template name contains "badtemplate"  → INVALID_TEMPLATE (PERMANENT)
- otherwise                             → success with a wamid-style id

Delivery/inbound events are produced by the test suite as raw webhook-shaped
payloads (see tests) — this mock does not fake a webhook transport.
"""

from __future__ import annotations

from app.services.marketing.phone import normalize_recipient_phone
from app.services.marketing.providers.base import ErrorClass, SendResult
from app.services.marketing.providers.whatsapp.provider import WhatsAppProvider


class WhatsAppMockProvider(WhatsAppProvider):
    provider_id = "whatsapp_mock"
    channel = "WHATSAPP"
    test_only = True

    def __init__(self) -> None:  # noqa: D107 — no transport, no HTTP ever
        super().__init__(transport_factory=None)

    async def validate_configuration(self, config: dict) -> list[str]:
        if not isinstance(config, dict) or not config.get("configured"):
            return ["Account is not configured"]
        if not str(config.get("phone_number_id") or "").strip():
            return ["phone_number_id is required"]
        return []

    async def send(self, *, account_config: dict, recipient_address: str,
                   subject: str | None, body: str, idempotency_key: str,
                   metadata: dict | None = None, credentials: dict | None = None,
                   template: dict | None = None) -> SendResult:
        if not (credentials or {}).get("access_token"):
            return SendResult.failure(
                "No access token available for this sending account",
                code="CREDENTIALS_MISSING", error_class=ErrorClass.CONFIGURATION,
            )
        ok, e164, reason = normalize_recipient_phone(recipient_address)
        if not ok:
            return SendResult.failure(
                f"Recipient phone is invalid ({reason})",
                code="INVALID_RECIPIENT", error_class=ErrorClass.PERMANENT,
            )
        template_name = str((template or {}).get("provider_template_name") or "")
        digits = (e164 or "").lstrip("+")
        token = str((credentials or {}).get("access_token") or "")

        if "00000000" in digits:
            return self._fail("Recipient phone number is not on WhatsApp", "INVALID_RECIPIENT")
        if "9999" in digits:
            result = self._fail("Rate limit hit — wait and retry", "RATE_LIMITED", ErrorClass.TRANSIENT)
            result.metadata = {"mock": True, "retry_after_seconds": 90.0}
            return result
        if "555000" in digits:
            return self._fail("Provider temporarily unavailable", "PROVIDER_UNAVAILABLE", ErrorClass.TRANSIENT)
        if token.startswith("EAAG-expired"):
            return self._fail("Access token validation failed", "AUTHENTICATION_ERROR")
        if "unapproved" in template_name.lower():
            return self._fail("Template is not approved", "TEMPLATE_NOT_APPROVED")
        if "badtemplate" in template_name.lower():
            return self._fail("Template does not exist in this language", "INVALID_TEMPLATE")
        if not template_name:
            return self._fail("WhatsApp Business sending requires a template", "INVALID_TEMPLATE")

        return SendResult.success(
            f"wamid.mock{abs(hash(idempotency_key)) % 10**12:012d}",
            status="SENT",
            metadata={"mock": True, "provider_status": "accepted"},
        )

    async def health_check(self, account_config: dict, credentials: dict | None = None) -> dict:
        if not (credentials or {}).get("access_token"):
            return {"health": "UNHEALTHY", "detail": "no credentials", "mock": True}
        return {"health": "HEALTHY", "detail": {"quality_rating": "GREEN"}, "mock": True}

    async def validate_account(self, account_config: dict, credentials: dict | None = None) -> dict:
        """Deterministic simulated connection flow (no network)."""
        from datetime import datetime, timezone

        config = account_config if isinstance(account_config, dict) else {}
        steps: dict[str, dict] = {}
        token = bool((credentials or {}).get("access_token"))
        steps["credentials"] = {"ok": token, "detail": "token present" if token else "no token"}
        phone_number_id = str(config.get("phone_number_id") or "").strip()
        steps["phone_number_configured"] = {
            "ok": bool(phone_number_id),
            "detail": phone_number_id or "phone_number_id missing",
        }
        if token and phone_number_id:
            steps["phone_number_valid"] = {
                "ok": True,
                "detail": {"display_phone_number_masked": "+91\u2022\u2022\u2022\u2022\u2022100",
                           "verified_name": "QBIT Mock", "quality_rating": "GREEN"},
            }
            steps["permissions"] = {"ok": True, "detail": "phone number readable"}
            steps["business_account"] = {
                "ok": True,
                "detail": {"name": "QBIT Mock WABA", "business_verification_status": "APPROVED"},
            }
        return {
            "ok": all(s.get("ok") for s in steps.values()),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "steps": steps,
        }

    async def fetch_templates(self, account_config: dict, credentials: dict | None = None) -> list[dict]:
        """Canned provider catalog (§41) covering every approval state."""
        if not (credentials or {}).get("access_token"):
            from app.services.marketing.providers.base import ErrorClass, ProviderError

            raise ProviderError(
                "No access token available for this sending account",
                error_class=ErrorClass.CONFIGURATION, code="CREDENTIALS_MISSING",
            )
        from app.services.marketing.providers.whatsapp.provider import normalize_provider_template

        return [
            t for t in (
                normalize_provider_template({
                    "id": "tpl-approved-1", "name": "welcome_business", "status": "APPROVED",
                    "category": "MARKETING", "language": "en",
                    "components": [{"type": "BODY", "text": "Hello {{1}}, welcome to {{2}}!"}],
                }),
                normalize_provider_template({
                    "id": "tpl-pending-1", "name": "order_update", "status": "PENDING",
                    "category": "UTILITY", "language": "en",
                    "components": [{"type": "BODY", "text": "Order {{1}} status."}],
                }),
                normalize_provider_template({
                    "id": "tpl-rejected-1", "name": "spam_offer", "status": "REJECTED",
                    "category": "MARKETING", "language": "en",
                    "components": [{"type": "BODY", "text": "Buy {{1}} now!"}],
                    "rejected_reason": "INVALID_FORMAT",
                }),
                normalize_provider_template({
                    "id": "tpl-paused-1", "name": "winback_offer", "status": "PAUSED",
                    "category": "MARKETING", "language": "en",
                    "components": [{"type": "BODY", "text": "Come back {{1}}!"}],
                }),
            ) if t is not None
        ]

    @staticmethod
    def _fail(message: str, code: str, error_class: ErrorClass = ErrorClass.PERMANENT) -> SendResult:
        result = SendResult.failure(message, code=code, error_class=error_class)
        result.metadata = {"mock": True}
        return result
