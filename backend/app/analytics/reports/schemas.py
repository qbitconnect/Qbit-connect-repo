"""Report configuration schema — STRICT allowlist validation (spec §17, §32).

Security rules:
- NO raw SQL, NO expression strings, NO operator characters — a report stores
  only validated identifiers, enums and values
- domains are the ReportDomain enum; metrics/dimensions/visualizations come
  from the allowlist catalogs below; filters reuse the analytics allowlist
- unknown keys anywhere are REJECTED (never silently dropped)
- maxima bound every list so a report can never explode a query
"""

from __future__ import annotations

import re

from app.analytics.core.exceptions import ReportConfigError
from app.analytics.core.filters import FILTER_KEYS, filters_from_payload
from app.analytics.core.time import PERIOD_PRESETS
from app.models.analytics import ReportDomain

NAME_RE = re.compile(r"^[\w \-()&,./]{1,200}$", re.UNICODE)

DOMAINS = sorted(domain.value for domain in ReportDomain)

METRIC_CATALOG: dict[str, list[str]] = {
    "OVERVIEW": [
        "total_leads", "new_leads", "qualified_leads", "contacted_leads",
        "interested_leads", "converted_leads", "active_campaigns",
        "messages_sent", "messages_delivered", "replies", "open_conversations",
        "automation_executions",
    ],
    "LEADS": [
        "total", "new", "valid", "verified", "qualified", "contacted",
        "replied", "interested", "converted", "lost", "archived",
        "avg_quality_score", "valid_rate",
    ],
    "SCRAPING": [
        "jobs_total", "completed", "failed", "cancelled", "records_found",
        "records_accepted", "records_duplicate", "records_rejected",
        "success_rate", "acceptance_rate", "avg_runtime_seconds",
    ],
    "MARKETING": [
        "campaigns_total", "campaigns_completed", "campaigns_running",
        "campaigns_failed", "recipients", "messages_queued", "messages_sent",
        "messages_delivered", "messages_failed", "replies", "unsubscribed",
        "delivery_rate", "failure_rate", "reply_rate", "unsubscribe_rate",
    ],
    "WHATSAPP": [
        "messages_sent", "messages_delivered", "messages_read",
        "messages_failed", "replies", "incoming_messages", "outgoing_messages",
        "conversations_total", "conversations_active", "delivery_rate",
        "reply_rate",
    ],
    "EMAIL": [
        "recipients", "emails_sent", "delivered", "bounced", "hard_bounces",
        "soft_bounces", "complained", "replies", "opens", "clicks",
        "unsubscribed", "delivery_rate", "bounce_rate", "complaint_rate",
        "reply_rate", "open_rate", "click_rate",
    ],
    "INBOX": [
        "conversations_total", "new", "open", "pending", "waiting", "resolved",
        "closed", "reopened", "unread", "assigned", "unassigned",
        "avg_first_response_seconds", "avg_resolution_seconds",
        "avg_messages_per_conversation",
    ],
    "AUTOMATION": [
        "workflows_active", "executions", "executions_completed",
        "executions_failed", "executions_cancelled", "action_steps_executed",
        "avg_duration_seconds", "success_rate", "failure_rate",
    ],
    "TEAM": [],  # team report = member table; no selectable metrics
}

DIMENSION_CATALOG: dict[str, list[str]] = {
    "LEADS": ["status", "source", "source_type", "category", "industry",
              "city", "state", "country", "tag", "scraper"],
    "SCRAPING": ["actor"],
    "MARKETING": ["channel", "campaign"],
    "INBOX": ["channel", "status", "priority", "assigned_user"],
    "AUTOMATION": ["workflow"],
    "WHATSAPP": ["account"],
    "EMAIL": ["account"],
    "OVERVIEW": [],
    "TEAM": [],
}

VISUALIZATIONS = ("kpi", "table", "line", "bar", "area", "funnel", "donut")

MAX_METRICS = 12
MAX_DIMENSIONS = 3


