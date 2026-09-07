"""AnalyticsService facade — one entry point for every analytics read.

Responsibilities (spec §3, §5, §22, §23):
- resolve periods (incl. comparison windows) once, identically for all domains
- build allowlisted filters once and pass THE SAME object to every domain call
- dialect-aware day bucketing (PostgreSQL tz db / SQLite fixed offset)
- Redis caching with filter+scope-derived keys (scope = caller-supplied
  authorization scope string, e.g. team visibility set hash)
- read-only: this service never writes anything
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.cache import AnalyticsCache, TTL_DASHBOARD, TTL_DISTRIBUTION
from app.analytics.core.filters import AnalyticsFilters, filters_from_params
from app.analytics.core.time import Period, previous_period, resolve_period
from app.analytics.domains import (
    automation,
    inbox,
    leads as leads_domain,
    marketing as marketing_domain,
    overview as overview_domain,
    scraping as scraping_domain,
    whatsapp as whatsapp_domain,
    email as email_domain,
    team as team_domain,
)


@dataclass
class AnalyticsRequest:
    """Parsed query parameters for one analytics call."""

    period: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    timezone: str | None = None
    compare: bool = False
    filter_params: dict = field(default_factory=dict)
    scope: str = "global"

    def resolve(self) -> tuple[Period, AnalyticsFilters, AnalyticsFilters | None]:
        period = resolve_period(
            period=self.period, date_from=self.date_from, date_to=self.date_to,
            tz_name=self.timezone,
        )
        filters = filters_from_params(self.filter_params, period)
        prev_filters = None
        if self.compare:
            prev = previous_period(period)
            if prev is not None:
                prev_filters = filters_from_params(self.filter_params, prev)
        return period, filters, prev_filters


class AnalyticsService:
    def __init__(self, redis_manager=None) -> None:
        self._redis = redis_manager
        self._cache = AnalyticsCache(redis_manager) if redis_manager else None

    # ------------------------------------------------------------- internals
    def _dialect(self, session: AsyncSession) -> str:
        try:
            return session.bind.dialect.name  # type: ignore[union-attr]
        except AttributeError:  # pragma: no cover
            return "sqlite"

    def _key(self, domain: str, period: Period, filters: AnalyticsFilters,
             scope: str, extra: dict | None = None) -> str:
        payload = {"filters": filters.to_payload(), "extra": extra or {}}
        return self._cache.build_key(domain, payload, scope=scope) if self._cache else domain

    async def _cached(self, key: str, factory, ttl: int):
        if self._cache is None:
            return await factory()
        return await self._cache.get_or_set(key, factory, ttl)

    # ---------------------------------------------------------------- domains
    async def overview(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, prev_filters = request.resolve()

        async def compute():
            return await overview_domain.overview_kpis(
                session, filters, prev_filters if request.compare else None,
            )

        key = self._key("overview", period, filters, request.scope,
                        {"compare": request.compare})
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}

    async def leads(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, prev_filters = request.resolve()
        dialect = self._dialect(session)
        compare = request.compare and prev_filters is not None

        async def compute():
            current = await leads_domain.lead_kpis(session, filters)
            timeseries = await leads_domain.leads_timeseries(session, filters, _tz(period), dialect)
            payload = {
                "kpis": current,
                "timeseries": timeseries,
                "by_status": await leads_domain.distribution(session, filters, "status"),
                "by_source": await leads_domain.distribution(session, filters, "source"),
                "by_category": await leads_domain.distribution(session, filters, "category"),
                "by_industry": await leads_domain.distribution(session, filters, "industry"),
                "by_city": await leads_domain.distribution(session, filters, "city"),
                "by_state": await leads_domain.distribution(session, filters, "state"),
                "by_country": await leads_domain.distribution(session, filters, "country"),
                "by_tag": await leads_domain.tags_distribution(session, filters),
                "by_scraper": await leads_domain.distribution(session, filters, "scraper"),
            }
            if compare:
                previous = await leads_domain.lead_kpis(session, prev_filters)
                payload["comparison"] = {
                    key: _comparison(current.get(key, 0), (previous or {}).get(key))
                    for key in ("total", "new", "verified", "qualified", "contacted",
                                "replied", "interested", "converted", "lost", "archived")
                }
            return payload

        key = self._key("leads", period, filters, request.scope,
                        {"compare": request.compare})
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}

    async def leads_sources(self, session: AsyncSession, request: AnalyticsRequest,
                            sort: str = "leads") -> dict:
        period, filters, _prev = request.resolve()

        async def compute():
            return {"sources": await leads_domain.source_performance(session, filters)}

        key = self._key("leads-sources", period, filters, request.scope, {"sort": sort})
        data = await self._cached(key, compute, TTL_DISTRIBUTION)
        rows = data["sources"]
        sort_keys = {
            "leads": lambda r: r["leads"],
            "quality": lambda r: r["avg_quality"] or -1,
            "qualification": lambda r: r["qualification_rate"] or -1,
            "conversion": lambda r: r["conversion_rate"] or -1,
        }
        rows = sorted(rows, key=sort_keys.get(sort, sort_keys["leads"]), reverse=True)
        return {"period": period.to_dict(), "sort": sort, "sources": rows}

    async def leads_funnel(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, _prev = request.resolve()

        async def compute():
            return await leads_domain.funnel(session, filters)

        key = self._key("leads-funnel", period, filters, request.scope)
        data = await self._cached(key, compute, TTL_DISTRIBUTION)
        return {"period": period.to_dict(), **data}

    async def scraping(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, prev_filters = request.resolve()
        dialect = self._dialect(session)
        compare = request.compare and prev_filters is not None

        async def compute():
            current = await scraping_domain.scraping_kpis(session, filters)
            payload = {
                "kpis": current,
                "timeseries": await scraping_domain.jobs_timeseries(session, filters, _tz(period), dialect),
                "scrapers": await scraping_domain.scraper_performance(session, filters),
                "errors": await scraping_domain.error_distribution(session, filters),
                "avg_runtime_by_actor": await scraping_domain.runtime_distribution(session, filters),
            }
            if compare:
                previous = await scraping_domain.scraping_kpis(session, prev_filters)
                payload["comparison"] = {
                    key: _comparison(current.get(key, 0), (previous or {}).get(key))
                    for key in ("jobs_total", "completed", "failed", "records_accepted",
                                "records_duplicate", "records_rejected")
                }
            return payload

        key = self._key("scraping", period, filters, request.scope,
                        {"compare": request.compare})
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}

    async def marketing(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, prev_filters = request.resolve()
        dialect = self._dialect(session)
        compare = request.compare and prev_filters is not None

        async def compute():
            current = await marketing_domain.marketing_kpis(session, filters)
            payload = {
                "kpis": current,
                "timeseries": await marketing_domain.campaigns_timeseries(session, filters, _tz(period), dialect),
                "channels": await marketing_domain.channel_comparison(session, filters),
                "campaigns": await marketing_domain.campaign_list_performance(session, filters),
            }
            if compare:
                previous = await marketing_domain.marketing_kpis(session, prev_filters)
                payload["comparison"] = {
                    key: _comparison(current.get(key, 0), (previous or {}).get(key))
                    for key in ("campaigns_total", "recipients", "messages_sent",
                                "messages_delivered", "messages_failed", "replies",
                                "unsubscribed")
                }
            return payload

        key = self._key("marketing", period, filters, request.scope,
                        {"compare": request.compare})
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}

    async def campaign_detail(self, session: AsyncSession, campaign_id,
                              tz_name: str = "UTC"):
        """Campaign analytics = existing Phase 5/7 definitions (REUSED, so the
        dashboard and the campaign page can never disagree) + event timeline
        from this module (spec §10)."""
        from zoneinfo import ZoneInfo

        from app.models.marketing import Campaign
        from app.services.marketing.analytics import AnalyticsService as Phase5Service

        tzinfo = ZoneInfo(tz_name)
        base = await Phase5Service().campaign_analytics(session, campaign_id)
        if not base:
            return None
        campaign = await session.get(Campaign, campaign_id)
        if campaign and campaign.channel == "EMAIL":
            base = await Phase5Service().email_campaign_analytics(session, campaign_id)
        base["timeline"] = await marketing_domain.campaign_timeline(
            session, campaign_id, tzinfo, self._dialect(session),
        )
        return base

    async def whatsapp(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, _prev = request.resolve()
        dialect = self._dialect(session)

        async def compute():
            return {
                "kpis": await whatsapp_domain.whatsapp_kpis(session, filters),
                "accounts": await whatsapp_domain.per_account(session, filters),
                "activity": await whatsapp_domain.response_activity(session, filters, _tz(period), dialect),
            }

        key = self._key("whatsapp", period, filters, request.scope)
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}

    async def email(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, _prev = request.resolve()

        async def compute():
            return {
                "kpis": await email_domain.email_kpis(session, filters),
                "accounts": await email_domain.per_sender_account(session, filters),
            }

        key = self._key("email", period, filters, request.scope)
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}

    async def inbox(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, prev_filters = request.resolve()
        dialect = self._dialect(session)
        compare = request.compare and prev_filters is not None

        async def compute():
            current = await inbox.inbox_kpis(session, filters)
            payload = {
                "kpis": current,
                "timeseries": await inbox.conversations_timeseries(session, filters, _tz(period), dialect),
                "by_channel": await inbox.conversation_distribution(session, filters, "channel"),
                "by_status": await inbox.conversation_distribution(session, filters, "status"),
                "by_priority": await inbox.conversation_distribution(session, filters, "priority"),
                "by_assignee": await inbox.conversation_distribution(session, filters, "assigned_user"),
                "response": await inbox.response_analytics(session, filters),
            }
            if compare:
                previous = await inbox.inbox_kpis(session, prev_filters)
                payload["comparison"] = {
                    key: _comparison(current.get(key, 0), (previous or {}).get(key))
                    for key in ("conversations_total", "open", "resolved", "closed",
                                "assigned", "unassigned")
                }
            return payload

        key = self._key("inbox", period, filters, request.scope,
                        {"compare": request.compare})
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}

    async def automation(self, session: AsyncSession, request: AnalyticsRequest) -> dict:
        period, filters, _prev = request.resolve()
        dialect = self._dialect(session)

        async def compute():
            return {
                "kpis": await automation.automation_kpis(session, filters),
                "timeseries": await automation.executions_timeseries(session, filters, _tz(period), dialect),
                "top_workflows": await automation.most_executed(session, filters),
                "failure_reasons": await automation.failure_reasons(session, filters),
                "action_frequency": await automation.action_frequency(session, filters),
            }

        key = self._key("automation", period, filters, request.scope)
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}

    async def team(self, session: AsyncSession, request: AnalyticsRequest,
                   allowed_user_ids: list) -> dict:
        """Team analytics — caller supplies the visibility-scoped user set
        (spec §14); an empty set yields an empty result, never other users'
        data."""
        period, filters, _prev = request.resolve()

        async def compute():
            return {"members": await team_domain.team_overview(session, filters, allowed_user_ids)}

        scope = f"team:{request.scope}" if request.scope != "global" else "team"
        key = self._key("team", period, filters, scope)
        data = await self._cached(key, compute, TTL_DASHBOARD)
        return {"period": period.to_dict(), **data}


# ------------------------------------------------------------------ helpers
def _tz(period: Period):
    """ZoneInfo object for the period's timezone (SQL day bucketing needs the
    zone, not its name)."""
    from app.analytics.core.time import resolve_tz

    return resolve_tz(period.tz)


def _comparison(current, previous) -> dict:
    from app.analytics.core.math import comparison

    return comparison(current, previous)
