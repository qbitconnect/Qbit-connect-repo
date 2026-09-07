"""Allowlisted analytics filters (spec §5, §32).

Security model:
- filters are plain dataclasses; NOTHING here ever composes raw SQL strings
- every value becomes a bound parameter; string values are length-capped and
  pattern-checked; multi-values are capped to a sane maximum
- the filter set is applied IDENTICALLY across KPI cards, charts and tables —
  one widget can never silently use different filtering rules than another
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from app.analytics.core.exceptions import AnalyticsValidationError
from app.analytics.core.time import Period

MAX_VALUES_PER_FILTER = 25
MAX_STR_LEN = 300

#: keys accepted in report configurations and API query params (allowlist)
FILTER_KEYS = (
    "source", "scraper", "scraper_version", "lead_status", "lead_category",
    "industry", "city", "state", "country", "tags", "campaign_id", "channel",
    "sending_account_id", "provider", "conversation_status", "conversation_priority",
    "assigned_user_id", "assigned_team_id", "workflow_id", "execution_status",
    "source_type", "campaign_status",
)

_LIST_KEYS = set(FILTER_KEYS) - {"campaign_id", "sending_account_id", "assigned_user_id",
                                 "assigned_team_id", "workflow_id"}
_UUID_KEYS = {"campaign_id", "sending_account_id", "assigned_user_id",
              "assigned_team_id", "workflow_id"}


def _clean_list(key: str, raw: list[str] | str | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [chunk for chunk in raw.split(",")]
    values: list[str] = []
    for chunk in raw[: MAX_VALUES_PER_FILTER * 2]:
        value = str(chunk).strip()
        if not value or value in values:
            continue
        if len(value) > MAX_STR_LEN:
            raise AnalyticsValidationError(f"Filter value too long: {key}")
        if key in _UUID_KEYS:
            try:
                uuid.UUID(value)
            except ValueError as exc:
                raise AnalyticsValidationError(f"Filter {key} expects UUID values") from exc
        values.append(value)
        if len(values) >= MAX_VALUES_PER_FILTER:
            break
    return values


@dataclass(frozen=True)
class AnalyticsFilters:
    """Immutable, validated filter set. `date_from/date_to` are pre-resolved
    UTC bounds carried by `period`."""

    period: Period | None = None
    source: list[str] = field(default_factory=list)
    source_type: list[str] = field(default_factory=list)
    scraper: list[str] = field(default_factory=list)
    scraper_version: list[str] = field(default_factory=list)
    lead_status: list[str] = field(default_factory=list)
    lead_category: list[str] = field(default_factory=list)
    industry: list[str] = field(default_factory=list)
    city: list[str] = field(default_factory=list)
    state: list[str] = field(default_factory=list)
    country: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    campaign_id: list[str] = field(default_factory=list)
    campaign_status: list[str] = field(default_factory=list)
    channel: list[str] = field(default_factory=list)
    sending_account_id: list[str] = field(default_factory=list)
    provider: list[str] = field(default_factory=list)
    conversation_status: list[str] = field(default_factory=list)
    conversation_priority: list[str] = field(default_factory=list)
    assigned_user_id: list[str] = field(default_factory=list)
    assigned_team_id: list[str] = field(default_factory=list)
    workflow_id: list[str] = field(default_factory=list)
    execution_status: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return all(
            not getattr(self, key) for key in FILTER_KEYS
        )

    def to_payload(self) -> dict:
        """Canonical JSON payload (deterministic cache/report keys)."""
        out: dict = {}
        for key in FILTER_KEYS:
            values = getattr(self, key)
            if values:
                out[key] = sorted(values)
        if self.period is not None:
            out["period"] = self.period.to_dict()
        return out

    def without_period(self) -> "AnalyticsFilters":
        return replace(self, period=None)


def filters_from_params(params: dict, period: Period | None) -> AnalyticsFilters:
    """Build filters from API query params (FastAPI provides the dict).
    Unknown keys are REJECTED — never silently ignored (explicit is safer)."""
    unknown = set(params) - set(FILTER_KEYS)
    if unknown:
        raise AnalyticsValidationError(f"Unknown filter(s): {', '.join(sorted(unknown))}")
    kwargs: dict = {"period": period}
    for key, raw in params.items():
        values = _clean_list(key, raw)
        if values:
            kwargs[key] = values
    return AnalyticsFilters(**kwargs)


def filters_from_payload(payload: dict, period: Period | None) -> AnalyticsFilters:
    """Build filters from a stored report configuration payload (validated)."""
    if not isinstance(payload, dict):
        raise AnalyticsValidationError("filters must be an object")
    return filters_from_params(payload, period)
