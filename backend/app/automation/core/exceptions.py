"""Automation exceptions with node-level error classification (§37).

Classification drives retry behaviour:
- TRANSIENT      → retry with exponential backoff (§38)
- PERMANENT      → fail/skip (never retried)
- CONFIGURATION  → workflow requires correction (publish validation should
                   have caught it; fail the execution honestly)
- PERMISSION     → fail safely and audit
"""

from __future__ import annotations

from app.models.automation import ErrorClass


class AutomationError(Exception):
    """Base class for automation errors."""

    error_class = ErrorClass.PERMANENT

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class TransientError(AutomationError):
    """Temporary failure (provider/network/DB) — safe to retry."""

    error_class = ErrorClass.TRANSIENT


class PermanentError(AutomationError):
    """Permanent failure — do not retry (§38: invalid lead/email/unsubscribed
    are permanent)."""


class ConfigurationError(AutomationError):
    """Invalid workflow configuration — requires correction."""

    error_class = ErrorClass.CONFIGURATION


class PermissionError(AutomationError):  # noqa: A003 — domain-specific name
    """Missing permission to perform an action — fail safely and audit (§37)."""

    error_class = ErrorClass.PERMISSION


class ActionSkipped(Exception):
    """Raised by actions that legitimately skip (§27): the step is recorded
    as SKIPPED with a reason and the workflow continues on its path.

    Examples: unsubscribed recipient, closed WhatsApp window without a
    template, missing conversation context."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ValidationError(AutomationError):
    """Definition validation failure (§21) — carries structured issues."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("Workflow validation failed: " + "; ".join(issues[:10]))
        self.issues = issues
