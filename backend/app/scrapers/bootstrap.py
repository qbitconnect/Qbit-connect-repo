"""Actor bootstrap — registers built-in actors into the registry.

Adding the next 50 actors = one import + one line below (brief FINAL RULE:
"make it possible to add the next 50+ scrapers without redesigning the core").
Future alternative: an entry-point loader; the registry API stays the same.

Feature flags (brief §50): an actor is DISABLED when
- the global switch QBIT_SCRAPER_ENABLED=false, or
- its id appears in QBIT_SCRAPER_DISABLED_ACTORS (comma-separated env).
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.scrapers.core.base import ActorStatus, ScraperActor
from app.services.scraping.registry import ActorRegistry

logger = get_logger("qbit.scrapers.bootstrap")


def builtin_actor_classes() -> list[type[ScraperActor]]:
    """Import + return every built-in actor class (order = UI card order)."""
    from app.scrapers.actors.business_directory import BusinessDirectoryActor
    from app.scrapers.actors.email_finder import EmailFinderActor
    from app.scrapers.actors.google_maps import GoogleMapsActor
    from app.scrapers.actors.public_data import PublicDataActor
    from app.scrapers.actors.sitemap_intelligence import SitemapIntelligenceActor
    from app.scrapers.actors.universal import UniversalWebActor
    from app.scrapers.actors.website import WebsiteActor

    classes = [
        GoogleMapsActor,
        WebsiteActor,
        SitemapIntelligenceActor,
        EmailFinderActor,
        BusinessDirectoryActor,
        PublicDataActor,
        UniversalWebActor,
    ]
    return classes


def register_builtin_actors(registry: ActorRegistry, settings=None) -> ActorRegistry:
    """Register all built-in actors, honoring feature flags."""
    from app.scrapers.actors.google_maps import GoogleMapsActor

    disabled = settings.scraper_disabled_actors() if settings else set()
    global_enabled = getattr(settings, "QBIT_SCRAPER_ENABLED", True) if settings else True

    for actor_cls in builtin_actor_classes():
        kwargs = {}
        if actor_cls is GoogleMapsActor:
            kwargs["settings"] = settings
        actor = actor_cls(**kwargs)
        enabled = global_enabled and actor.id not in disabled
        try:
            entry = registry.register(actor, enabled=enabled)
        except Exception as exc:  # noqa: BLE001 — a broken actor must not kill boot
            logger.error(
                "Actor registration failed",
                extra={"extra_fields": {"actor": actor_cls.__name__, "error": str(exc)}},
            )
            continue
        if not enabled:
            entry.status = ActorStatus.DISABLED
        logger.info(
            "Actor available",
            extra={
                "extra_fields": {
                    "actor": actor.id,
                    "version": actor.version,
                    "enabled": enabled,
                }
            },
        )
    return registry
