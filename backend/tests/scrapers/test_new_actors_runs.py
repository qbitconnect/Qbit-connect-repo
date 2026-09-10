"""Actor Platform spec §7/§36/§40 — run-level tests for the new actors.

Each actor is executed through a real ScraperContext with an httpx
MockTransport serving deterministic fixture pages. Acceptance flows
(spec §40) are covered against the FIXTURE server — live targets are
network-dependent and covered by the guarded smoke script instead.

Key honesty rules verified here (spec §42):
- a login/anti-bot wall FAILS the run (ScraperBlockedTargetError), never
  silently "succeeds" with zero fabricated items;
- genuine public content parses into lead-shaped records.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.scrapers.actors.indiamart import IndiaMartActor
from app.scrapers.actors.instagram import InstagramActor
from app.scrapers.actors.justdial import JustDialActor
from app.scrapers.actors.linkedin import LinkedInActor
from app.scrapers.actors.meta_ads_library import MetaAdsLibraryActor
from app.scrapers.actors.universal import UniversalWebActor
from app.scrapers.core.context import ScraperContext
from app.scrapers.core.exceptions import ScraperBlockedTargetError
from app.scrapers.core.http import HttpPolicy
from app.scrapers.core.netguard import UrlPolicy

HOST = "fixture.test"


def _ok(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _context(actor, inp: dict, transport: httpx.MockTransport) -> ScraperContext:
    policy = HttpPolicy(
        request_timeout=5, requests_per_second=1000.0, concurrency=4,
        max_retries=1, respect_robots=True,
    )
    urls = UrlPolicy(allowed_ports={80, 443}, allowed_hosts={HOST})
    return ScraperContext(
        job_id=uuid.uuid4(),
        actor_id=actor.id,
        actor_version=actor.version,
        attempt=1,
        input=inp,
        http_policy=policy,
        url_policy=urls,
        settings=None,
        http_transport=transport,
    )


async def _collect(actor, inp: dict, handler) -> list[dict]:
    ctx = _context(actor, inp, _ok(handler))
    records = [rec async for rec in actor.run(ctx)]
    await ctx.close()
    return records


# ------------------------------------------------------------------ fixtures
IG_PROFILE_HTML = (
    '<html><head>'
    '<meta property="og:title" content="Fixture Brand (@fixturebrand) • Instagram photos and videos">'
    '<meta property="og:description" content="123K Followers, 45 Following, 678 Posts - Retail shop in Pune">'
    "</head><body>ok</body></html>"
)

ADS_PAYLOAD = {"ads": [
    {"ad_archive_id": "9001", "snapshot": {"body": "Sale on fixtures", "cta_text": "Shop now",
     "page": {"name": "Fixture Advertiser"}}, "platforms": ["facebook"]},
]}
ADS_HTML = (
    "<html><body><script>window.__initialData = " + json.dumps(ADS_PAYLOAD) + ";</script></body></html>"
)

LD_COMPANY_HTML = (
    "<html><head>"
    "<meta property='og:title' content='Fixture Labs on LinkedIn: \"Widgets and tools\"'>"
    "<meta property='og:description' content='Fixture Labs makes widgets. 1,200 employees on LinkedIn.'>"
    "</head><body>ok</body></html>"
)

JD_LISTING_HTML = (
    "<html><body><div class='cntanr'>"
    "<h2 class='business-name'><span class='lng_cont_name'>Fixture Traders</span></h2>"
    "<span class='cont_sw_addr'>12 MG Road, Bengaluru 560001</span>"
    "<span class='total_rate'>4.2</span><span class='rating_count'>88 Votes</span>"
    "<a href='tel:+919876543210'></a>"
    "</div></body></html>"
)

IM_SEARCH_HTML = (
    "<html><body><div class='card'>"
    "<a class='cardlinks' href='https://www.indiamart.com/fixture-exports/'>Fixture Exports</a>"
    "<p class='company name'><a href='https://www.indiamart.com/fixture-exports/'>Fixture Exports</a></p>"
    "<span class='price'>Rs 500 / Piece</span>"
    "<span class='newLocationUi'>Delhi</span>"
    "<a href='tel:+919812345678'></a>"
    "</div></body></html>"
)


# ------------------------------------------------------------------ Instagram
@pytest.mark.asyncio
async def test_instagram_profile_run_yields_record():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=IG_PROFILE_HTML)

    records = await _collect(
        InstagramActor(),
        {"mode": "profile", "username": "fixturebrand"},
        handler,
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["business_name"] == "Fixture Brand"
    assert rec["metadata"]["username"] == "fixturebrand"
    assert rec["metadata"]["followers"] == 123000
    assert rec["source"] == "instagram"


@pytest.mark.asyncio
async def test_instagram_login_wall_fails_run_honestly():
    """Spec §36/§42: a wall must FAIL the run — never a fake empty success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html="<html><head><title>Login • Instagram</title></head>"
                 "<body>Sign up to see photos from your friends.</body></html>",
        )

    with pytest.raises(ScraperBlockedTargetError):
        await _collect(InstagramActor(), {"mode": "profile", "username": "fixturebrand"}, handler)


