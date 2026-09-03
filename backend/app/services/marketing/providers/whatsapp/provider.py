"""WhatsAppProvider — official WhatsApp Business Cloud API adapter (Phase 6 §1).

Responsibilities (and NOTHING else — campaign logic stays in CampaignService):
- configuration validation (structural, never echoes secrets)
- recipient validation (strict E.164 — §11)
- account validation (the connection flow: credentials → phone → WABA, §5)
- template send via the official /messages endpoint (§14, §28 text-template)
- provider response normalization into SendResult (no secrets in result)
- template catalog synchronization (§9) + requirement validation (§10)
- health probe (§7)
- webhook payload/event normalization helpers (§17)

Compliance: the adapter uses ONLY provider-supported APIs. If the provider
rejects an operation, the real error is surfaced (sanitized) — there is no
retry-hiding, no window evasion, no automation of any kind.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.core.logging import get_logger
from app.services.marketing.phone import normalize_recipient_phone
from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    ErrorClass,
    ProviderError,
    SendResult,
)
from app.services.marketing.providers.whatsapp.client import (
    DEFAULT_API_VERSION,
    DEFAULT_BASE_URL,
    WhatsAppCloudClient,
)
from app.services.marketing.providers.whatsapp.errors import (
    TEMPLATE_NOT_APPROVED,
    WhatsAppErrorNormalizer,
)

logger = get_logger("qbit.marketing.whatsapp")

PLACEHOLDER_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")

#: platform capability surface advertised by this adapter (§27)
DEFAULT_CAPABILITIES = {
    "supports_templates": True,
    "supports_media": False,     # future capability — not implemented in Phase 6
    "supports_inbound": True,
    "supports_webhooks": True,
}

USABLE_PROVIDER_STATUSES = {"APPROVED"}


def count_placeholders(components: list[dict]) -> dict[str, int]:
    """Count {{N}} placeholders per component type (BODY/HEADER)."""
    counts = {"body": 0, "header": 0}
    for component in components or []:
        ctype = str(component.get("type") or "").lower()
        if ctype in counts:
            text = str(component.get("text") or "")
            counts[ctype] += len(PLACEHOLDER_RE.findall(text))
    return counts


def normalize_provider_template(raw: dict) -> dict | None:
    """Graph API template entry → QBIT's normalized template shape (§9)."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    components = raw.get("components") if isinstance(raw.get("components"), list) else []
    return {
        "provider_template_id": str(raw.get("id") or "").strip() or None,
        "name": name,
        "provider_status": str(raw.get("status") or "").strip().upper() or None,
        "category": str(raw.get("category") or "").strip().upper() or None,
        "language": str(raw.get("language") or "en").strip(),
        "components": {
            "raw": components,
            "placeholders": count_placeholders(components),
        },
        "rejected_reason": (str(raw.get("rejected_reason") or "").strip() or None),
    }


