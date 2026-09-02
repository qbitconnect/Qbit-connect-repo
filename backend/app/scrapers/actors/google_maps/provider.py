"""MapsProvider abstraction (brief §28, §47).

QBIT does NOT scrape Google Maps directly and NEVER implements CAPTCHA
bypass, anti-bot evasion, stealth fingerprinting or any mechanism designed to
defeat platform restrictions (§17, §28, §55). Instead the actor consumes a
pluggable provider:

    MapsProvider (Protocol)
        ├── HttpMapsProvider  — operator-configured compliant API endpoint
        │                       (e.g. a licensed maps-data vendor). Config:
        │                       QBIT_MAPS_PROVIDER=http
        │                       QBIT_MAPS_PROVIDER_URL=https://.../search
        │                       QBIT_MAPS_PROVIDER_API_KEY=*** (env only)
        ├── MockMapsProvider  — deterministic fixture data, TESTS/DEV ONLY.
        │                       Refused in production at config validation.
        └── (future providers: implement the Protocol + register here)

Provider response contract (JSON array of raw items):
    { "business_name": str, "category": str?, "phone": str?, "website": str?,
      "address": str?, "city": str?, "state": str?, "country": str?,
      "rating": float?, "review_count": int?, "source_url": str? }

The provider URL must be a public http(s) endpoint (netguard-validated) and is
called with the configured API key via the `Authorization: Bearer` header.
Pagination follows a simple `page_token` convention so checkpoints work.
"""

from __future__ import annotations

from typing import Protocol

from app.core.logging import get_logger
from app.scrapers.core.exceptions import ScraperConfigurationError, ScraperProviderError

logger = get_logger("qbit.scrapers.maps_provider")

PROVIDER_PAGE_SIZE = 20


class MapsProvider(Protocol):
    name: str

    async def search(
        self,
        *,
        query: str,
        city: str | None,
        state: str | None,
        country: str | None,
        language: str | None,
        page_token: str | None,
        max_results: int,
        http,  # PolicyHttpClient
    ) -> tuple[list[dict], str | None]:
        """One provider page: (raw_items, next_page_token)."""
        ...


def build_maps_provider(settings) -> MapsProvider | None:
    """Factory from Settings. None = no provider configured (health DEGRADED)."""
    kind = (settings.QBIT_MAPS_PROVIDER or "none").strip().lower()
    if kind in ("", "none"):
        return None
    if kind == "mock":
        from app.scrapers.actors.google_maps.mock_provider import MockMapsProvider

        return MockMapsProvider()
    if kind == "http":
        if not settings.QBIT_MAPS_PROVIDER_URL:
            raise ScraperConfigurationError(
                "QBIT_MAPS_PROVIDER=http requires QBIT_MAPS_PROVIDER_URL"
            )
        return HttpMapsProvider(
            settings.QBIT_MAPS_PROVIDER_URL,
            api_key=settings.QBIT_MAPS_PROVIDER_API_KEY,
        )
    raise ScraperConfigurationError(f"Unknown QBIT_MAPS_PROVIDER: {kind!r}")


class HttpMapsProvider:
    """Operator-configured compliant HTTP provider (licensed vendor/API)."""

    name = "http"

    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def search(
        self,
        *,
        query: str,
        city: str | None,
        state: str | None,
        country: str | None,
        language: str | None,
        page_token: str | None,
        max_results: int,
        http,
    ) -> tuple[list[dict], str | None]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        params: dict[str, str] = {
            "q": query,
            "page_size": str(min(max_results, PROVIDER_PAGE_SIZE)),
        }
        for key, value in (
            ("city", city), ("state", state), ("country", country),
            ("language", language), ("page_token", page_token),
        ):
            if value:
                params[key] = value
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        try:
            payload = await http.get_json(f"{self.base_url}?{query_string}", headers=headers)
        except ScraperProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalized below
            raise ScraperProviderError(f"Maps provider request failed: {exc}", retryable=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ScraperProviderError(
                "Maps provider returned an unexpected payload shape (expected "
                "{'results': [...], 'next_page_token': str?})",
                retryable=False,
            )
        return payload["results"], payload.get("next_page_token")


# NOTE: MockMapsProvider lives in mock_provider.py and is importable ONLY for
# tests/development — build_maps_provider refuses it in production
# (config.validate_runtime) and it is never registered by default.