# ------------------------------------------------------------------ Meta Ads
@pytest.mark.asyncio
async def test_meta_ads_run_yields_ads():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=ADS_HTML)

    records = await _collect(
        MetaAdsLibraryActor(), {"keyword": "fixtures", "country": "IN"}, handler
    )
    assert len(records) == 1
    ad = records[0]
    assert ad["metadata"]["ad_id"] == "9001"
    assert ad["metadata"]["record_type"] == "ad"
    assert "content_hash" in ad["metadata"]  # change-detection key (spec §7.B)


# ------------------------------------------------------------------ LinkedIn
@pytest.mark.asyncio
async def test_linkedin_company_run_yields_record():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=LD_COMPANY_HTML)

    records = await _collect(
        LinkedInActor(), {"mode": "company", "company_slug": "fixture-labs"}, handler
    )
    assert records[0]["business_name"] == "Fixture Labs"
    assert records[0]["metadata"]["employees_hint"] == 1200


@pytest.mark.asyncio
async def test_linkedin_authwall_fails_run_honestly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(999, text="authwall")

    with pytest.raises(ScraperBlockedTargetError):
        await _collect(LinkedInActor(), {"mode": "company", "company_slug": "fixture-labs"}, handler)


# ------------------------------------------------------------------ JustDial
@pytest.mark.asyncio
async def test_justdial_search_run_yields_listings():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/Delhi/Electricians")
        return httpx.Response(200, html=JD_LISTING_HTML)

    records = await _collect(
        JustDialActor(),
        {"mode": "search", "city": "Delhi", "category": "Electricians"},
        handler,
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["business_name"] == "Fixture Traders"
    assert rec["phone"] == "+919876543210"
    assert rec["rating"] == 4.2
    assert rec["review_count"] == 88


# ------------------------------------------------------------------ IndiaMART
@pytest.mark.asyncio
async def test_indiamart_search_run_yields_suppliers():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "ss=leather+shoes" in str(request.url) or "ss=leather%20shoes" in str(request.url)
        return httpx.Response(200, html=IM_SEARCH_HTML)

    records = await _collect(
        IndiaMartActor(), {"mode": "product_search", "keyword": "leather shoes"}, handler
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["business_name"] == "Fixture Exports"
    assert rec["metadata"]["price_inr"] == 500.0


# ------------------------------------------------------------------ Universal auto
AUTO_HTML = (
    "<html><head><title>Fixture Industries</title>"
    '<script type="application/ld+json">{"@type":"Organization","name":"Fixture Industries",'
    '"telephone":"+91 11 4000 5000","address":"Connaught Place, New Delhi"}</script>'
    "</head><body><main>Contact sales@fixture.industries</main>"
    "<p>+91 11 4000 5000 call us</p></body></html>"
)


@pytest.mark.asyncio
async def test_universal_auto_strategy_acceptance():
    """Acceptance TEST 6 (spec §40): paste an arbitrary URL, auto-detect runs."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=AUTO_HTML)

    records = await _collect(
        UniversalWebActor(), {"url": f"http://{HOST}/", "strategy": "auto"}, handler
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["metadata"]["extraction_strategy"] == "auto"
    assert rec["metadata"]["json_ld_org"]["name"] == "Fixture Industries"
    assert rec["email"] == "sales@fixture.industries"
    assert rec["phone"]


@pytest.mark.asyncio
async def test_universal_selectors_strategy_still_works():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html="<html><body><div class='item'><h2 class='name'>Widget Co</h2></div></body></html>",
        )

    records = await _collect(
        UniversalWebActor(),
        {"url": f"http://{HOST}/", "fields": [{"name": "business_name", "selector": ".name"}]},
        handler,
    )
    assert records[0]["business_name"] == "Widget Co"
    assert records[0]["metadata"]["extraction_strategy"] == "selectors"
