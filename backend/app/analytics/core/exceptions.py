"""Analytics exceptions (spec §24, §32)."""

from __future__ import annotations

from app.core.errors import QBITError


class AnalyticsError(QBITError):
    """Base class for analytics errors."""

    status_code = 400
    message = "Analytics request failed"


class AnalyticsValidationError(AnalyticsError):
    """Raised when a filter/period/report configuration is invalid."""

    message = "Invalid analytics request"


class AnalyticsUnavailable(AnalyticsError):
    """A metric cannot be computed from existing data (never estimated)."""

    message = "Metric not available for the requested data"


class ReportConfigError(AnalyticsError):
    """Report configuration failed validation (allowlist violation etc.)."""

    message = "Invalid report configuration"
