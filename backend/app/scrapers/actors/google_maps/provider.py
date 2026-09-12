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
from urllib.parse import urlencode

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
    if kind == "outscraper":
        if not settings.QBIT_MAPS_PROVIDER_API_KEY:
            raise ScraperConfigurationError(
                "QBIT_MAPS_PROVIDER=outscraper requires QBIT_MAPS_PROVIDER_API_KEY"
            )
        base_url = (settings.QBIT_MAPS_PROVIDER_URL or "").strip() or OutscraperMapsProvider.DEFAULT_ENDPOINT
        return OutscraperMapsProvider(
            base_url=base_url,
            api_key=settings.QBIT_MAPS_PROVIDER_API_KEY,
        )
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


class OutscraperMapsProvider:
    """Compliant Outscraper Google Maps API v3 provider adapter."""

    name = "outscraper"
    DEFAULT_ENDPOINT = "https://api.app.outscraper.com/maps/search-v3"

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()

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
        # Outscraper pagination uses skip offset
        skip = 0
        if page_token:
            try:
                skip = max(0, int(page_token))
            except (ValueError, TypeError):
                skip = 0

        # Construct full query string if location components provided
        loc_parts = [p.strip() for p in (city, state, country) if p and p.strip()]
        full_query = query.strip()
        if loc_parts and not any(p.lower() in full_query.lower() for p in loc_parts):
            full_query = f"{full_query}, {', '.join(loc_parts)}"

        limit = min(max_results, PROVIDER_PAGE_SIZE)
        headers = {
            "X-API-KEY": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }
        params: dict[str, str] = {
            "query": full_query,
            "limit": str(limit),
            "skip": str(skip),
            "async": "false",
        }
        if language:
            params["language"] = language
        if country:
            params["region"] = country

        url = f"{self.base_url}?{urlencode(params)}"
        try:
            resp = await http.get(url, headers=headers)
        except Exception as exc:
            raise ScraperProviderError(f"Outscraper connection failed: {exc}", retryable=True)

        if resp.status_code == 401:
            raise ScraperProviderError("Outscraper API authentication failed (invalid or missing API key)", retryable=False)
        if resp.status_code == 402:
            raise ScraperProviderError("Outscraper account quota exhausted or payment required", retryable=False)
        if resp.status_code == 429:
            raise ScraperProviderError("Outscraper rate limit exceeded", retryable=True)
        if resp.status_code >= 500:
            raise ScraperProviderError(f"Outscraper server error (HTTP {resp.status_code})", retryable=True)
        if resp.status_code >= 400:
            raise ScraperProviderError(f"Outscraper request failed (HTTP {resp.status_code}): {resp.text[:200]}", retryable=False)

        try:
            payload = resp.json()
        except Exception as exc:
            raise ScraperProviderError(f"Outscraper returned invalid JSON: {exc}", retryable=False)

        # Outscraper returns {"status": "Success", "data": [[place1, place2, ...]]}
        # Or {"data": [place1, place2, ...]} if dropDuplicates or single array format
        raw_places: list[dict] = []
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                if len(data) > 0 and isinstance(data[0], list):
                    raw_places = [item for item in data[0] if isinstance(item, dict)]
                elif len(data) > 0 and isinstance(data[0], dict):
                    raw_places = [item for item in data if isinstance(item, dict)]
            elif isinstance(payload.get("results"), list):
                raw_places = [item for item in payload["results"] if isinstance(item, dict)]

        results: list[dict] = []
        for place in raw_places:
            email = place.get("email") or place.get("email_1")
            if not email and isinstance(place.get("emails"), list) and place["emails"]:
                email = str(place["emails"][0])

            item: dict = {
                "business_name": place.get("name") or place.get("business_name") or "",
                "category": place.get("type") or place.get("category"),
                "phone": place.get("phone"),
                "email": email,
                "website": place.get("site") or place.get("website"),
                "address": place.get("full_address") or place.get("address"),
                "city": place.get("city") or city,
                "state": place.get("state") or state,
                "country": place.get("country") or country,
                "rating": place.get("rating"),
                "review_count": place.get("reviews") or place.get("review_count"),
                "source_url": place.get("location_link") or place.get("source_url"),
                "metadata": {
                    "provider": "outscraper",
                    "place_id": place.get("place_id"),
                    "google_id": place.get("google_id"),
                    "subtypes": place.get("subtypes"),
                },
            }
            results.append(item)

        next_page_token: str | None = None
        if len(raw_places) >= limit:
            next_page_token = str(skip + len(raw_places))

        return results, next_page_token


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
        # URL-encode every parameter: spaces/&/# in the query previously
        # corrupted the request and allowed parameter injection.
        query_string = urlencode(params)
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
