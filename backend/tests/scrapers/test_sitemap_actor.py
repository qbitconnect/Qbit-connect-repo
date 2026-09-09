"""Sitemap & Structured Data Intelligence actor tests (spec §18).

Mock transport: robots.txt declares a sitemap, sitemap.xml holds two URLs,
each sampled page carries JSON-LD + OpenGraph + canonical. Asserts the
page records + the site-summary record and the inventory metadata.
"""

from __future__ import annotations

import httpx
import pytest

from app.scrapers.actors.sitemap_intelligence import SitemapIntelligenceActor
import uuid

from app.scrapers.core.context import ScraperContext
from app.scrapers.core.http import HttpPolicy
from app.scrapers.core.netguard import UrlPolicy
from tests.scrapers.test_actors_http import HOST

ROBOTS = "User-agent: *\nAllow: /\nSitemap: http://fixture.test/sitemap.xml\n"

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://fixture.test/</loc><lastmod>2026-09-01</lastmod></url>
  <url><loc>http://fixture.test/about</loc><lastmod>2026-08-20</lastmod></url>
</urlset>"""

PAGE_HOME = """
<html><head><title>Fixture Business</title>
<link rel="canonical" href="http://fixture.test/">
<script type="application/ld+json">{"@type":"LocalBusiness","name":"Fixture"}</script>
<meta property="og:title" content="Fixture Business — Home">
</head><body><p>Welcome</p></body></html>
"""

PAGE_ABOUT = """
<html><head><title>About — Fixture Business</title></head>
<body><p>About us</p></body></html>
"""


def transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if path == "/sitemap.xml":
            return httpx.Response(200, content=SITEMAP.encode(), headers={"content-type": "application/xml"})
        if path == "/":
            return httpx.Response(200, html=PAGE_HOME)
        if path == "/about":
            return httpx.Response(200, html=PAGE_ABOUT)
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


class _StubProgress:
    def add_page(self): ...
    def set_stage(self, stage): ...


def _context() -> ScraperContext:
    policy = HttpPolicy(
        request_timeout=5, requests_per_second=1000.0, concurrency=4,
        max_retries=1, respect_robots=True,
    )
    urls = UrlPolicy(allowed_ports={80, 443}, allowed_hosts={HOST})
    return ScraperContext(
        job_id=uuid.uuid4(),
        actor_id=SitemapIntelligenceActor.id,
        actor_version=SitemapIntelligenceActor.version,
        attempt=1,
        input={"url": f"http://{HOST}/"},
        http_policy=policy,
        url_policy=urls,
        progress=_StubProgress(),
        settings=None,
        http_transport=transport(),
    )





@pytest.mark.asyncio
async def test_sitemap_actor_yields_pages_and_summary():
    ctx = _context()
    records = []
    async for rec in SitemapIntelligenceActor().run(ctx):
        records.append(rec)
    await ctx.close()

    kinds = [r["metadata"]["record_type"] for r in records]
    assert "page" in kinds and "site_summary" in kinds
    pages = [r for r in records if r["metadata"]["record_type"] == "page"]
    assert len(pages) == 2
    home = next(r for r in pages if r["source_url"].endswith("/"))
    assert home["business_name"] == "Fixture Business"
    assert home["metadata"]["canonical"] == "http://fixture.test/"
    assert "LocalBusiness" in home["metadata"]["json_ld_types"]
    assert home["metadata"]["open_graph"].get("og_title") == "Fixture Business — Home"
    summary = next(r for r in records if r["metadata"]["record_type"] == "site_summary")
    inv = summary["metadata"]["sitemap_inventory"]
    assert inv["total_urls"] == 2
    assert inv["lastmod_min"] == "2026-08-20"
    assert inv["lastmod_max"] == "2026-09-01"
    assert inv["sampled"] == 2
    assert all(r["source"] == "sitemap-intelligence" for r in records)
