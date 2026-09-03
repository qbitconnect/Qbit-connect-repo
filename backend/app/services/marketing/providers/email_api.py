"""Generic Email API provider adapter (Phase 7 §3, §5).

A documented, vendor-neutral HTTP contract for transactional email APIs. The
platform ships with this adapter so self-hosted / generic vendors work out of
the box; vendor-specific adapters (SES, SendGrid, …) subclass it and override
`_send_payload` / `handle_event` without touching campaign code.

Config (non-secret):  api_base_url, timeout_seconds, region (optional)
Credentials (vault):  api_key, account_id (optional)

Contract:
    POST {api_base_url}/messages
      Headers: Authorization: Bearer <api_key>
      Body:    {"from": "...", "from_name": "...", "reply_to": "...",
                "to": "...", "subject": "...", "html": "...", "text": "...",
                "headers": {...}, "metadata": {...}}
      2xx →    {"message_id": "...", "status": "queued|sent"}

Webhook (receiver side, generic): signature `X-QBIT-Signature: sha256=HMAC(secret, body)`
with `X-QBIT-Timestamp` replay protection — implemented by the webhook service.
"""

from __future__ import annotations

import re

import httpx

from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    HealthOutcome,
    NormalizedEvent,
    OutboundMessage,
    SendResult,
    ValidationOutcome,
)
from app.services.marketing.providers.errors import classify_http_failure
from app.services.marketing.providers.events import EmailEventNormalizer

CRLF_RE = re.compile(r"[\r\n]")


class GenericEmailAPIProvider(BaseMarketingProvider):
    channel = "EMAIL"
    provider_id = "email_api"

    def _client(self, config: dict, credentials: dict) -> httpx.AsyncClient:
        base = str((config or {}).get("api_base_url", "")).rstrip("/")
        headers = {"Authorization": f"Bearer {(credentials or {}).get('api_key', '')}"}
        return httpx.AsyncClient(
            base_url=base,
            headers=headers,
            timeout=float((config or {}).get("timeout_seconds", 30)),
        )

    async def validate_configuration(self, config: dict, credentials: dict) -> ValidationOutcome:
        base = str((config or {}).get("api_base_url", "")).strip().rstrip("/")
        if not base:
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "API base URL is required")
        if not base.startswith(("http://", "https://")):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "API base URL must be http(s)")
        if not (credentials or {}).get("api_key"):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "API key is required")
        return ValidationOutcome(True)

    async def validate_sender(self, config: dict, credentials: dict, sender: dict) -> ValidationOutcome:
        address = (sender or {}).get("sender_email", "").strip()
        if not address or CRLF_RE.search(address) or " " in address:
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "Valid sender email is required")
        return ValidationOutcome(True)

    async def validate_recipient(self, recipient: str) -> ValidationOutcome:
        from app.services.marketing.normalization import normalize_email

        result = normalize_email(recipient)
        if not result.valid:
            return ValidationOutcome(False, result.reason, "Invalid recipient email")
        return ValidationOutcome(True)

    async def validate_message(self, message: OutboundMessage) -> ValidationOutcome:
        if message.channel != "EMAIL":
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "Channel must be EMAIL")
        if not message.text and not message.html:
            return ValidationOutcome(False, "MESSAGE_REJECTED", "Message body is empty")
        if message.subject and CRLF_RE.search(message.subject):
            return ValidationOutcome(False, "MESSAGE_REJECTED", "Subject contains forbidden characters")
        return ValidationOutcome(True)

    async def send(self, config: dict, credentials: dict, message: OutboundMessage) -> SendResult:
        outcome = await self.validate_configuration(config, credentials)
        if not outcome.ok:
            return SendResult(False, error_code=outcome.code, error_message=outcome.message)
        outcome = await self.validate_message(message)
        if not outcome.ok:
            return SendResult(False, error_code=outcome.code, error_message=outcome.message)

        payload = {
            "from": message.sender,
            "from_name": message.sender_name,
            "reply_to": message.reply_to,
            "to": message.recipient,
            "subject": message.subject or "",
            "html": message.html,
            "text": message.text,
            "headers": {k: v for k, v in (message.headers or {}).items() if not CRLF_RE.search(str(v))},
            "metadata": self._sanitized(message.metadata),
        }
        try:
            async with self._client(config, credentials) as client:
                response = await client.post("/messages", json=self._send_payload(payload))
        except httpx.TimeoutException:
            return SendResult(False, error_code="CONNECTION_ERROR", error_message="API timeout", retryable=True)
        except httpx.HTTPError as exc:
            return SendResult(
                False,
                error_code="CONNECTION_ERROR",
                error_message=f"API connection failed: {type(exc).__name__}",
                retryable=True,
            )
        if 200 <= response.status_code < 300:
            try:
                body = response.json()
            except ValueError:
                body = {}
            return SendResult(
                True,
                provider_message_id=(str(body.get("message_id")) if body.get("message_id") else None),
                provider_status=str(body.get("status", "SENT")),
                raw_metadata={"http_status": response.status_code},
            )
        code, msg = classify_http_failure(response.status_code, response.text)
        from app.services.marketing.providers.errors import retry_class

        return SendResult(
            False,
            error_code=code,
            error_message=msg,
            retryable=retry_class(code) == "TRANSIENT",
        )

    async def get_status(
        self, config: dict, credentials: dict, provider_message_id: str
    ) -> SendResult:
        try:
            async with self._client(config, credentials) as client:
                response = await client.get(f"/messages/{provider_message_id}")
        except httpx.HTTPError as exc:
            return SendResult(
                False,
                error_code="CONNECTION_ERROR",
                error_message=f"API connection failed: {type(exc).__name__}",
                retryable=True,
            )
        if 200 <= response.status_code < 300:
            try:
                body = response.json()
            except ValueError:
                body = {}
            return SendResult(
                True,
                provider_message_id=provider_message_id,
                provider_status=str(body.get("status", "UNKNOWN")),
            )
        code, msg = classify_http_failure(response.status_code, response.text)
        return SendResult(False, error_code=code, error_message=msg)

    async def handle_event(self, payload, headers, credentials) -> list[NormalizedEvent]:
        data = payload if isinstance(payload, dict) else {}
        return EmailEventNormalizer().normalize(data)

    async def health_check(self, config: dict, credentials: dict) -> HealthOutcome:
        outcome = await self.validate_configuration(config, credentials)
        if not outcome.ok:
            return HealthOutcome(False, code=outcome.code, message=outcome.message)
        try:
            async with self._client(config, credentials) as client:
                response = await client.get("/health")
        except httpx.TimeoutException:
            return HealthOutcome(False, degraded=True, code="CONNECTION_ERROR", message="API timeout")
        except httpx.HTTPError as exc:
            return HealthOutcome(
                False, degraded=True, code="CONNECTION_ERROR", message=f"API unreachable: {type(exc).__name__}"
            )
        if 200 <= response.status_code < 300:
            return HealthOutcome(True)
        code, msg = classify_http_failure(response.status_code, response.text)
        return HealthOutcome(False, degraded=code in ("RATE_LIMITED",), code=code, message=msg)

    # ------------------------------------------------------------- hooks
    def _send_payload(self, payload: dict) -> dict:
        """Vendor adapters override this to translate the generic payload."""
        return payload
