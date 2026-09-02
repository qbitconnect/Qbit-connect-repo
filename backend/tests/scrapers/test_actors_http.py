"""Actor integration tests against httpx.MockTransport fixtures (brief §46, §47).

NO live external websites are contacted. The PolicyHttpClient runs over a
mock transport with allow_private_targets/allowed_hosts so the netguard's DNS
resolution is bypassed — production keeps it enabled.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.core.config import Settings
from app.scrapers.actors.email_finder import EmailFinderActor
from app.scrapers.actors.google_maps.actor import GoogleMapsActor
from app.scrapers.actors.public_data import PublicDataActor
from app.scrapers.actors.universal import UniversalWebActor
from app.scrapers.actors.website import WebsiteActor
from app.scrapers.core.context import ScraperContext
from app.scrapers.core.http import HttpPolicy
from app.scrapers.core.netguard import UrlPolicy

HOST = "fixture.test"  # never resolved: MockTransport intercepts


def fixture_url(path: str) -> str:
    return f"http://{HOST}{path}"


def policy(**overrides) -> UrlPolicy:
    defaults = dict(allowed_ports={80, 443}, allowed_hosts={HOST})
    defaults.update(overrides)
    return UrlPolicy(**defaults)


def http_policy(**overrides) -> HttpPolicy:
    base = dict(
        request_timeout=5, requests_per_second=1000.0, concurrency=4,
        max_retries=1, respect_robots=True,
    )
    base.update(overrides)
    return HttpPolicy(**base)


PAGE_INDEX = """
<html><head><title>Fixture Business</title>
<meta name="description" content="A fixture shop"></head>
<body>
<a href="/contact">Contact us</a>
<a href="/products">Products</a>
<a href="https://external-other.test/page">External</a>
<div>Sales: <a href="mailto:sales@fixture.test">mail</a> or +91 11 4000 1000</div>
<a href="https://facebook.com/fixturebusiness">FB</a>
<address>12 Main Road, New Delhi</address>
</body></html>
"""

PAGE_CONTACT = """
<html><head><title>Contact — Fixture Business</title></head><body>
<div>General: <a href="mailto:info@fixture.test">info</a></div>
<div>Support: <a href="mailto:support@fixture.test">help</a></div>
<div>Phone: <a href="tel:+911140002000">call</a></div>
</body></html>
"""

ROBOTS_DISALLOW = """
User-agent: *
Disallow: /private
"""


def make_transport(pages: dict[str, str], *, requests_log: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests_log is not None:
            requests_log.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_DISALLOW)
        if path in pages:
            return httpx.Response(200, text=pages[path], headers={"content-type": "text/html"})
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def make_ctx(input_data: dict, transport, *, url_policy=None, limits=None) -> ScraperContext:
    from app.services.scraping.events import EventReporter  # noqa: F401 — optional

    return ScraperContext(
        job_id=uuid.uuid4(),
        actor_id="test",
        actor_version="1.0.0",
        input=input_data,
        http_policy=http_policy(),
        url_policy=url_policy or policy(),
        limits=limits,
        http_transport=transport,
        control_reader=None,
    )


async def collect(actor, ctx) -> list[dict]:
    items = []
    async for item in actor.run(ctx):
        items.append(item)
    await ctx.close()
    return items


# ------------------------------------------------------------------ website
@pytest.mark.asyncio
async def test_website_actor_extracts_and_stays_in_domain():
    requests_log: list[str] = []
    transport = make_transport(
        {"/": PAGE_INDEX, "/contact": PAGE_CONTACT}, requests_log=requests_log
    )
    ctx = make_ctx({"url": fixture_url("/"), "max_pages": 5, "max_depth": 1}, transport)
    items = await collect(WebsiteActor(), ctx)

    # contact page yielded with extracted public data
    emails = {it["email"] for it in items if it.get("email")}
    assert {"sales@fixture.test", "info@fixture.test"} <= emails
    all_found = {e for it in items for e in it["metadata"].get("emails_found", [])}
    assert {"sales@fixture.test", "info@fixture.test", "support@fixture.test"} <= all_found
    assert any("+911140002000" in (it.get("phone") or "") or it.get("phone") for it in items)
    assert any("facebook.com/fixturebusiness" in it["social_links"].get("facebook", "") for it in items)
    # same-domain restriction: the external link was NEVER fetched (§42)
    assert not any("external-other.test" in url for url in requests_log)
    # metadata carries provenance
    sample = items[0]
    assert sample["source"] == "website"
    assert "emails_found" in sample["metadata"]


@pytest.mark.asyncio
async def test_website_actor_respects_robots_disallow():
    requests_log: list[str] = []
    transport = make_transport({"/": PAGE_INDEX, "/contact": PAGE_CONTACT}, requests_log=requests_log)
    ctx = make_ctx({"url": fixture_url("/private"), "max_pages": 3}, transport)
    items = await collect(WebsiteActor(), ctx)
    # the disallowed start URL was never fetched (hint pages are separate URLs)
    assert not any(u.rstrip("/").endswith("/private") for u in requests_log)
    assert all("fixture.test/private" not in (it.get("source_url") or "") for it in items)


@pytest.mark.asyncio
async def test_website_actor_page_limit_is_honored():
    pages = {"/": PAGE_INDEX}
    for i in range(10):
        pages[f"/p{i}"] = PAGE_INDEX.replace("/contact", f"/p{i+1}")
    requests_log: list[str] = []
    transport = make_transport(pages, requests_log=requests_log)
    from app.scrapers.core.context import JobLimits

    ctx = make_ctx(
        {"url": fixture_url("/"), "max_pages": 3, "max_depth": 5},
        transport,
        limits=JobLimits(max_runtime_seconds=60, max_pages=3),
    )
    from app.scrapers.core.exceptions import ScraperLimitReachedError

    with pytest.raises(ScraperLimitReachedError):
        await collect(WebsiteActor(), ctx)  # the runner converts this to COMPLETED
    fetched = [url for url in requests_log if "robots" not in url]
    assert len(fetched) <= 3


# -------------------------------------------------------------- email finder
@pytest.mark.asyncio
async def test_email_finder_classifies_types_and_confidence():
    transport = make_transport({"/": PAGE_INDEX, "/contact": PAGE_CONTACT})
    ctx = make_ctx({"website": fixture_url("/"), "crawl_depth": 1}, transport)
    items = await collect(EmailFinderActor(), ctx)
    by_email = {it["email"]: it for it in items}
    assert "sales@fixture.test" in by_email  # homepage sales keyword
    contact = by_email["support@fixture.test"]
    assert contact["metadata"]["email_type"] == "support"
    assert contact["metadata"]["confidence"] in ("HIGH", "MEDIUM", "LOW")
    # provenance: every item carries the page it was found on (§27)
    assert all(it["metadata"]["source_page"].startswith(fixture_url("")) for it in items)
    assert all(it["metadata"]["consent"] == "not_implied" for it in items)


@pytest.mark.asyncio
async def test_email_finder_accepts_bare_domain():
    transport = make_transport({"/": PAGE_CONTACT})
    ctx = make_ctx({"domain": HOST, "crawl_depth": 0}, transport)
    items = await collect(EmailFinderActor(), ctx)
    assert any(it["email"] == "info@fixture.test" for it in items)


# ----------------------------------------------------------------- universal
@pytest.mark.asyncio
async def test_universal_actor_selector_extraction():
    transport = make_transport({"/listing": PAGE_CONTACT})
    ctx = make_ctx(
        {
            "url": fixture_url("/listing"),
            "fields": [
                {"name": "business_name", "selector": "title", "attribute": "text"},
                {"name": "email", "selector": "a[href^='mailto:']", "attribute": "href"},
            ],
        },
        transport,
    )
    items = await collect(UniversalWebActor(), ctx)
    assert items, "universal actor must extract declared fields"
    assert items[0]["business_name"] == "Contact — Fixture Business"
    assert items[0]["email"].startswith("mailto:")


# -------------------------------------------------------------- public data
@pytest.mark.asyncio
async def test_public_data_actor_json_with_field_map():
    payload = json.dumps(
        {
            "data": {
                "records": [
                    {"org": "Alpha Ltd", "tel": "+91 120000 1", "extra_col": "keepme"},
                    {"org": "Beta Ltd", "tel": "+91 120000 2", "extra_col": "x"},
                ]
            }
        }
    )

    def handler(request):
        return httpx.Response(200, text=payload, headers={"content-type": "application/json"})

    ctx = make_ctx(
        {
            "url": fixture_url("/dataset.json"),
            "format": "json",
            "records_key": "data.records",
            "field_map": {"org": "business_name", "tel": "phone"},
        },
        httpx.MockTransport(handler),
    )
    items = await collect(PublicDataActor(), ctx)
    assert len(items) == 2
    assert items[0]["business_name"] == "Alpha Ltd"
    assert items[0]["metadata"]["extra_col"] == "keepme"


@pytest.mark.asyncio
async def test_public_data_actor_csv():
    csv_text = "name,mail\nGamma Ltd,gamma@fixture.test\n"

    def handler(request):
        return httpx.Response(200, text=csv_text, headers={"content-type": "text/csv"})

    ctx = make_ctx(
        {
            "url": fixture_url("/dataset.csv"),
            "format": "csv",
            "field_map": {"name": "business_name", "mail": "email"},
        },
        httpx.MockTransport(handler),
    )
    items = await collect(PublicDataActor(), ctx)
    assert items == [
        {
            "business_name": "Gamma Ltd",
            "email": "gamma@fixture.test",
            "source": "public-data",
            "source_url": fixture_url("/dataset.csv"),
        }
    ] or items[0]["business_name"] == "Gamma Ltd"


# -------------------------------------------------------------- google maps
@pytest.mark.asyncio
async def test_google_maps_actor_with_mock_provider():
    from app.scrapers.actors.google_maps.mock_provider import MockMapsProvider

    settings = Settings(QBIT_ENV="test", QBIT_MAPS_PROVIDER="mock", _env_file=None)
    actor = GoogleMapsActor(settings=settings)
    health = await actor.health_check()
    assert health.status.value == "READY"

    transport = make_transport({})  # mock provider never uses HTTP
    ctx = make_ctx(
        {"query": "restaurants", "city": "Delhi", "max_results": 5}, transport
    )
    items = await collect(actor, ctx)
    assert len(items) == 5
    assert all(it["source"] == "google-maps" for it in items)
    assert all(it.get("source_url") for it in items)


@pytest.mark.asyncio
async def test_google_maps_actor_without_provider_refuses_to_run():
    settings = Settings(QBIT_ENV="test", QBIT_MAPS_PROVIDER="none", _env_file=None)
    actor = GoogleMapsActor(settings=settings)
    transport = make_transport({})
    ctx = make_ctx({"query": "x"}, transport)
    from app.scrapers.core.exceptions import ScraperConfigurationError

    with pytest.raises(ScraperConfigurationError):
        await collect(actor, ctx)


# -------------------------------------------------------- resource safety
@pytest.mark.asyncio
async def test_huge_response_is_refused():
    def handler(request):
        return httpx.Response(200, text="x" * (10 * 1024 * 1024))  # 10 MB

    ctx = make_ctx(
        {"url": fixture_url("/big"), "max_pages": 1},
        httpx.MockTransport(handler),
    )
    # Per-page error handling (§26): one huge page must not kill the crawl.
    items = await collect(WebsiteActor(), ctx)
    assert items == []
