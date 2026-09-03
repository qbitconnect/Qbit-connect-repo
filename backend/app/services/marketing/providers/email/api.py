"""GenericEmailAPIProvider — transactional email HTTP API adapter (Phase 7 §3, §5).

A VENDOR-NEUTRAL adapter over the documented QBIT email-send contract:

    POST {EMAIL_API_BASE_URL}/messages
    Authorization: Bearer {api_key}
    {
      "from": {"email": "sender@company.com", "name": "QBIT Sales"},
      "to": [{"email": "recipient@example.com"}],           # 1:1 sends only
      "reply_to": "reply@company.com",                       # optional
      "subject": "...",
      "text": "...",                                        # plain text
      "html": "...",                                        # optional
      "headers": {"Message-ID": "<...>"}                    # optional
    }
    2xx → {"id": "<provider_message_id>", ...}

Provider-specific settings belong in provider-specific configuration; this
adapter makes NO vendor assumptions beyond the contract above (brief §5).
Any future vendor adapter plugs into the same BaseMarketingProvider shape.

Security:
- the API key arrives per-call via `credentials` (decrypted vault) and is
  never logged, persisted, or echoed
- header values are CR/LF-guarded before the request is built (§39)
- errors are normalized + sanitized (§23); provider limits are never bypassed
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import datetime, timezone

import httpx

from app.core.logging import get_logger
from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    ErrorClass,
    SendResult,
)
from app.services.marketing.providers.email.errors import (
    CONFIGURATION_ERROR,
    EmailErrorNormalizer,
)
from app.services.marketing.providers.email.smtp import build_message_id, header_safe
from app.services.marketing.providers.interfaces import EMAIL_RE

logger = get_logger("qbit.marketing.email_api")

DEFAULT_TIMEOUT_SECONDS = 30.0

_CRLF_RE = re.compile(r"[\r\n]")


class GenericEmailAPIProvider(BaseMarketingProvider):
    """Generic transactional-email HTTP API adapter (multi-account)."""

    provider_id = "email_api"
    channel = "EMAIL"
    interface_only = False

    def __init__(self, *, error_normalizer: EmailErrorNormalizer | None = None,
                 transport_factory=None) -> None:
        self.errors = error_normalizer or EmailErrorNormalizer()
        # transport_factory(api_key) -> httpx.AsyncBaseTransport — TEST HOOK
        self._transport_factory = transport_factory

    # ------------------------------------------------------- configuration
    async def validate_configuration(self, config: dict) -> list[str]:
        config = config if isinstance(config, dict) else {}
        if not config.get("configured"):
            return ["Account is not configured — complete the connection wizard"]
        problems: list[str] = []
        base = str(config.get("api_base_url") or "").strip()
        if not base:
            problems.append("api_base_url is required")
        elif not base.startswith(("http://", "https://")):
            problems.append("api_base_url must be an http(s) URL")
        sender = str(config.get("sender_email") or "").strip()
        if not sender:
            problems.append("sender_email is required")
        elif not EMAIL_RE.match(sender):
            problems.append("sender_email is not a valid address")
        reply_to = str(config.get("reply_to") or "").strip()
        if reply_to and not EMAIL_RE.match(reply_to):
            problems.append("reply_to is not a valid address")
        if not str(config.get("credential_ref") or "").strip():
            problems.append(
                "No credential reference — store the API key in the encrypted vault"
            )
        return problems

    async def validate_recipient(self, address: str) -> bool:
        return bool(EMAIL_RE.match((address or "").strip()))

    async def validate_message(self, *, subject: str | None, body: str) -> list[str]:
        problems: list[str] = []
        if not (subject or "").strip():
            problems.append("Email requires a subject")
        if header_safe(subject) is False:
            problems.append("Subject contains forbidden control characters")
        if len(body or "") > 200_000:
            problems.append("Email body exceeds 200,000 characters")
        return problems

    async def validate_send_requirements(self, *, template, account_config: dict) -> list[str]:
        problems: list[str] = []
        sender = str((account_config or {}).get("sender_email") or "").strip()
        if not sender or not EMAIL_RE.match(sender):
            problems.append("Sending account has no valid sender_email configured")
        base = str((account_config or {}).get("api_base_url") or "").strip()
        if not base:
            problems.append("Sending account has no api_base_url configured")
        return problems

    # ------------------------------------------------------------------ send
    async def send(
        self, *, account_config: dict, recipient_address: str,
        subject: str | None, body: str, idempotency_key: str,
        metadata: dict | None = None, credentials: dict | None = None,
        template: dict | None = None,
    ) -> SendResult:
        config = account_config if isinstance(account_config, dict) else {}
        base = str(config.get("api_base_url") or "").strip().rstrip("/")
        sender = str(config.get("sender_email") or "").strip()
        sender_name = str(config.get("sender_name") or "").strip()
        reply_to = str(config.get("reply_to") or "").strip()
        api_key = str((credentials or {}).get("api_key") or "").strip()
        region = str(config.get("region") or "").strip()

        if not base or not sender or not api_key:
            return SendResult.failure(
                "Email API account is not fully configured (api_base_url / sender_email / api_key)",
                code=CONFIGURATION_ERROR, error_class=ErrorClass.CONFIGURATION,
            )
        if not EMAIL_RE.match(recipient_address.strip()):
            return SendResult.failure(
                "Recipient address is invalid",
                code="INVALID_RECIPIENT", error_class=ErrorClass.PERMANENT,
            )
        for label, value in (("subject", subject), ("sender", sender),
                             ("reply_to", reply_to), ("sender_name", sender_name)):
            if not header_safe(value):
                return SendResult.failure(
                    f"{label} contains forbidden control characters (header injection rejected)",
                    code="MESSAGE_REJECTED", error_class=ErrorClass.PERMANENT,
                )

        template = template if isinstance(template, dict) else {}
        html = str(template.get("html") or "") or None
        text = str(template.get("text") or body or "")
        extra_headers = template.get("headers") if isinstance(template.get("headers"), dict) else {}

        sender_domain = sender.rsplit("@", 1)[-1]
        message_id = build_message_id(sender_domain)
        headers_payload = {"Message-ID": message_id, "X-QBIT-Idempotency-Key": idempotency_key[:200]}
        for key, value in (extra_headers or {}).items():
            if header_safe(key) and header_safe(value) and str(key).lower() not in (
                "from", "to", "cc", "bcc",
            ):
                headers_payload[str(key)] = str(value)

        payload: dict = {
            "from": {"email": sender, **({"name": sender_name} if sender_name else {})},
            # §40: individual recipient delivery — a one-element list, never CC
            "to": [{"email": recipient_address.strip()}],
            "subject": str(subject or ""),
            "text": text,
            "headers": headers_payload,
        }
        if html:
            payload["html"] = html
        if reply_to:
            payload["reply_to"] = reply_to
        if region:
            payload["region"] = region

        request_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key[:200],
        }
        transport = self._transport_factory(api_key) if self._transport_factory else None
        try:
            async with httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_SECONDS, transport=transport, follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{base}/messages", json=payload, headers=request_headers,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            normalized = self.errors.normalize_api(exception=exc)
            return SendResult(
                ok=False, error=normalized.message[:500], error_code=normalized.code,
                error_class=normalized.error_class, status="FAILED",
                metadata={"provider": self.provider_id,
                          "retry_after_seconds": normalized.retry_after_seconds},
            )

        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {"raw": str(data)[:200]}

        if 200 <= response.status_code < 300:
            provider_message_id = str(data.get("id") or "").strip() or message_id
            return SendResult.success(
                provider_message_id,
                status="SENT",
                metadata={
                    "provider": self.provider_id,
                    "http_status": response.status_code,
                    "region": region or None,
                },
            )
        normalized = self.errors.normalize_api(status_code=response.status_code, payload=data)
        return SendResult(
            ok=False, error=normalized.message[:500], error_code=normalized.code,
            error_class=normalized.error_class, status="FAILED",
            metadata={
                "provider": self.provider_id,
                "http_status": response.status_code,
                "retry_after_seconds": normalized.retry_after_seconds,
            },
        )

    # ---------------------------------------------------------------- health
    async def health_check(self, account_config: dict, credentials: dict | None = None) -> dict:
        """§7 probe: authenticated GET {base}/ping (or 401/404 detection)."""
        config = account_config if isinstance(account_config, dict) else {}
        checked_at = datetime.now(timezone.utc).isoformat()
        base = str(config.get("api_base_url") or "").strip().rstrip("/")
        api_key = str((credentials or {}).get("api_key") or "").strip()
        if not base or not api_key:
            return {"health": "UNHEALTHY", "checked_at": checked_at,
                    "detail": "api_base_url / api_key are not configured"}
        transport = self._transport_factory(api_key) if self._transport_factory else None
        try:
            async with httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_SECONDS, transport=transport, follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"{base}/ping",
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            normalized = self.errors.normalize_api(exception=exc)
            return {"health": "UNHEALTHY", "checked_at": checked_at,
                    "detail": f"{normalized.code}: {normalized.message[:300]}"}
        if response.status_code == 200:
            return {"health": "HEALTHY", "checked_at": checked_at,
                    "detail": {"api_base_url": base}}
        if response.status_code in (401, 403):
            return {"health": "UNHEALTHY", "checked_at": checked_at,
                    "detail": "AUTHENTICATION_ERROR: credentials rejected by API"}
        normalized = self.errors.normalize_api(status_code=response.status_code)
        return {"health": "UNHEALTHY", "checked_at": checked_at,
                "detail": f"{normalized.code}: {normalized.message[:300]}"}

    # ---------------------------------------------------- account validation
    async def validate_account(self, account_config: dict, credentials: dict | None = None) -> dict:
        """§7 connection flow: configuration → sender → credentials → API auth."""
        config = account_config if isinstance(account_config, dict) else {}
        checked_at = datetime.now(timezone.utc).isoformat()
        steps: dict[str, dict] = {}

        base = str(config.get("api_base_url") or "").strip()
        sender = str(config.get("sender_email") or "").strip()
        reply_to = str(config.get("reply_to") or "").strip()
        steps["configuration"] = {
            "ok": bool(base and base.startswith(("http://", "https://"))),
            "detail": base or "api_base_url missing or invalid",
        }
        steps["sender_email"] = {
            "ok": bool(sender and EMAIL_RE.match(sender)),
            "detail": sender or "sender_email missing or invalid",
        }
        if reply_to:
            steps["reply_to"] = {
                "ok": bool(EMAIL_RE.match(reply_to)),
                "detail": reply_to if EMAIL_RE.match(reply_to) else "reply_to invalid",
            }
        api_key = str((credentials or {}).get("api_key") or "").strip()
        steps["credentials"] = {
            "ok": bool(api_key),
            "detail": "api key present" if api_key else "api_key missing",
        }
        if not (base and sender and api_key):
            return {"ok": False, "checked_at": checked_at, "steps": steps}

        transport = self._transport_factory(api_key) if self._transport_factory else None
        try:
            async with httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_SECONDS, transport=transport, follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"{base.rstrip('/')}/ping",
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                )
            if response.status_code == 200:
                steps["api_auth"] = {"ok": True, "detail": "credentials accepted by API"}
            elif response.status_code in (401, 403):
                steps["api_auth"] = {"ok": False, "detail": "credentials rejected by API"}
            else:
                steps["api_auth"] = {
                    "ok": False,
                    "detail": f"unexpected API response ({response.status_code})",
                }
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            normalized = self.errors.normalize_api(exception=exc)
            steps["api_auth"] = {"ok": False, "detail": f"{normalized.code}: {normalized.message[:300]}"}

        return {
            "ok": all(step.get("ok") for step in steps.values()),
            "checked_at": checked_at, "steps": steps,
        }

    # ---------------------------------------------------------------- events
    async def handle_event(self, payload: dict) -> dict:
        """Normalize a pre-extracted provider event (internal ingestion path)."""
        return {
            "event_type": str(payload.get("event_type") or "").upper(),
            "provider_message_id": payload.get("provider_message_id"),
            "metadata": dict(payload.get("metadata") or {}),
        }


def decode_b64url(value: str) -> bytes:
    """Base64url decode without padding issues (tracking URL payloads)."""
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + pad).encode("ascii"))


def encode_b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_tracking_key() -> str:
    """Unguessable per-recipient tracking key (no DB ids in URLs, §29)."""
    return uuid.uuid4().hex + uuid.uuid4().hex[:16]


__all__ = [
    "GenericEmailAPIProvider",
    "build_message_id",
    "decode_b64url",
    "encode_b64url",
    "header_safe",
    "new_tracking_key",
]
