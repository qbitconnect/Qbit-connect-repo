"""EmailMockProvider — MOCK / TEST ONLY (Phase 7 §55).

Simulates the full email event lifecycle without ever touching a real
mailbox. NEVER registered outside isolated test environments (the registry
gates on QBIT_ENV and an explicit flag) and never used for production sends.

Scenario control: `config_metadata.scenario` on the sending account (or the
per-call metadata) selects the outcome:

    success (default) | temporary_failure | permanent_failure |
    auth_failure | unavailable | rate_limited | uncertain
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    ErrorClass,
    SendResult,
)
from app.services.marketing.providers.interfaces import EMAIL_RE

TEST_ONLY_WARNING = "MOCK / TEST ONLY — never for production email delivery"


class EmailMockProvider(BaseMarketingProvider):
    """Deterministic mock used by the test-suite and the mock smoke script."""

    provider_id = "email_mock"
    channel = "EMAIL"
    interface_only = False
    test_only = True

    async def validate_configuration(self, config: dict) -> list[str]:
        config = config if isinstance(config, dict) else {}
        if not config.get("configured"):
            return ["Account is not configured"]
        problems: list[str] = []
        if not EMAIL_RE.match(str(config.get("sender_email") or "")):
            problems.append("sender_email is required for the mock provider")
        return problems

    async def validate_recipient(self, address: str) -> bool:
        return bool(EMAIL_RE.match((address or "").strip()))

    async def validate_message(self, *, subject: str | None, body: str) -> list[str]:
        problems: list[str] = []
        if not (subject or "").strip():
            problems.append("Email requires a subject")
        return problems

    async def send(
        self, *, account_config: dict, recipient_address: str,
        subject: str | None, body: str, idempotency_key: str,
        metadata: dict | None = None, credentials: dict | None = None,
        template: dict | None = None,
    ) -> SendResult:
        if not EMAIL_RE.match((recipient_address or "").strip()):
            return SendResult.failure(
                "Recipient address is invalid",
                code="INVALID_RECIPIENT", error_class=ErrorClass.PERMANENT,
            )
        scenario = str(
            (metadata or {}).get("scenario")
            or (account_config or {}).get("scenario")
            or "success"
        ).lower()

        def _fail(code: str, error_class: ErrorClass, message: str,
                 extra: dict | None = None) -> SendResult:
            return SendResult(
                ok=False, error=message, error_code=code, error_class=error_class,
                status="FAILED",
                metadata={"mock": True, "scenario": scenario, **(extra or {})},
            )

        if scenario == "temporary_failure":
            return _fail("PROVIDER_UNAVAILABLE", ErrorClass.TRANSIENT,
                         "Mock temporary failure (provider unavailable)")
        if scenario == "permanent_failure":
            return _fail("INVALID_RECIPIENT", ErrorClass.PERMANENT,
                         "Mock permanent failure (recipient rejected)")
        if scenario == "auth_failure":
            return _fail("AUTHENTICATION_ERROR", ErrorClass.PERMANENT,
                         "Mock authentication failure")
        if scenario == "unavailable":
            return _fail("PROVIDER_UNAVAILABLE", ErrorClass.TRANSIENT,
                         "Mock provider unavailable")
        if scenario == "rate_limited":
            return _fail("RATE_LIMITED", ErrorClass.TRANSIENT, "Mock rate limited",
                         {"retry_after_seconds": 30})
        if scenario == "uncertain":
            return _fail("DELIVERY_STATE_UNKNOWN", ErrorClass.PERMANENT,
                         "Mock delivery state unknown (timeout) — not retried to prevent duplicates")

        # success: a stable pseudo provider_message_id derived from the
        # idempotency key so repeated sends for the same logical message
        # return the same id (mirrors real provider idempotent behaviour)
        digest = abs(hash(idempotency_key))
        provider_message_id = f"mock-email-{digest:x}"
        return SendResult.success(
            provider_message_id, status="SENT",
            metadata={"mock": True, "scenario": "success", TEST_ONLY_WARNING.split(" — ")[0]: True},
        )

    async def health_check(self, account_config: dict, credentials: dict | None = None) -> dict:
        scenario = str((account_config or {}).get("scenario") or "success").lower()
        health = "UNHEALTHY" if scenario in ("unavailable", "auth_failure") else "HEALTHY"
        return {
            "health": health,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "detail": {"mock": True, "scenario": scenario},
        }

    async def validate_account(self, account_config: dict, credentials: dict | None = None) -> dict:
        """Connection-flow validation mirroring the real adapters' step shape.

        NOTE: `configured` is an OUTPUT of validation, never a prerequisite —
        the account arrives PENDING/unconfigured and validation flips it."""
        config = account_config if isinstance(account_config, dict) else {}
        scenario = str(config.get("scenario") or "success").lower()
        sender = str(config.get("sender_email") or "").strip()
        steps = {
            "sender_email": {"ok": bool(EMAIL_RE.match(sender)),
                             "detail": sender or "sender_email missing or invalid"},
        }
        if scenario in ("unavailable", "auth_failure"):
            steps["connectivity"] = {"ok": False, "detail": f"mock scenario: {scenario}"}
        else:
            steps["connectivity"] = {"ok": True, "detail": "mock provider reachable"}
        return {
            "ok": all(step["ok"] for step in steps.values()),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "steps": steps,
        }

    async def handle_event(self, payload: dict) -> dict:
        return {
            "event_type": str(payload.get("event_type") or "").upper(),
            "provider_message_id": payload.get("provider_message_id"),
            "metadata": dict(payload.get("metadata") or {}),
        }
