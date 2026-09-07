"""Global dashboard overview (spec §4) — cross-domain KPI cards with
comparison vs the previous equivalent period (spec §4, §5).

Every value comes from the domain modules (same queries as the dedicated
domain pages); this module only orchestrates and computes comparisons, so a
metric can never mean one thing on the dashboard and another on its own page.

Counting rules that prevent double-counting:
- "messages sent / delivered / replies" come from the CAMPAIGN event stream
  (spec §9) — the same provider delivery is NOT also counted again from
  inbox mirrors; inbox-level volumes live on /analytics/inbox and /analytics/
  whatsapp where they are the primary metric
- conversations counted by status; open = OPEN + PENDING + WAITING
"""

from __future__ import annotations

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.math import comparison
from app.analytics.domains import automation, inbox, leads, marketing, scraping


def _cmp(current: dict, previous: dict | None, keys: list[str]) -> dict:
    out = {}
    for key in keys:
        prev_value = None
        if previous is not None:
            prev_value = previous.get(key)
        out[key] = comparison(current.get(key, 0), prev_value)
    return out


async def overview_kpis(
    session,
    current_filters: AnalyticsFilters,
    previous_filters: AnalyticsFilters | None,
) -> dict:
    current = await _snapshot(session, current_filters)
    previous = None
    if previous_filters is not None:
        previous = await _snapshot(session, previous_filters)

    kpi_keys = [
        "total_leads", "new_leads", "qualified_leads", "contacted_leads",
        "interested_leads", "converted_leads", "active_campaigns",
        "messages_sent", "messages_delivered", "replies",
        "open_conversations", "automation_executions",
    ]
    return {
        "period": current_filters.period.to_dict() if current_filters.period else None,
        "kpis": _cmp(current, previous, kpi_keys),
        "scraping": current["scraping"],
        "campaigns_by_status": current["campaigns_by_status"],
        "unsubscribed": current["unsubscribed"],
        "inbox_by_status": current["inbox_by_status"],
    }


async def _snapshot(session, filters: AnalyticsFilters) -> dict:
    lead_kpis = await leads.lead_kpis(session, filters)
    marketing_kpis = await marketing.marketing_kpis(session, filters)
    inbox_kpis = await inbox.inbox_kpis(session, filters)
    automation_kpis = await automation.automation_kpis(session, filters)
    scraping_kpis = await scraping.scraping_kpis(session, filters)

    return {
        "total_leads": lead_kpis["total"],
        "new_leads": lead_kpis["new"],
        "qualified_leads": lead_kpis["qualified"],
        "contacted_leads": lead_kpis["contacted"],
        "interested_leads": lead_kpis["interested"],
        "converted_leads": lead_kpis["converted"],
        "active_campaigns": marketing_kpis["campaigns_running"],
        "messages_sent": marketing_kpis["messages_sent"],
        "messages_delivered": marketing_kpis["messages_delivered"],
        "replies": marketing_kpis["replies"],
        "open_conversations": (
            inbox_kpis["open"] + inbox_kpis["pending"] + inbox_kpis["waiting"]
        ),
        "automation_executions": automation_kpis["executions"],
        "unsubscribed": marketing_kpis["unsubscribed"],
        "inbox_by_status": inbox_kpis["by_status"],
        "campaigns_by_status": marketing_kpis["by_status"],
        "scraping": {
            "jobs_total": scraping_kpis["jobs_total"],
            "records_accepted": scraping_kpis["records_accepted"],
            "success_rate": scraping_kpis["success_rate"],
        },
    }
