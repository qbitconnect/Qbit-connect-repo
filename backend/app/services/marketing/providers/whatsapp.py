"""WhatsApp Cloud API provider adapter (Phase 7 provider foundation).

Official WhatsApp Business Cloud API transport ONLY (Meta Graph API).
- No WhatsApp Web automation, no QR session reuse, no unofficial gateways.
- Template messages must reference provider templates in APPROVED state.
- Rate limits and error codes from Meta are honored via backoff (never evaded).

Config (non-secret):  api_base_url (default graph.facebook.com), api_version
Credentials (vault):  access_token, app_secret (webhook signature verify)
Account columns:      phone_number_id, business_account_id
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
from app.services.marketing.providers.errors import (
    TEMPLATE_NOT_APPROVED,
    classify_whatsapp_failure,
    retry_class,
)
from app.services.marketing.providers.events import WhatsAppEventNormalizer
from app.services.marketing.normalization import normalize_phone

DEFAULT_BASE = "https://graph.facebook.com"
DEFAULT_VERSION = "v21.0"


class WhatsAppProvider(BaseMarketingProvider):
    channel = "WHATSAPP"
    provider_id = "whatsapp_cloud"

    async def validate_configuration(self, config: dict, credentials: dict) -> ValidationOutcome:
        if not (credentials or {}).get("access_token"):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "Access token is required")
        if not (config or {}).get("phone_number_id"):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "phone_number_id is required")
        return ValidationOutcome(True)

    async def validate_sender(self, config: dict, credentials: dict, sender: dict) -> ValidationOutcome:
        phone_id = (sender or {}).get("phone_number_id", "").strip()
        if not phone_id or not re.fullmatch(r"\d{3,20}", phone_id):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "Valid phone_number_id is required")
        return ValidationOutcome(True)

    async def validate_recipient(self, recipient: str) -> ValidationOutcome:
        result = normalize_phone(recipient)
        if not result.valid:
            return ValidationOutcome(False, result.reason, "Recipient must be a valid E.164 number")
        return ValidationOutcome(True)

    async def validate_message(self, message: OutboundMessage) -> ValidationOutcome:
        if message.channel != "WHATSAPP":
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "Channel must be WHATSAPP")
        if not message.template_name:
            return ValidationOutcome(False, "INVALID_TEMPLATE", "Template messages require template_name")
        return ValidationOutcome(True)

    # ------------------------------------------------------------------ send
    async def send(self, config: dict, credentials: dict, message: OutboundMessage) -> SendResult:
        outcome = await self.validate_configuration(config, credentials)
        if not outcome.ok:
            return SendResult(False, error_code=outcome.code, error_message=outcome.message)
        recipient = normalize_phone(message.recipient)
        if not recipient.valid:
            return SendResult(False, error_code="INVALID_RECIPIENT", error_message="Invalid recipient phone")
        if not message.template_name:
            return SendResult(
                False, error_code="INVALID_TEMPLATE", error_message="Template messages require template_name"
            )

        components: list[dict] = []
        body_vars = message.template_vars.get("body") if isinstance(message.template_vars, dict) else None
        if isinstance(body_vars, list) and body_vars:
            components.append(
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(v)} for v in body_vars
                    ],
                }
            )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient.normalized,
            "type": "template",
            "template": {
                "name": message.template_name,
                "language": {"code": message.template_language or "en"},
                **({"components": components} if components else {}),
            },
        }
        base = str(config.get("api_base_url") or DEFAULT_BASE).rstrip("/")
        version = str(config.get("api_version") or DEFAULT_VERSION)
        url = f"{base}/{version}/{config.get('phone_number_id')}/messages"
        headers = {
            "Authorization": f"Bearer {credentials.get('access_token', '')}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, json=payload, headers=headers)
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
            messages = body.get("messages") or [{}]
            return SendResult(
                True,
                provider_message_id=(str(messages[0].get("id")) if messages[0].get("id") else None),
                provider_status="QUEUED",
                raw_metadata=self._sanitized({"wa_response": body.get("messaging_product", "whatsapp")}),
            )
        error_code, message_text = self._extract_error(response)
        code, msg = classify_whatsapp_failure(error_code, message_text)
        return SendResult(
            False,
            error_code=code,
            error_message=msg,
            retryable=retry_class(code) == "TRANSIENT",
        )

    async def get_status(self, config, credentials, provider_message_id: str) -> SendResult:
        # Delivery status arrives via webhooks; polling is not part of the
        # Cloud API contract — return honest UNKNOWN.
        return SendResult(True, provider_message_id=provider_message_id, provider_status="UNKNOWN")

    # ---------------------------------------------------------------- webhook
    async def handle_event(self, payload, headers, credentials) -> list[NormalizedEvent]:
        data = payload if isinstance(payload, dict) else {}
        return WhatsAppEventNormalizer().normalize(data)

    def verify_webhook_signature(
        self, *, raw_body: bytes, signature_header: str, app_secret: str
    ) -> bool:
        """X-Hub-Signature-256 verification (official scheme)."""
        if not signature_header or not app_secret:
            return False
        expected = hmac.new(
            app_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        provided = signature_header.strip()
        if provided.lower().startswith("sha256="):
            provided = provided[7:]
        return hmac.compare_digest(expected, provided.lower())

    # ----------------------------------------------------------------- health
    async def health_check(self, config: dict, credentials: dict) -> HealthOutcome:
        outcome = await self.validate_configuration(config, credentials)
        if not outcome.ok:
            return HealthOutcome(False, code=outcome.code, message=outcome.message)
        base = str(config.get("api_base_url") or DEFAULT_BASE).rstrip("/")
        version = str(config.get("api_version") or DEFAULT_VERSION)
        url = f"{base}/{version}/{config.get('phone_number_id')}"
        headers = {"Authorization": f"Bearer {credentials.get('access_token', '')}"}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            return HealthOutcome(False, degraded=True, code="CONNECTION_ERROR", message="API timeout")
        except httpx.HTTPError as exc:
            return HealthOutcome(
                False, degraded=True, code="CONNECTION_ERROR", message=f"API unreachable: {type(exc).__name__}"
            )
        if 200 <= response.status_code < 300:
            return HealthOutcome(True)
        error_code, message_text = self._extract_error(response)
        code, msg = classify_whatsapp_failure(error_code, message_text)
        return HealthOutcome(False, degraded=retry_class(code) == "TRANSIENT", code=code, message=msg)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _extract_error(response: httpx.Response) -> tuple[int | None, str]:
        try:
            body = response.json()
        except ValueError:
            return None, response.text[:300]
        error = body.get("error") or {}
        code = error.get("code")
        message = error.get("error_data", {}).get("details") or error.get("message") or ""
        return (int(code) if isinstance(code, int) else None), str(message)[:300]

    @staticmethod
    def extract_challenge(params: dict) -> str | None:
        """GET verification challenge (official webhook verification flow)."""
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token and challenge:
            return str(challenge)
        return None
