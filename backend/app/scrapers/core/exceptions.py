"""Scraper-specific exceptions (brief §40).

Mapping to job outcomes:
    ScraperValidationError   → job FAILED, no retry (input/config problem)
    ScraperConfigurationError → job FAILED, no retry
    ScraperBlockedTargetError → job FAILED, no retry (SSRF/private target)
    ScraperNetworkError      → retryable (transient network conditions)
    ScraperTimeoutError      → per-request retryable; job deadline → PAUSE at checkpoint
    ScraperLimitReachedError → clean stop (max_records/max_pages); results kept
    ScraperProviderError     → retryable iff `retryable=True`
    ScraperPausedError       → internal control flow (not a failure)
    ScraperCancelledError    → internal control flow (not a failure)
"""

from __future__ import annotations


class ScraperError(Exception):
    """Base class for all scraper subsystem errors."""

    code = "SCRAPER_ERROR"
    retryable = False

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}


class ScraperValidationError(ScraperError):
    """Input failed schema/policy validation. Never retried."""

    code = "SCRAPER_VALIDATION_FAILED"
    retryable = False


class ScraperConfigurationError(ScraperError):
    """Actor/job configuration is invalid or a required dependency is missing."""

    code = "SCRAPER_CONFIGURATION_ERROR"
    retryable = False


class ScraperNetworkError(ScraperError):
    """Transient network failure (connection reset, DNS hiccup, ...)."""

    code = "SCRAPER_NETWORK_ERROR"
    retryable = True


class ScraperTimeoutError(ScraperError):
    """A request or the whole job exceeded its deadline.

    Per-request timeouts are retryable; when raised for the JOB deadline the
    runner pauses the job at its checkpoint instead of burning retries.
    """

    code = "SCRAPER_TIMEOUT"
    retryable = True


class ScraperLimitReachedError(ScraperError):
    """A configured max_records/max_pages limit was reached (brief §32).

    Not a failure and never retried: the job stops safely, results already
    processed are kept, and the completion event reports LIMIT_REACHED.
    """

    code = "SCRAPER_LIMIT_REACHED"
    retryable = False


class ScraperProviderError(ScraperError):
    """External data provider failed or is not configured.

    `retryable=True` only for transient provider-side conditions (5xx, rate
    limit responses). Configuration mistakes are never retryable.
    """

    code = "SCRAPER_PROVIDER_ERROR"

    def __init__(
        self, message: str = "", *, retryable: bool = False, details: dict | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.retryable = retryable


class ScraperPausedError(ScraperError):
    """Raised at a safe point when the job was paused (internal control flow)."""

    code = "SCRAPER_PAUSED"
    retryable = False


class ScraperCancelledError(ScraperError):
    """Raised at a safe point when the job was cancelled (internal control flow)."""

    code = "SCRAPER_CANCELLED"
    retryable = False


class ScraperBlockedTargetError(ScraperError):
    """Target URL failed SSRF/private-network validation (brief §41)."""

    code = "SCRAPER_TARGET_BLOCKED"
    retryable = False
