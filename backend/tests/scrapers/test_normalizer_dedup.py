"""Normalizer + deduplication + lead storage tests (brief §9, §20, §21, §22, §23)."""

from __future__ import annotations

import uuid

import pytest

from app.services.leads import LeadService
from app.services.scraping.dedup import Deduplicator, MatchConfidence
from app.services.scraping.normalizer import normalize_item

# ----------------------------------------------------------------- normalize
def test_normalize_full_item():
    item = {
        "business_name": "  Spice   Garden  ",
        "phone": "+91 (11) 4000-1000",
        "email": "HELLO@SpiceGarden.example.com",
        "website": "https://www.spicegarden.example.com/menu",
        "city": "New Delhi",
        "rating": 4.5,
        "review_count": 12,
        # source-specific extras must land in metadata (brief §9)
        "opening_hours": "10-22",
        "price_level": "$$",
    }
    out = normalize_item(item, source="google-maps")
    assert out is not None
    assert out["business_name"] == "Spice Garden"
    assert out["phone"] == "+911140001000"
    assert out["email"] == "hello@spicegarden.example.com"
    assert out["website_norm"] == "spicegarden.example.com"
    assert out["email_norm"] == "hello@spicegarden.example.com"
    assert out["name_key"].startswith("spice garden|new delhi")
    assert out["source"] == "google-maps"
    assert out["metadata"]["opening_hours"] == "10-22"
    assert out["metadata"]["price_level"] == "$$"
    assert "scraped_at" in out


def test_normalize_rejects_useless_records():
    assert normalize_item({}, source="x") is None
    assert normalize_item({"foo": "bar"}, source="x") is None
    # name only, no provenance → rejected
    assert normalize_item({"business_name": "Ghost Biz"}, source="x") is None
    # name + source_url → kept (provenance preserved)
    kept = normalize_item(
        {"business_name": "Biz", "source_url": "https://a.example.com/x"}, source="x"
    )
    assert kept is not None


def test_normalize_salvages_bad_email():
    out = normalize_item(
        {"business_name": "Biz", "email": "not-an-email", "phone": "+91 90000 00000"},
        source="x",
    )
    assert out is not None
    assert out["email"] is None
    assert out["metadata"]["unparsed"]["email"] == "not-an-email"


# ---------------------------------------------------------------- dedup keys
def test_dedup_confidence_matrix():
    dedup = Deduplicator()
    high = dedup.should_merge(
        type("M", (), {"confidence": MatchConfidence.HIGH, "matched_on": "email"})()
    )
    medium_auto = dedup.should_merge(
        type("M", (), {"confidence": MatchConfidence.MEDIUM, "matched_on": "name"})()
    )
    assert high is True
    assert medium_auto is False  # information-preserving default (brief §21)
    strict = Deduplicator(policy="strict")
    assert strict.should_merge(
        type("M", (), {"confidence": MatchConfidence.MEDIUM, "matched_on": "name"})()
    )
    none = dedup.should_merge(
        type("M", (), {"confidence": MatchConfidence.NONE, "matched_on": None})()
    )
    assert none is False


# ------------------------------------------------------------- lead storage
@pytest.mark.asyncio
async def test_lead_service_create_update_merge(tmp_path):
    from tests.scrapers.conftest import make_settings
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from app.models.scrape import Lead
    from sqlalchemy import select

    settings = make_settings(tmp_path)
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    job_id = uuid.uuid4()
    service = LeadService()

    async with db.session() as session:
        lead, created = await service.create_or_update(
            session,
            normalize_item(
                {"business_name": "Biz A", "email": "a@biz.example.com", "phone": "+911100000000"},
                source="website",
            ),
            actor_id="website", actor_version="1.0.0", job_id=job_id,
        )
        assert created and lead.email_norm == "a@biz.example.com"
        assert lead.source_actor_id == "website" and lead.source_job_id == job_id

    async with db.session() as session:
        match = type("M", (), {
            "lead_id": lead.id, "confidence": MatchConfidence.HIGH, "matched_on": "email",
        })()
        updated, created2 = await service.create_or_update(
            session,
            normalize_item(
                {"business_name": "Biz A Renamed", "email": "a@biz.example.com",
                 "website": "https://biza.example.com"},
                source="google-maps",
            ),
            actor_id="google-maps", actor_version="1.0.0", job_id=job_id, match=match, merge=True,
        )
        assert not created2
        assert updated.id == lead.id
        assert updated.seen_count == 2
        # HIGH-confidence merge fills EMPTY fields, never overwrites
        assert updated.business_name == "Biz A"
        # website was normalized to the bare host (normalize_website)
        assert updated.website == "biza.example.com"
        assert updated.metadata_json["last_actor_id"] == "google-maps"

    async with db.session() as session:
        # MEDIUM-confidence, non-merge: information preserved with a flag
        match2 = type("M", (), {
            "lead_id": lead.id, "confidence": MatchConfidence.MEDIUM, "matched_on": "name+location",
        })()
        new_lead, created3 = await service.create_or_update(
            session,
            normalize_item({"business_name": "Biz A", "city": "Delhi", "phone": "+911100000001"},
                           source="business-directory"),
            actor_id="business-directory", actor_version="1.0.0", job_id=job_id,
            match=match2, merge=False,
        )
        assert created3
        assert new_lead.metadata_json["possible_duplicate_of"] == str(lead.id)
        total = len((await session.scalars(select(Lead))).all())
        assert total == 2
    await db.close()
