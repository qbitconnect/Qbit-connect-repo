"""WhatsApp Business Cloud API client (Phase 6 §1, §14).

Thin async wrapper over the OFFICIAL Graph API endpoints — no unofficial
endpoints, no WhatsApp Web, no session scraping, nothing evasive:

    GET  /{phone_number_id}          phone number + account health probe
    GET  /{business_account_id}      business account validation
    GET  /{waba_id}/message_templates   provider template catalog (§9)
    POST /{phone_number_id}/messages    template message send (§14)

Security:
- the access token is injected per request and NEVER logged, echoed, or
  written into error objects that survive the call
- network/HTTP failures are returned as (status, payload) tuples; the client
  never raises for provider-side errors — callers normalize them
"""

from __future__ import annotations

import httpx

DEFAULT_BASE_URL = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"
DEFAULT_TIMEOUT_SECONDS = 20.0
#: hard cap on template pages fetched during one sync (safety bound)
MAX_TEMPLATE_PAGES = 20
PAGE_LIMIT = 100


class WhatsAppCloudClient:
    """One client per call-site; construct per request/worker cycle."""

    def __init__(
        self, *,
        access_token: str,
        base_url: str = DEFAULT_BASE_URL,
        api_version: str = DEFAULT_API_VERSION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = access_token
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._version = (api_version or DEFAULT_API_VERSION).strip("/")
        self._timeout = timeout_seconds
        self._transport = transport  # test hook (httpx.MockTransport)

    # ------------------------------------------------------------------ core
    def _endpoint(self, path: str) -> str:
        return f"{self._base_url}/{self._version}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, url: str, *, json_body: dict | None = None) -> tuple[int, dict]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=False,
            ) as client:
                response = await client.request(method, url, headers=self._headers(), json=json_body)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"raw": response.text[:500]}
                return response.status_code, payload
        except httpx.TimeoutException:
            return 0, {"error": {"code": -1, "message": "Provider request timed out"}}
        except httpx.HTTPError:
            return 0, {"error": {"code": -2, "message": "Provider is unreachable"}}

    # --------------------------------------------------------------- accounts
    async def get_phone_number(self, phone_number_id: str) -> tuple[int, dict]:
        """Phone number + quality probe (health check + connection validation)."""
        fields = "id,display_phone_number,verified_name,quality_rating,platform_type,code_verification_status"
        return await self._request("GET", self._endpoint(f"{phone_number_id}?fields={fields}"))

    async def get_business_account(self, business_account_id: str) -> tuple[int, dict]:
        fields = "id,name,account_review_status,business_verification_status,messaging_limit_tier"
        return await self._request("GET", self._endpoint(f"{business_account_id}?fields={fields}"))

    # -------------------------------------------------------------- templates
    async def get_message_templates(self, business_account_id: str) -> tuple[int, list[dict]]:
        """Fetch ALL template pages (bounded). Returns (status_code, items)."""
        fields = "id,name,status,category,language,components,rejected_reason"
        url = self._endpoint(
            f"{business_account_id}/message_templates?fields={fields}&limit={PAGE_LIMIT}"
        )
        items: list[dict] = []
        status_code = 0
        for _ in range(MAX_TEMPLATE_PAGES):
            status_code, payload = await self._request("GET", url)
            if status_code != 200 or not isinstance(payload, dict):
                return status_code, []
            data = payload.get("data")
            if isinstance(data, list):
                items.extend([entry for entry in data if isinstance(entry, dict)])
            next_url = (payload.get("paging") or {}).get("next")
            if not next_url:
                break
            url = next_url
        return status_code, items

    # ------------------------------------------------------------------ send
    async def send_template(
        self, *, phone_number_id: str, to: str, template_name: str,
        language: str, components: list[dict],
    ) -> tuple[int, dict]:
        """Official template message send (business-initiated messaging REQUIRES
        a provider-approved template — the client refuses to send anything else)."""
        if not template_name:
            return 400, {"error": {"code": 400, "message": "template_name is required"}}
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language or "en"},
                "components": components,
            },
        }
        return await self._request("POST", self._endpoint(f"{phone_number_id}/messages"), json_body=body)

    async def send_session_text(
        self, *, phone_number_id: str, to: str, body: str,
    ) -> tuple[int, dict]:
        """Official free-text send INSIDE the 24h customer-service window
        (Phase 8 §21–§22 — inbox replies only; business-initiated campaign
        sends still REQUIRE templates). Callers must enforce the window
        BEFORE invoking this; the client refuses obviously invalid input."""
        if not body or not body.strip():
            return 400, {"error": {"code": 400, "message": "text body is required"}}
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body[:4096]},
        }
        return await self._request("POST", self._endpoint(f"{phone_number_id}/messages"), json_body=payload)
