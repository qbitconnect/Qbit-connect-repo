"""QBIT CONNECT — Scraper Health & Structured Diagnostics (Brief §17, §18, §19).

Provides structured diagnostics for scraper failures, distinguishing transient
network glitches from markup changes, access barriers, and configuration errors,
with actionable remediation recommendations.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.scrapers.core.base import ActorHealth, ActorStatus


class FailureCategory(str, enum.Enum):
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    MARKUP_CHANGED = "MARKUP_CHANGED"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    SOURCE_EXHAUSTED = "SOURCE_EXHAUSTED"
    RESOURCE_BUDGET_EXCEEDED = "RESOURCE_BUDGET_EXCEEDED"
    UNKNOWN = "UNKNOWN"


@dataclass
class StructuredDiagnostics:
    scraper_id: str
    scraper_name: str
    stage: str
    error_code: str
    reason: str
    attempts: int
    category: FailureCategory
    is_retryable: bool
    possible_causes: list[str]
    recommended_actions: list[str]
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        return data


class DiagnosticsEngine:
    """Analyzes errors and generates structured diagnostics."""

    @classmethod
    def diagnose_error(
        cls,
        *,
        actor_id: str,
        actor_name: str,
        error: Exception | str,
        error_code: str | None = None,
        attempts: int = 1,
        stage: str = "execution",
    ) -> StructuredDiagnostics:
        err_str = str(error).lower()
        code = (error_code or "SCRAPER_ERROR").upper()

        category = FailureCategory.UNKNOWN
        is_retryable = False
        possible_causes: list[str] = []
        recommended_actions: list[str] = []

        # 1. Anti-bot / access barriers / login walls
        if any(w in err_str for w in ("blocked", "target_blocked", "login", "authwall", "403", "captcha")):
            category = FailureCategory.ACCESS_BLOCKED
            is_retryable = False
            possible_causes = [
                "Target site served an authentication wall (login required)",
                "Anti-bot or rate-limiting challenge presented",
                "IP or user-agent blocked by edge gateway",
                "Public surface no longer accessible without credentials",
            ]
            recommended_actions = [
                "Verify if the target URL is accessible in a clean logged-out browser",
                "Reduce request concurrency and increase delays",
                "Choose an alternative directory or provider tool",
                "Halt the task and report the source limitation",
            ]

        # 2. Structure / markup changes
        elif any(w in err_str for w in ("selector", "extract", "parse", "structure", "unconsumed column", "jsondecode")):
            category = FailureCategory.MARKUP_CHANGED
            is_retryable = False
            possible_causes = [
                "Target site HTML layout or CSS classes were updated",
                "Embedded JSON structure changed its key schema",
                "Dynamic JavaScript rendering delayed content arrival",
                "Expected DOM elements absent from response",
            ]
            recommended_actions = [
                "Run actor diagnostic probe to inspect latest raw markup",
                "Update CSS selectors or JSON extraction paths in parser",
                "Use Universal Web Scraper with auto extraction as temporary alternative",
                "Stop task and notify scraping platform engineering",
            ]

        # 3. Transient Network / Timeouts
        elif any(w in err_str for w in ("timeout", "connect", "reset", "502", "503", "504", "temporary")):
            category = FailureCategory.TRANSIENT_NETWORK
            is_retryable = True
            possible_causes = [
                "Target server experienced high load or transient downtime",
                "Upstream gateway timeout before response completed",
                "Local network latency or socket timeout",
            ]
            recommended_actions = [
                "Retry with exponential backoff and higher request timeout",
                "Verify target host connectivity",
                "Resume from last valid checkpoint",
            ]

        # 4. Configuration Error
        elif any(w in err_str for w in ("config", "missing", "provider", "invalid input", "validation")):
            category = FailureCategory.CONFIGURATION_ERROR
            is_retryable = False
            possible_causes = [
                "Missing required provider configuration (e.g. maps provider / credentials)",
                "Input parameters failed schema or policy constraints",
                "Invalid URL or disallowed port / private IP address",
            ]
            recommended_actions = [
                "Review and correct input parameters in job configuration",
                "Ensure required environment settings are wired",
            ]

        else:
            category = FailureCategory.UNKNOWN
            is_retryable = attempts < 3
            possible_causes = ["Unexpected runtime error during execution"]
            recommended_actions = ["Inspect full job logs", "Retry once if safe", "Report issue to administrator"]

        return StructuredDiagnostics(
            scraper_id=actor_id,
            scraper_name=actor_name,
            stage=stage,
            error_code=code,
            reason=str(error),
            attempts=attempts,
            category=category,
            is_retryable=is_retryable,
            possible_causes=possible_causes,
            recommended_actions=recommended_actions,
        )
