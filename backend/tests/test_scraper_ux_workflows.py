"""Tests for Scraper UX & Execution Flow fixes (Workflows A, B, C, Google Maps notice, results actions)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD
from app.services.orchestration.url_analyzer import UrlAnalyzer


@pytest_asyncio.fixture
async def ui_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _login(ui_client: AsyncClient):
    resp = await ui_client.post(
        "/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": "/scraping"},
    )
    assert resp.status_code == 303
    return resp


def test_url_analyzer_unit():
    """Verify UrlAnalyzer correctly classifies diverse target URLs."""
    # 1. Google Maps URL
    res_maps = UrlAnalyzer.analyze("https://www.google.com/maps/search/shoe+wholesalers+in+modinagar/@28.83,77.58,14z")
    assert res_maps.source == "google-maps"
    assert res_maps.confidence >= 0.9
    assert "shoe wholesalers" in (res_maps.query or "").lower()
    assert res_maps.location == "modinagar" or "modinagar" in (res_maps.query or "").lower()

    # 2. Justdial URL
    res_jd = UrlAnalyzer.analyze("https://www.justdial.com/Delhi/Textile-Merchants")
    assert res_jd.source == "justdial"
    assert res_jd.confidence >= 0.9
    assert res_jd.location == "Delhi"
    assert "Textile" in (res_jd.query or "")

    # 3. IndiaMART URL
    res_im = UrlAnalyzer.analyze("https://dir.indiamart.com/search.mp?ss=cotton+fabric&city=Surat")
    assert res_im.source == "indiamart"
    assert res_im.confidence >= 0.9
    assert res_im.query == "cotton fabric"
    assert res_im.location == "Surat"

    # 4. Sitemap XML URL
    res_sm = UrlAnalyzer.analyze("https://example.com/sitemap.xml")
    assert res_sm.source == "sitemap-intelligence"
    assert res_sm.input_type == "sitemap"

    # 5. Public Data CSV
    res_csv = UrlAnalyzer.analyze("https://data.gov.in/dataset/companies.csv")
    assert res_csv.source == "public-data"
    assert res_csv.input_type == "dataset"

    # 6. Generic Website
    res_web = UrlAnalyzer.analyze("https://acme-corp.com/about")
    assert res_web.source in ("website", "universal-web")
    assert res_web.confidence >= 0.7


async def test_ui_analyze_url_endpoint(ui_client):
    """Test the POST /scraping/analyze-url endpoint."""
    await _login(ui_client)

    # Valid Justdial URL
    resp = await ui_client.post(
        "/scraping/analyze-url",
        json={"url": "https://www.justdial.com/Delhi/Textile-Merchants"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "justdial"
    assert data["location"] == "Delhi"
    assert "Textile" in data["query"]

    # Empty URL rejected
    resp_empty = await ui_client.post("/scraping/analyze-url", json={"url": ""})
    assert resp_empty.status_code == 400


async def test_intelligent_search_workflow_a(ui_client):
    """Workflow A: natural search query decomposed into execution plan."""
    await _login(ui_client)

    resp = await ui_client.post(
        "/scraping/agent/plan",
        json={
            "prompt": "Find shoe wholesalers in Modinagar",
            "source": "google-maps",
            "target_count": 75,
        },
    )
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["primary_tool"] == "google-maps"
    assert plan["target_count"] == 75
    assert plan["source_locked"] is True
    assert "orchestration_strategy" in plan
    assert len(plan["steps"]) >= 1


async def test_scraper_detail_renders_all_3_workflows_and_google_maps_notice(ui_client):
    """Verify detail page renders 3 workflow modes and Google Maps provider warning."""
    await _login(ui_client)

    # 1. Check Google Maps actor page has the provider warning and 3 modes
    resp_maps = await ui_client.get("/scraping/google-maps")
    assert resp_maps.status_code == 200
    html_maps = resp_maps.text
    assert "Google Maps Provider Not Configured" in html_maps
    assert "QBIT_MAPS_PROVIDER" in html_maps
    assert "Intelligent Search" in html_maps
    assert "Scrape from URL" in html_maps
    assert "Direct Schema" in html_maps
    assert "Pre-Flight Execution Plan Preview" in html_maps
    assert "URL Analysis &amp; Execution Preview" in html_maps

    # 2. Check website actor page
    resp_web = await ui_client.get("/scraping/website")
    assert resp_web.status_code == 200
    html_web = resp_web.text
    assert "Intelligent Search" in html_web
    assert "Scrape from URL" in html_web
    assert "Run configuration" in html_web