def validate_report_config(config: dict) -> dict:
    """Validate + normalize a report configuration; returns the canonical
    form persisted on the Report row."""
    if not isinstance(config, dict):
        raise ReportConfigError("Report config must be an object")

    unknown = set(config) - {
        "domain", "metrics", "dimensions", "filters", "period", "date_from",
        "date_to", "grouping", "sort_by", "sort_dir", "visualization", "limit",
    }
    if unknown:
        raise ReportConfigError(f"Unknown report config key(s): {', '.join(sorted(unknown))}")

    domain = str(config.get("domain", "")).upper()
    if domain not in DOMAINS:
        raise ReportConfigError(f"domain must be one of: {', '.join(DOMAINS)}")

    metrics = config.get("metrics", [])
    if not isinstance(metrics, list) or not metrics:
        raise ReportConfigError("metrics must be a non-empty list")
    if len(metrics) > MAX_METRICS:
        raise ReportConfigError(f"at most {MAX_METRICS} metrics per report")
    allowed_metrics = METRIC_CATALOG[domain]
    if allowed_metrics:
        for metric in metrics:
            if metric not in allowed_metrics:
                raise ReportConfigError(
                    f"metric {metric!r} is not available for domain {domain}. "
                    f"Allowed: {', '.join(allowed_metrics)}"
                )

    dimensions = config.get("dimensions", [])
    if not isinstance(dimensions, list):
        raise ReportConfigError("dimensions must be a list")
    if len(dimensions) > MAX_DIMENSIONS:
        raise ReportConfigError(f"at most {MAX_DIMENSIONS} dimensions per report")
    allowed_dimensions = DIMENSION_CATALOG[domain]
    for dimension in dimensions:
        if dimension not in allowed_dimensions:
            raise ReportConfigError(
                f"dimension {dimension!r} is not available for domain {domain}. "
                f"Allowed: {', '.join(allowed_dimensions) or '(none)'}"
            )

    filters_payload = config.get("filters", {})
    if not isinstance(filters_payload, dict):
        raise ReportConfigError("filters must be an object")
    unknown_filters = set(filters_payload) - set(FILTER_KEYS)
    if unknown_filters:
        raise ReportConfigError(
            f"Unknown filter key(s): {', '.join(sorted(unknown_filters))}"
        )
    # full allowlist validation + normalization through the shared filter
    # module (scalar strings become value lists, values are cleaned/capped)
    normalized_filters = filters_from_payload(filters_payload, period=None)
    normalized_filter_payload = {
        key: values for key, values in normalized_filters.to_payload().items()
        if key != "period"
    }

    period = str(config.get("period", "30d")).lower()
    if period != "custom" and period not in PERIOD_PRESETS:
        raise ReportConfigError(
            f"period must be one of {', '.join(PERIOD_PRESETS)} or 'custom'"
        )
    if period == "custom":
        date_from, date_to = config.get("date_from"), config.get("date_to")
        if not date_from or not date_to:
            raise ReportConfigError("custom period requires date_from and date_to")
        if not _is_date(date_from) or not _is_date(date_to):
            raise ReportConfigError("date_from/date_to must be YYYY-MM-DD")

    grouping = config.get("grouping", "day")
    if grouping not in ("day", "none"):
        raise ReportConfigError("grouping must be 'day' or 'none'")

    sort_dir = config.get("sort_dir", "desc")
    if sort_dir not in ("asc", "desc"):
        raise ReportConfigError("sort_dir must be 'asc' or 'desc'")

    visualization = str(config.get("visualization", "table")).lower()
    if visualization not in VISUALIZATIONS:
        raise ReportConfigError(
            f"visualization must be one of: {', '.join(VISUALIZATIONS)}"
        )
    if visualization == "funnel" and domain != "LEADS":
        raise ReportConfigError("funnel visualization is only valid for LEADS")
    if visualization == "kpi" and domain == "TEAM":
        raise ReportConfigError("kpi visualization is not valid for TEAM reports")

    limit = config.get("limit", 100)
    if not isinstance(limit, int) or not (1 <= limit <= 1000):
        raise ReportConfigError("limit must be an integer between 1 and 1000")

    sort_by = config.get("sort_by")
    if sort_by is not None and (not isinstance(sort_by, str) or len(sort_by) > 50):
        raise ReportConfigError("sort_by must be a short metric/dimension name")

    return {
        "domain": domain,
        "metrics": list(metrics),
        "dimensions": list(dimensions),
        "filters": normalized_filter_payload,
        "period": period,
        "date_from": config.get("date_from"),
        "date_to": config.get("date_to"),
        "grouping": grouping,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "visualization": visualization,
        "limit": limit,
    }


def _is_date(value: str) -> bool:
    from datetime import date as date_type

    try:
        date_type.fromisoformat(str(value))
        return True
    except (TypeError, ValueError):
        return False


def validate_report_name(name: str) -> str:
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ReportConfigError(
            "Report name must be 1-200 chars: letters, digits, spaces and basic punctuation"
        )
    return name
