"""QBIT scraper plugin system (Phase 3).

Layout:
    scrapers/core/       actor contract, context, schemas, exceptions
    scrapers/actors/     one independent package per actor (no shared logic)

The core engine contains NO source-specific scraping logic (brief §2).
"""

from app.scrapers.core.base import ActorCategory, ActorStatus, ScraperActor
from app.scrapers.core.exceptions import (
    ScraperBlockedTargetError,
    ScraperCancelledError,
    ScraperConfigurationError,
    ScraperError,
    ScraperLimitReachedError,
    ScraperNetworkError,
    ScraperPausedError,
    ScraperProviderError,
    ScraperTimeoutError,
    ScraperValidationError,
)

__all__ = [
    "ActorCategory",
    "ActorStatus",
    "ScraperActor",
    "ScraperBlockedTargetError",
    "ScraperCancelledError",
    "ScraperConfigurationError",
    "ScraperError",
    "ScraperLimitReachedError",
    "ScraperNetworkError",
    "ScraperPausedError",
    "ScraperProviderError",
    "ScraperTimeoutError",
    "ScraperValidationError",
]
