"""Actor contract + registry tests (brief §3, §4, §5, §7, §8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scrapers.actors.email_finder import EmailFinderActor
from app.scrapers.actors.google_maps import GoogleMapsActor
from app.scrapers.actors.public_data import PublicDataActor
from app.scrapers.actors.universal import UniversalWebActor
from app.scrapers.actors.website import WebsiteActor
from app.scrapers.core.base import ActorStatus, ScraperActor
from app.scrapers.core.exceptions import ScraperError
from app.services.scraping.registry import ActorRegistry


def test_builtin_actors_register_and_discover(registry: ActorRegistry):
    assert set(registry.discover()) == {
        "google-maps", "website", "email-finder", "business-directory",
        "public-data", "universal-web", "sitemap-intelligence",
        # Actor Platform (spec §7): social / ads / india-lead actors
        "instagram", "meta-ads-library", "linkedin-public", "justdial", "indiamart",
    }
    summary = registry.summary()
    assert summary["total"] == 12


def test_register_rejects_duplicate(registry: ActorRegistry):
    with pytest.raises(ScraperError):
        registry.register(WebsiteActor())


def test_register_rejects_incomplete_contract():
    class Broken(ScraperActor):
        input_schema = WebsiteActor.input_schema  # name/description missing

        async def run(self, ctx):  # pragma: no cover
            yield

    broken = Broken()
    broken.id = "broken"
    registry = ActorRegistry()
    with pytest.raises(ScraperError, match="contract validation"):
        registry.register(broken)


def test_actor_metadata_shape(registry: ActorRegistry):
    actor = registry.get("website")
    meta = actor.metadata()
    for key in ("id", "name", "slug", "version", "description", "category",
                "author", "capabilities", "input_fields", "input_schema", "output_fields"):
        assert key in meta
    assert meta["slug"] == "website"
    assert meta["version"] == "1.0.0"
    assert "same-domain crawling" in meta["capabilities"]


def test_input_validation_website_actor():
    actor = WebsiteActor()
    ok = actor.validate_input({"url": "https://example.com"})
    assert ok.valid
    # HttpUrl normalizes to a trailing slash; mode="json" keeps it a string
    assert ok.normalized_input["url"] == "https://example.com/"
    assert ok.normalized_input["max_pages"] == 20  # schema default

    bad = actor.validate_input({"url": "not-a-url"})
    assert not bad.valid and "url" in bad.errors

    missing = actor.validate_input({})
    assert not missing.valid


def test_input_validation_email_finder_requires_target():
    report = EmailFinderActor().validate_input({"crawl_depth": 2})
    assert not report.valid


def test_input_validation_google_maps():
    actor = GoogleMapsActor()
    ok = actor.validate_input({"query": "Restaurants", "city": "Delhi", "max_results": 100})
    assert ok.valid and ok.normalized_input["max_results"] == 100
    bad = actor.validate_input({"query": " ", "max_results": 0})
    assert not bad.valid


async def test_registry_health_and_feature_flags():
    from tests.scrapers.conftest import make_settings

    settings = make_settings(Path("/tmp"), QBIT_SCRAPER_DISABLED_ACTORS="website")
    from app.scrapers.bootstrap import register_builtin_actors

    reg = register_builtin_actors(ActorRegistry(), settings)
    assert reg.entry("website").enabled is False
    report = await reg.health_check()
    assert report["website"].status is ActorStatus.DISABLED
    assert report["email-finder"].status is ActorStatus.READY


async def test_google_maps_degraded_without_provider():
    actor = GoogleMapsActor(settings=None)
    # settings wired but no provider configured
    from tests.scrapers.conftest import make_settings

    actor2 = GoogleMapsActor(settings=make_settings(Path("/tmp")))
    health = await actor2.health_check()
    assert health.status is ActorStatus.DEGRADED
    # no provider → refuse to run (never fake success, brief §55)
    import uuid

    from app.scrapers.core.context import ScraperContext
    from app.scrapers.core.exceptions import ScraperConfigurationError

    ctx = ScraperContext(
        job_id=uuid.uuid4(), actor_id=actor2.id, actor_version=actor2.version,
        input={"query": "coffee", "city": "Delhi"},
    )
    with pytest.raises(ScraperConfigurationError):
        async for _ in actor2.run(ctx):
            pass
