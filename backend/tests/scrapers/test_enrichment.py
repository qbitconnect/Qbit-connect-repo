"""Lead enrichment layer tests (spec §20) — mock transport only, no live web.

- fills EMPTY fields from public contact pages, never overwrites
- provenance written to metadata_json.enrichment (consent=not_implied)
- last_verified_at finally gets a writer
- no website → SKIPPED; unreachable site → honest FAILED
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.models.scrape import Lead
from app.services.scraping.enrichment import LeadEnrichmentService
from tests.scrapers.test_actors_http import HOST, policy

INDEX_HTML = """
<html><head><title>Fixture Business</title></head><body>
<a href="/contact">Contact</a>
<div>Write to hello@fixture.test or call +91 11 4000 1000</div>
</body></html>
"""

CONTACT_HTML = """
<html><head><title>Contact — Fixture Business</title></head><body>
Email: <a href="mailto:sales@fixture.test">sales@fixture.test</a>,
       <a href="mailto:random@gmail.com">random@gmail.com</a>
Call <a href="tel:+911140002000">+91 11 4000 2000</a>
<a href="https://facebook.com/fixturebiz">Facebook</a>
</body></html>
"""


def transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in ("/", "/index.html"):
            return httpx.Response(200, html=INDEX_HTML)
        if path == "/contact":
            return httpx.Response(200, html=CONTACT_HTML)
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


async def _mk_lead(db, **overrides) -> Lead:
    async with db.session() as session:
        lead = Lead(
            business_name=overrides.pop("business_name", "Fixture Business"),
            website=overrides.pop("website", f"http://{HOST}"),
            source="manual",
            source_type="manual",
            created_by=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            **overrides,
        )
        session.add(lead)
        await session.commit()
        await session.refresh(lead)
        return lead


@pytest.mark.asyncio
async def test_enrichment_fills_empty_fields(scrape_env):
    settings, db, *_rest = scrape_env
    lead = await _mk_lead(db)
    async with db.session() as session:
        service = LeadEnrichmentService(session, settings, transport=transport(), url_policy=policy())
        row = await session.get(Lead, lead.id)
        updated = await service.enrich_lead(row)

    assert updated.enrichment_status == "ENRICHED"
    assert updated.email == "sales@fixture.test"  # own-domain wins over free-host
    assert updated.phone
    assert (updated.social_links or {}).get("facebook")
    meta = updated.metadata_json or {}
    assert meta["enrichment"]["consent"] == "not_implied"
    assert meta["enrichment"]["pages_scanned"] >= 2
    assert updated.last_verified_at is not None
    assert updated.confidence is not None and updated.confidence >= 55


@pytest.mark.asyncio
async def test_enrichment_never_overwrites_existing_values(scrape_env):
    settings, db, *_rest = scrape_env
    lead = await _mk_lead(db, email="existing@owner.test", phone="+15550001111")
    async with db.session() as session:
        service = LeadEnrichmentService(session, settings, transport=transport(), url_policy=policy())
        row = await session.get(Lead, lead.id)
        updated = await service.enrich_lead(row)
    assert updated.email == "existing@owner.test"
    assert updated.phone == "+15550001111"
    # empty fields still fill: social link found → only social_links updated
    assert updated.metadata_json["enrichment"]["fields_updated"] == ["social_links"]
    assert (updated.social_links or {}).get("facebook")


@pytest.mark.asyncio
async def test_enrichment_skips_lead_without_website(scrape_env):
    settings, db, *_rest = scrape_env
    lead = await _mk_lead(db, website=None)
    async with db.session() as session:
        service = LeadEnrichmentService(session, settings, transport=transport(), url_policy=policy())
        row = await session.get(Lead, lead.id)
        updated = await service.enrich_lead(row)
    assert updated.enrichment_status == "SKIPPED"
