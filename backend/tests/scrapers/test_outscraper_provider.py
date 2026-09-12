"""Unit and contract tests for OutscraperMapsProvider (brief §28, §47).

Verifies OutscraperMapsProvider response parsing, error handling (401, 402, 429, 500),
pagination skip handling, URL queries, Source Lock guarantees, and end-to-end integration.
NO live external network requests are made; all tests exercise PolicyHttpClient with
MockTransport fixtures.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.config import Settings
from app.scrapers.actors.google_maps.actor import GoogleMapsActor
from app.scrapers.actors.google_maps.provider import OutscraperMapsProvider, build_maps_provider
from app.scrapers.core.context import JobLimits, ScraperContext
from app.scrapers.core.exceptions import ScraperConfigurationError, ScraperProviderError
from app.scrapers.core.http import HttpPolicy, PolicyHttpClient
from app.scrapers.core.netguard import UrlPolicy
from app.services.scraping.events import EventReporter
from app.services.scraping.progress import ProgressReporter


OUTSCRAPER_MOCK_DATA = {
    "status": "Success",
    "data": [
        [
            {
                "query": "sweets in Modinagar",
                "name": "Shri Ram Sweets",
                "place_id": "ChIJrc9T9fpYwokRdvjYRHT8nI4",
                "google_id": "0x886916e8bc273979:0x5141fcb11460b226",
                "type": "Sweet shop",
                "subtypes": "Confectionery, Candy store",
                "phone": "+91 98371 23456",
                "site": "https://shriramsweets.example.com",
                "full_address": "Bazaar, Modinagar, Uttar Pradesh 201204",
                "city": "Modinagar",
                "state": "Uttar Pradesh",
                "country": "India",
                "rating": 4.6,
                "reviews": 128,
                "location_link": "https://www.google.com/maps/place/Shri+Ram+Sweets/@28.83,77.58,14z",
                "email_1": "contact@shriramsweets.example.com",
            },
            {
                "query": "sweets in Modinagar",
                "name": "Bikaner Sweets Corner",
                "place_id": "ChIJ8ccnM7dbwokRy-pTMsdgvS4",
                "google_id": "0x886916e8bc273979:0x5141fcb11460b227",
                "type": "Sweet shop",
                "phone": "+91 98371 99999",
                "site": "https://bikanersweets.example.com",
                "full_address": "Main Road, Modinagar, Uttar Pradesh 201204",
                "city": "Modinagar",
                "state": "Uttar Pradesh",
                "country": "India",
                "rating": 4.2,
                "reviews": 45,
                "location_link": "https://www.google.com/maps/place/Bikaner+Sweets/@28.83,77.58,14z",
                "emails": ["info@bikanersweets.example.com"],
            },
        ]
    ],
}


def make_http_client(handler) -> PolicyHttpClient:
    policy = HttpPolicy(request_timeout=5.0, max_retries=0, respect_robots=False)
    url_policy = UrlPolicy(allow_private_targets=True, allowed_hosts={"api.app.outscraper.com", "api.outscraper.cloud", "fixture.test"})
    return PolicyHttpClient(policy, url_policy, transport=httpx.MockTransport(handler))


def make_ctx(actor_input: dict, client: PolicyHttpClient) -> ScraperContext:
    job_id = uuid.uuid4()
    ctx = ScraperContext(
        job_id=job_id,
        actor_id="google-maps",
        actor_version="1.0.0",
        attempt=1,
        input=actor_input,
        config={},
        limits=JobLimits(max_records=100, max_pages=10, max_runtime_seconds=60),
        http_policy=client.policy,
        url_policy=client.url_policy,
        http_transport=client._client._transport,
    )
    ctx._http = client
    return ctx


async def collect(actor: GoogleMapsActor, ctx: ScraperContext) -> list[dict]:
    items: list[dict] = []
    async for item in actor.run(ctx):
        items.append(item)
    return items


# ==============================================================================
# 1. Direct OutscraperMapsProvider Unit Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_outscraper_provider_search_success():
    """Verify provider parses Outscraper payload, extracts canonical fields, and sets next_page_token."""
    captured_req: dict[str, Any] = {}

    def handler(request: httpx.Request):
        captured_req["url"] = str(request.url)
        captured_req["headers"] = dict(request.headers)
        return httpx.Response(200, json=OUTSCRAPER_MOCK_DATA)

    client = make_http_client(handler)
    provider = OutscraperMapsProvider(
        base_url="https://api.app.outscraper.com/maps/search-v3",
        api_key="test-api-key-12345",
    )

    results, next_token = await provider.search(
        query="sweets shop",
        city="Modinagar",
        state="Uttar Pradesh",
        country="India",
        language="en",
        page_token=None,
        max_results=2,
        http=client,
    )

    assert len(results) == 2
    # Verify request headers
    assert captured_req["headers"].get("x-api-key") == "test-api-key-12345"
    assert "Bearer test-api-key-12345" in captured_req["headers"].get("authorization", "")

    # Verify query params
    qs = parse_qs(urlsplit(captured_req["url"]).query)
    assert qs["limit"] == ["2"]
    assert qs["skip"] == ["0"]
    assert qs["async"] == ["false"]
    assert "Modinagar" in qs["query"][0]

    # Verify field extraction
    r0 = results[0]
    assert r0["business_name"] == "Shri Ram Sweets"
    assert r0["category"] == "Sweet shop"
    assert r0["phone"] == "+91 98371 23456"
    assert r0["email"] == "contact@shriramsweets.example.com"
    assert r0["website"] == "https://shriramsweets.example.com"
    assert r0["address"] == "Bazaar, Modinagar, Uttar Pradesh 201204"
    assert r0["rating"] == 4.6
    assert r0["review_count"] == 128
    assert r0["metadata"]["provider"] == "outscraper"
    assert r0["metadata"]["place_id"] == "ChIJrc9T9fpYwokRdvjYRHT8nI4"

    # Second result emails array fallback
    r1 = results[1]
    assert r1["business_name"] == "Bikaner Sweets Corner"
    assert r1["email"] == "info@bikanersweets.example.com"

    # Next page token should be skip (0) + len (2) = "2"
    assert next_token == "2"


@pytest.mark.asyncio
async def test_outscraper_provider_pagination_cursor():
    """Verify page_token translates into skip offset."""
    captured_req: dict[str, Any] = {}

    def handler(request: httpx.Request):
        captured_req["url"] = str(request.url)
        return httpx.Response(200, json={"status": "Success", "data": [[]]})

    client = make_http_client(handler)
    provider = OutscraperMapsProvider(
        base_url="https://api.app.outscraper.com/maps/search-v3",
        api_key="test-key",
    )

    results, next_token = await provider.search(
        query="textiles",
        city=None,
        state=None,
        country=None,
        language=None,
        page_token="40",
        max_results=20,
        http=client,
    )

    assert results == []
    assert next_token is None
    qs = parse_qs(urlsplit(captured_req["url"]).query)
    assert qs["skip"] == ["40"]


# ==============================================================================
# 2. Error Handling Tests (401, 402, 429, 500)
# ==============================================================================
@pytest.mark.asyncio
async def test_outscraper_provider_401_unauthorized():
    def handler(request: httpx.Request):
        return httpx.Response(401, json={"message": "Invalid API key"})

    client = make_http_client(handler)
    provider = OutscraperMapsProvider("https://api.app.outscraper.com/maps/search-v3", "bad-key")

    with pytest.raises(ScraperProviderError) as exc_info:
        await provider.search(
            query="test", city=None, state=None, country=None, language=None,
            page_token=None, max_results=10, http=client,
        )
    assert exc_info.value.retryable is False
    assert "authentication failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_outscraper_provider_402_payment_required():
    def handler(request: httpx.Request):
        return httpx.Response(402, json={"message": "Out of balance"})

    client = make_http_client(handler)
    provider = OutscraperMapsProvider("https://api.app.outscraper.com/maps/search-v3", "key")

    with pytest.raises(ScraperProviderError) as exc_info:
        await provider.search(
            query="test", city=None, state=None, country=None, language=None,
            page_token=None, max_results=10, http=client,
        )
    assert exc_info.value.retryable is False
    assert "quota exhausted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_outscraper_provider_429_rate_limited():
    def handler(request: httpx.Request):
        return httpx.Response(429, json={"message": "Rate limit reached"})

    client = make_http_client(handler)
    provider = OutscraperMapsProvider("https://api.app.outscraper.com/maps/search-v3", "key")

    with pytest.raises(ScraperProviderError) as exc_info:
        await provider.search(
            query="test", city=None, state=None, country=None, language=None,
            page_token=None, max_results=10, http=client,
        )
    assert exc_info.value.retryable is True
    assert "rate limit" in str(exc_info.value)


@pytest.mark.asyncio
async def test_outscraper_provider_500_server_error():
    def handler(request: httpx.Request):
        return httpx.Response(500, text="Internal Server Error")

    client = make_http_client(handler)
    provider = OutscraperMapsProvider("https://api.app.outscraper.com/maps/search-v3", "key")

    with pytest.raises(ScraperProviderError) as exc_info:
        await provider.search(
            query="test", city=None, state=None, country=None, language=None,
            page_token=None, max_results=10, http=client,
        )
    assert exc_info.value.retryable is True
    assert "server error" in str(exc_info.value)


# ==============================================================================
# 3. Actor + Factory + Health Integration Tests
# ==============================================================================
def test_build_maps_provider_outscraper_requires_key():
    settings = Settings(QBIT_ENV="test", QBIT_MAPS_PROVIDER="outscraper", QBIT_MAPS_PROVIDER_API_KEY=None, _env_file=None)
    with pytest.raises(ScraperConfigurationError) as exc_info:
        build_maps_provider(settings)
    assert "requires QBIT_MAPS_PROVIDER_API_KEY" in str(exc_info.value)


def test_build_maps_provider_outscraper_configured():
    settings = Settings(
        QBIT_ENV="test",
        QBIT_MAPS_PROVIDER="outscraper",
        QBIT_MAPS_PROVIDER_API_KEY="my-secret-token",
        _env_file=None,
    )
    provider = build_maps_provider(settings)
    assert provider is not None
    assert provider.name == "outscraper"
    assert provider.api_key == "my-secret-token"
    assert provider.base_url == "https://api.app.outscraper.com/maps/search-v3"


@pytest.mark.asyncio
async def test_google_maps_actor_health_with_outscraper():
    settings = Settings(
        QBIT_ENV="test",
        QBIT_MAPS_PROVIDER="outscraper",
        QBIT_MAPS_PROVIDER_API_KEY="valid-key",
        _env_file=None,
    )
    actor = GoogleMapsActor(settings=settings)
    health = await actor.health_check()
    assert health.status.value == "READY"
    assert health.dependencies.get("maps_provider") == "outscraper"


@pytest.mark.asyncio
async def test_google_maps_actor_run_with_outscraper():
    """Verify GoogleMapsActor yields normalized items from Outscraper provider with source tags."""
    def handler(request: httpx.Request):
        return httpx.Response(200, json=OUTSCRAPER_MOCK_DATA)

    client = make_http_client(handler)
    settings = Settings(
        QBIT_ENV="test",
        QBIT_MAPS_PROVIDER="outscraper",
        QBIT_MAPS_PROVIDER_API_KEY="valid-key",
        _env_file=None,
    )
    actor = GoogleMapsActor(settings=settings)
    ctx = make_ctx({"query": "sweets in Modinagar", "max_results": 2}, client)

    items = await collect(actor, ctx)
    assert len(items) == 2
    for it in items:
        assert it["source"] == "google-maps"
        assert it.get("source_url")
        assert it.get("business_name")
        assert it["metadata"]["provider"] == "outscraper"


@pytest.mark.asyncio
async def test_google_maps_url_input_with_outscraper():
    """Verify that a full Google Maps URL search query works seamlessly with Outscraper provider."""
    captured_req: dict[str, Any] = {}

    def handler(request: httpx.Request):
        captured_req["url"] = str(request.url)
        return httpx.Response(200, json=OUTSCRAPER_MOCK_DATA)

    client = make_http_client(handler)
    settings = Settings(
        QBIT_ENV="test",
        QBIT_MAPS_PROVIDER="outscraper",
        QBIT_MAPS_PROVIDER_API_KEY="valid-key",
        _env_file=None,
    )
    actor = GoogleMapsActor(settings=settings)
    ctx = make_ctx(
        {
            "query": "https://www.google.com/maps/search/sweets+in+modinagar/@28.83,77.58,14z",
            "max_results": 2,
        },
        client,
    )

    items = await collect(actor, ctx)
    assert len(items) == 2
    assert "https%3A%2F%2Fwww.google.com%2Fmaps%2Fsearch" in captured_req["url"]


@pytest.mark.asyncio
async def test_outscraper_source_lock_strict_guarantee():
    """Verify Source Lock: when Outscraper fails, no fallback is triggered and error surfaces."""
    def handler(request: httpx.Request):
        return httpx.Response(500, text="Internal Server Error")

    client = make_http_client(handler)
    settings = Settings(
        QBIT_ENV="test",
        QBIT_MAPS_PROVIDER="outscraper",
        QBIT_MAPS_PROVIDER_API_KEY="valid-key",
        _env_file=None,
    )
    actor = GoogleMapsActor(settings=settings)
    ctx = make_ctx({"query": "shoe wholesalers", "max_results": 10}, client)

    with pytest.raises(ScraperProviderError) as exc_info:
        await collect(actor, ctx)
    assert "server error" in str(exc_info.value)


@pytest.mark.asyncio
async def test_outscraper_pipeline_lead_persisted_and_exported(app, tmp_path):
    """Verify Outscraper results flow through ResultPipeline into Leads DB and export cleanly."""
    from app.models.scrape import Lead
    from app.services.leads.exporter import LeadExportService
    from app.services.scraping.pipeline import ResultPipeline
    from app.services.scraping.result_files import JobResultFiles
    from sqlalchemy import select

    def handler(request: httpx.Request):
        return httpx.Response(200, json=OUTSCRAPER_MOCK_DATA)

    client = make_http_client(handler)
    settings = Settings(
        QBIT_ENV="test",
        QBIT_MAPS_PROVIDER="outscraper",
        QBIT_MAPS_PROVIDER_API_KEY="valid-key",
        _env_file=None,
    )
    actor = GoogleMapsActor(settings=settings)
    job_id = uuid.uuid4()
    ctx = make_ctx({"query": "sweets in Modinagar", "max_results": 2}, client)

    result_files = JobResultFiles(tmp_path / "results", "google-maps", str(job_id))
    session_factory = app.state.db.session
    async with session_factory() as db_session:
        pipeline = ResultPipeline(
            db_session,
            actor_id="google-maps",
            actor_version="1.0.0",
            job_id=job_id,
            result_files=result_files,
        )

        async for item in actor.run(ctx):
            await pipeline.add(item)
        await pipeline.finish()

        # Verify leads were inserted into DB
        res = await db_session.execute(
            select(Lead).where(Lead.source_job_id == job_id)
        )
        leads = res.scalars().all()
        assert len(leads) == 2
        names = {l.business_name for l in leads}
        assert "Shri Ram Sweets" in names
        assert "Bikaner Sweets Corner" in names

        # Verify CSV export generation for leads
        from app.services.leads.exporter import _CSVWriter, lead_to_row
        import io
        fields = ["business_name", "category", "phone", "email", "website", "address"]
        bio = io.BytesIO()
        writer = _CSVWriter(bio, fields)
        for l in leads:
            writer.write_row(lead_to_row(l, fields))
        writer.close()
        csv_text = bio.getvalue().decode("utf-8")
        assert "Shri Ram Sweets" in csv_text
        assert "Bikaner Sweets Corner" in csv_text
        assert "9837123456" in csv_text