class WhatsAppProvider(BaseMarketingProvider):
    """Official WhatsApp Business Cloud API adapter (multi-account: ALL account
    context arrives per call — one class serves every sending account)."""

    provider_id = "whatsapp_cloud"
    channel = "WHATSAPP"
    interface_only = False

    def __init__(self, *, transport_factory=None, error_normalizer: WhatsAppErrorNormalizer | None = None) -> None:
        # transport_factory(access_token) -> httpx.AsyncBaseTransport — TEST HOOK
        self._transport_factory = transport_factory
        self.errors = error_normalizer or WhatsAppErrorNormalizer()

    # ------------------------------------------------------- configuration
    async def validate_configuration(self, config: dict) -> list[str]:
        config = config if isinstance(config, dict) else {}
        if not config.get("configured"):
            return ["Account is not configured — complete the connection wizard"]
        problems: list[str] = []
        if not str(config.get("phone_number_id") or "").strip():
            problems.append("phone_number_id is required")
        if not (str(config.get("credential_ref") or "").strip() or config.get("allow_env_credentials")):
            problems.append(
                "No credential reference — store the access token in the encrypted vault"
            )
        if config.get("api_base_url") and not str(config["api_base_url"]).startswith(("http://", "https://")):
            problems.append("api_base_url must be an http(s) URL")
        return problems

    async def validate_recipient(self, address: str) -> bool:
        ok, _normalized, _reason = normalize_recipient_phone(address)
        return ok

    async def validate_message(self, *, subject: str | None, body: str) -> list[str]:
        problems: list[str] = []
        if subject:
            problems.append("WhatsApp template messages do not use a subject")
        if len(body or "") > 4096:
            problems.append("WhatsApp message exceeds 4096 characters")
        return problems

    # ------------------------------------------------------------- template
    async def validate_send_requirements(self, *, template, account_config: dict) -> list[str]:
        """§8/§10 gate: WhatsApp business-initiated messages MUST use a
        provider-approved template. Returns a problem list (empty = usable)."""
        problems: list[str] = []
        config = account_config if isinstance(account_config, dict) else {}
        if not str(config.get("phone_number_id") or "").strip():
            problems.append("Sending account has no phone_number_id configured")
        if getattr(template, "origin", None) != "PROVIDER":
            problems.append(
                "WhatsApp campaigns must use a provider-synced template "
                "(run 'Sync templates' on the sending account)"
            )
            return problems
        if not template.provider_template_id:
            problems.append("Template has no provider template id")
        status = (template.provider_status or "").upper()
        if status not in USABLE_PROVIDER_STATUSES:
            reason = f" Provider says: {template.rejected_reason or 'n/a'}" if template.rejected_reason else ""
            problems.append(
                f"Provider template status is {status or 'UNKNOWN'} — only APPROVED templates "
                f"can be used for WhatsApp campaigns.{reason}"
            )
        placeholders = ((template.components or {}).get("placeholders") or {})
        required = int(placeholders.get("body", 0)) + int(placeholders.get("header", 0))
        declared = len(template.variables or [])
        if required != declared:
            problems.append(
                f"Template requires {required} variable(s) in order "
                f"(body {placeholders.get('body', 0)} + header {placeholders.get('header', 0)}) "
                f"but declares {declared}: {list(template.variables or [])}"
            )
        return problems

    def build_template_payload(self, template, lead) -> tuple[dict | None, list[str]]:
        """Render the provider template payload for ONE recipient.

        Returns (payload, missing_variables). payload is None when a required
        variable has no value on the lead — the recipient is skipped honestly
        instead of sending an incomplete template (providers reject those with
        a permanent error anyway).
        """
        placeholders = ((template.components or {}).get("placeholders") or {})
        variables = list(template.variables or [])
        components: list[dict] = []
        missing: list[str] = []

        def _values_for(count: int) -> tuple[list[dict] | None, list[str]]:
            params: list[dict] = []
            missing_here: list[str] = []
            for name in variables[:count]:
                value = getattr(lead, name, None)
                if value is None or not str(value).strip():
                    missing_here.append(name)
                    continue
                params.append({"type": "text", "text": str(value).strip()})
            if missing_here:
                return None, missing_here
            return params, []

        body_count = int(placeholders.get("body", 0))
        header_count = int(placeholders.get("header", 0))
        if header_count:
            params, miss = _values_for(header_count)
            if params is None:
                return None, miss
            components.append({"type": "header", "parameters": params})
        if body_count:
            params, miss = _values_for(body_count)
            if params is None:
                return None, miss
            components.append({"type": "body", "parameters": params})

        return {
            "provider_template_name": template.name,
            "provider_template_id": template.provider_template_id,
            "language": template.language,
            "components": components,
        }, missing

    # ------------------------------------------------------------------ send
    async def send(
        self, *, account_config: dict, recipient_address: str,
        subject: str | None, body: str, idempotency_key: str,
        metadata: dict | None = None, credentials: dict | None = None,
        template: dict | None = None,
    ) -> SendResult:
        config = account_config if isinstance(account_config, dict) else {}
        try:
            client = self._client(config, credentials)
        except ProviderError as exc:
            return SendResult.failure(str(exc), code=exc.code, error_class=exc.error_class)

        ok, e164, reason = normalize_recipient_phone(recipient_address)
        if not ok:
            return SendResult.failure(
                f"Recipient phone is invalid ({reason})",
                code="INVALID_RECIPIENT", error_class=ErrorClass.PERMANENT,
            )
        if template is None or not template.get("provider_template_name"):
            # whatsapp_cloud is template-only for business-initiated messages
            return SendResult.failure(
                "WhatsApp Business sending requires a provider template payload",
                code="INVALID_TEMPLATE", error_class=ErrorClass.PERMANENT,
            )

        status_code, payload = await client.send_template(
            phone_number_id=str(config.get("phone_number_id")),
            to=e164,
            template_name=str(template["provider_template_name"]),
            language=str(template.get("language") or "en"),
            components=list(template.get("components") or []),
        )

        if status_code == 200:
            messages = payload.get("messages") or []
            first = messages[0] if isinstance(messages, list) and messages else {}
            provider_message_id = str(first.get("id") or "").strip() or None
            provider_status = str(first.get("message_status") or "accepted")
            contacts = payload.get("contacts") or []
            wa_id = str(contacts[0].get("wa_id") or "") if isinstance(contacts, list) and contacts else ""
            return SendResult.success(
                provider_message_id,
                status="SENT" if provider_status in ("accepted", "sent") else provider_status.upper(),
                metadata={
                    "provider_status": provider_status,
                    "wa_id": wa_id,
                    "provider_message_id": provider_message_id,
                },
            )

        normalized = self.errors.normalize(status_code=status_code, payload=payload)
        return SendResult(
            ok=False,
            error=normalized.message[:500],
            error_code=normalized.code,
            error_class=normalized.error_class,
            status="FAILED",
            metadata={
                "provider_code": normalized.provider_code,
                "provider_subcode": normalized.provider_subcode,
                "detail": normalized.detail,
                "retry_after_seconds": normalized.retry_after_seconds,
            },
        )

    # ---------------------------------------------------------------- health
    async def health_check(self, account_config: dict, credentials: dict | None = None) -> dict:
        """§7 probe: credentials + availability + phone configuration.
        Response contains ONLY sanitized fields (no tokens, no raw errors)."""
        config = account_config if isinstance(account_config, dict) else {}
        checked_at = datetime.now(timezone.utc).isoformat()
        phone_number_id = str(config.get("phone_number_id") or "").strip()
        if not phone_number_id:
            return {"health": "UNHEALTHY", "checked_at": checked_at,
                    "detail": "phone_number_id is not configured"}
        try:
            client = self._client(config, credentials)
        except ProviderError as exc:
            return {"health": "UNHEALTHY", "checked_at": checked_at, "detail": str(exc)}

        status_code, payload = await client.get_phone_number(phone_number_id)
        if status_code != 200:
            normalized = self.errors.normalize(status_code=status_code, payload=payload)
            return {
                "health": "UNHEALTHY",
                "checked_at": checked_at,
                "detail": f"{normalized.code}: {normalized.message[:300]}",
            }
        data = payload if isinstance(payload, dict) else {}
        quality = str(data.get("quality_rating") or "").upper()
        health = {"GREEN": "HEALTHY", "YELLOW": "DEGRADED", "RED": "UNHEALTHY"}.get(
            quality, "DEGRADED" if quality == "" else "DEGRADED"
        )
        return {
            "health": health,
            "checked_at": checked_at,
            "detail": {
                "quality_rating": quality or "UNKNOWN",
                "verified_name": data.get("verified_name"),
                "display_phone_number_masked": self._mask(data.get("display_phone_number")),
                "platform_type": data.get("platform_type"),
            },
        }

    # ---------------------------------------------------- account validation
    async def validate_account(self, account_config: dict, credentials: dict | None = None) -> dict:
        """Connection-flow validation (§5): credentials → phone number →
        business account → permissions. Never marks anything ACTIVE — the
        caller decides based on the returned step results."""
        config = account_config if isinstance(account_config, dict) else {}
        checked_at = datetime.now(timezone.utc).isoformat()
        steps: dict[str, dict] = {}

        token = str((credentials or {}).get("access_token") or "")
        steps["credentials"] = {
            "ok": bool(token),
            "detail": "access token present" if token else "no access token available",
        }
        phone_number_id = str(config.get("phone_number_id") or "").strip()
        steps["phone_number_configured"] = {
            "ok": bool(phone_number_id),
            "detail": phone_number_id or "phone_number_id missing",
        }
        if not token or not phone_number_id:
            return {"ok": False, "checked_at": checked_at, "steps": steps}

        client = self._client(config, credentials)
        phone_status, payload = await client.get_phone_number(phone_number_id)
        if phone_status == 200:
            data = payload if isinstance(payload, dict) else {}
            steps["phone_number_valid"] = {
                "ok": True,
                "detail": {
                    "display_phone_number_masked": self._mask(data.get("display_phone_number")),
                    "verified_name": data.get("verified_name"),
                    "quality_rating": data.get("quality_rating"),
                },
            }
            steps["permissions"] = {"ok": True, "detail": "phone number readable with this token"}
            if config.get("business_account_id"):
                waba_status, waba_payload = await client.get_business_account(
                    str(config["business_account_id"])
                )
                if waba_status == 200:
                    waba = waba_payload if isinstance(waba_payload, dict) else {}
                    steps["business_account"] = {
                        "ok": True,
                        "detail": {"name": waba.get("name"),
                                   "business_verification_status": waba.get("business_verification_status"),
                                   "messaging_limit_tier": waba.get("messaging_limit_tier")},
                    }
                else:
                    normalized = self.errors.normalize(status_code=waba_status, payload=waba_payload)
                    steps["business_account"] = {"ok": False,
                                                 "detail": f"{normalized.code}: {normalized.message[:300]}"}
            else:
                steps["business_account"] = {"ok": True, "detail": "not configured (optional)"}
        else:
            normalized = self.errors.normalize(status_code=phone_status, payload=payload)
            steps["phone_number_valid"] = {
                "ok": False,
                "detail": f"{normalized.code}: {normalized.message[:300]}",
            }
            if normalized.code == "AUTHENTICATION_ERROR":
                steps["permissions"] = {"ok": False, "detail": "token rejected by provider"}

        return {
            "ok": all(step.get("ok") for step in steps.values()),
            "checked_at": checked_at,
            "steps": steps,
        }

    # ------------------------------------------------------------ templates
    async def fetch_templates(self, account_config: dict, credentials: dict | None = None) -> list[dict]:
        """Provider template catalog (§9). Raises ProviderError on failure."""
        config = account_config if isinstance(account_config, dict) else {}
        waba_id = str(config.get("business_account_id") or "").strip()
        if not waba_id:
            raise ProviderError(
                "business_account_id is required to synchronize templates",
                error_class=ErrorClass.CONFIGURATION, code="BUSINESS_ACCOUNT_MISSING",
            )
        client = self._client(config, credentials)
        status, items = await client.get_message_templates(waba_id)
        if status != 200:
            normalized = self.errors.normalize(status_code=status, payload={"error": {"message": "template fetch failed"}} if status == 0 else items)
            raise ProviderError(
                f"{normalized.code}: {normalized.message}"[:400],
                error_class=normalized.error_class, code=normalized.code,
            )
        normalized_rows = []
        for raw in items:
            row = normalize_provider_template(raw)
            if row is not None:
                normalized_rows.append(row)
        return normalized_rows

    # ---------------------------------------------------------- webhooks
    async def handle_event(self, payload: dict) -> dict:
        """Normalize a pre-extracted provider event (internal ingestion path)."""
        event_type = str(payload.get("event_type") or "").upper()
        return {
            "event_type": event_type,
            "provider_message_id": payload.get("provider_message_id"),
            "metadata": dict(payload.get("metadata") or {}),
        }

    # ---------------------------------------------------------------- helpers
    def _client(self, config: dict, credentials: dict | None) -> WhatsAppCloudClient:
        token = str((credentials or {}).get("access_token") or "").strip()
        if not token:
            raise ProviderError(
                "No access token available for this sending account",
                error_class=ErrorClass.CONFIGURATION, code="CREDENTIALS_MISSING",
            )
        return WhatsAppCloudClient(
            access_token=token,
            base_url=str(config.get("api_base_url") or DEFAULT_BASE_URL),
            api_version=str(config.get("api_version") or DEFAULT_API_VERSION),
            transport=self._transport_factory(token) if self._transport_factory else None,
        )

    @staticmethod
    def _mask(value) -> str:
        raw = "".join(ch for ch in str(value or "") if ch.isdigit())
        if len(raw) < 6:
            return "\u2022\u2022\u2022"
        return f"+{raw[:2]}\u2022\u2022\u2022\u2022\u2022{raw[-3:]}"
