"""Analytics API (Phase 10 §24).

All routes are JWT-authenticated and enforce granular `analytics.*`
permissions SERVER-SIDE. Analytics is read-only against operational data;
the only mutating endpoints are the admin aggregate-rebuild and cache-clear
actions (both `analytics.manage`, both audit-logged — spec §26, §27).

Responses never contain provider credentials or secrets (spec §32): the
domain modules query operational tables only and never touch the credential
vault, connection secrets or token storage.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.analytics.aggregation import AggregationService, run_diagnostics
from app.analytics.core.cache import AnalyticsCache
from app.analytics.core.exceptions import AnalyticsValidationError
from app.analytics.core.filters import FILTER_KEYS
from app.analytics.core.time import PERIOD_PRESETS
from app.analytics.service import AnalyticsRequest, AnalyticsService
from app.api.deps import AuditDep, DbSession, require_permission
from app.models.user import User

router = APIRouter(prefix="/analytics", tags=["analytics"])

_TEAM_PERMISSION = "analytics.view_team"


def _service(request: Request) -> AnalyticsService:
    return AnalyticsService(request.app.state.redis)


#: non-filter query params accepted on analytics endpoints
_RESERVED_PARAMS = {"period", "date_from", "date_to", "timezone", "compare"}


def _request_params(
    request: Request,
    *,
    period: str | None,
    date_from: str | None,
    date_to: str | None,
    timezone: str | None,
    compare: bool,
    scope: str = "global",
) -> AnalyticsRequest:
    """Pull allowlisted filters from query params. Unknown query params are
    REJECTED (never silently ignored — explicit is safer, spec §32)."""
    filter_params: dict[str, list[str]] = {}
    for key in request.query_params.keys():
        if key in _RESERVED_PARAMS:
            continue
        if key not in FILTER_KEYS:
            raise AnalyticsValidationError(f"Unknown filter(s): {key}")
        filter_params[key] = request.query_params.getlist(key)
    return AnalyticsRequest(
        period=period, date_from=date_from, date_to=date_to,
        timezone=timezone, compare=compare,
        filter_params=filter_params, scope=scope,
    )


_PERIOD_DESC = " | ".join(PERIOD_PRESETS) + " | custom (with date_from/date_to)"


# ----------------------------------------------------------------- overview
@router.get("/overview")
async def overview(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None, description=_PERIOD_DESC),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    compare: bool = Query(default=False),
    user: User = Depends(require_permission("analytics.view")),
):
    data = await _service(request).overview(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=compare),
    )
    return {"success": True, "data": data}


# -------------------------------------------------------------------- leads
@router.get("/leads")
async def leads_analytics(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    compare: bool = Query(default=False),
    user: User = Depends(require_permission("analytics.view_leads")),
):
    data = await _service(request).leads(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=compare),
    )
    return {"success": True, "data": data}


@router.get("/leads/sources")
async def lead_sources(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    sort: str = Query(default="leads", pattern=r"^(leads|quality|qualification|conversion)$"),
    user: User = Depends(require_permission("analytics.view_leads")),
):
    data = await _service(request).leads_sources(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=False),
        sort=sort,
    )
    return {"success": True, "data": data}


@router.get("/leads/funnel")
async def lead_funnel(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    user: User = Depends(require_permission("analytics.view_leads")),
):
    data = await _service(request).leads_funnel(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=False),
    )
    return {"success": True, "data": data}


# ----------------------------------------------------------------- scraping
@router.get("/scraping")
async def scraping_analytics(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    compare: bool = Query(default=False),
    user: User = Depends(require_permission("analytics.view_scraping")),
):
    data = await _service(request).scraping(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=compare),
    )
    return {"success": True, "data": data}


@router.get("/scraping/scrapers")
async def scraping_scrapers(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    user: User = Depends(require_permission("analytics.view_scraping")),
):
    params = _request_params(request, period=period, date_from=date_from,
                             date_to=date_to, timezone=timezone, compare=False)
    data = await _service(request).scraping(session, params)
    return {"success": True, "data": {"scrapers": data.get("scrapers", []),
                                      "period": data.get("period")}}


# ---------------------------------------------------------------- marketing
@router.get("/marketing")
async def marketing_analytics(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    compare: bool = Query(default=False),
    user: User = Depends(require_permission("analytics.view_marketing")),
):
    data = await _service(request).marketing(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=compare),
    )
    return {"success": True, "data": data}


@router.get("/campaigns/{campaign_id}")
async def campaign_analytics(
    request: Request,
    session: DbSession,
    campaign_id: str,
    user: User = Depends(require_permission("campaigns.analytics")),
):
    """Campaign detail reuses the existing Phase 5/7 analytics service so the
    campaign page and this endpoint can never disagree (spec §10, §28)."""
    import uuid as uuid_module

    try:
        campaign_uuid = uuid_module.UUID(campaign_id)
    except ValueError as exc:
        raise AnalyticsValidationError("campaign_id must be a UUID") from exc
    data = await _service(request).campaign_detail(session, campaign_uuid)
    if data is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("Campaign not found")
    return {"success": True, "data": data}


# ------------------------------------------------------- whatsapp and email
@router.get("/whatsapp")
async def whatsapp_analytics(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    user: User = Depends(require_permission("analytics.view_whatsapp")),
):
    data = await _service(request).whatsapp(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=False),
    )
    return {"success": True, "data": data}


@router.get("/email")
async def email_analytics(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    user: User = Depends(require_permission("analytics.view_email")),
):
    data = await _service(request).email(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=False),
    )
    return {"success": True, "data": data}


# -------------------------------------------------------------------- inbox
@router.get("/inbox")
async def inbox_analytics(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    compare: bool = Query(default=False),
    user: User = Depends(require_permission("analytics.view_inbox")),
):
    data = await _service(request).inbox(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=compare),
    )
    return {"success": True, "data": data}


# --------------------------------------------------------------------- team
@router.get("/team")
async def team_analytics(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    user: User = Depends(require_permission(_TEAM_PERMISSION)),
):
    """Team analytics scoped by the established visibility rules (spec §14):
    ASSIGNED_ONLY deployments see only the requesting user's own activity."""
    from sqlalchemy import select as sa_select

    from app.models.user import User as UserModel

    settings = request.app.state.settings
    visibility = getattr(settings, "QBIT_INBOX_VISIBILITY", "ALL")
    if visibility == "ASSIGNED_ONLY":
        allowed = [user.id]
    else:
        rows = (await session.execute(sa_select(UserModel.id))).scalars().all()
        allowed = list(rows)
    data = await _service(request).team(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=False,
                                 scope=str(user.id)),
        allowed_user_ids=allowed,
    )
    return {"success": True, "data": data}


# --------------------------------------------------------------- automation
@router.get("/automation")
async def automation_analytics(
    request: Request,
    session: DbSession,
    period: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    timezone: str | None = Query(default=None, max_length=64),
    user: User = Depends(require_permission("analytics.view_automation")),
):
    data = await _service(request).automation(
        session, _request_params(request, period=period, date_from=date_from,
                                 date_to=date_to, timezone=timezone, compare=False),
    )
    return {"success": True, "data": data}


# ----------------------------------------------------- admin: aggregates etc
@router.get("/aggregates/status")
async def aggregates_status(
    request: Request,
    session: DbSession,
    user: User = Depends(require_permission("analytics.manage")),
):
    service = AggregationService(dialect=session.bind.dialect.name
                                 if session.bind is not None else "sqlite")
    return {"success": True, "data": {"recent_runs": await service.recent_runs(session)}}


class RebuildIn(BaseModel):
    domains: list[str] | None = None
    day_start: date | None = None
    day_end: date | None = None


@router.post("/aggregates/rebuild")
async def aggregates_rebuild(
    request: Request,
    session: DbSession,
    body: RebuildIn,
    audit: AuditDep,
    user: User = Depends(require_permission("analytics.manage")),
):
    """Manual aggregate rebuild for administrators (spec §21). NEVER deletes
    operational data — only derived aggregate rows are recomputed (§39)."""
    from datetime import datetime, timedelta, timezone as dt_timezone

    service = AggregationService(dialect=session.bind.dialect.name
                                 if session.bind is not None else "sqlite")
    day_end = body.day_end or (datetime.now(dt_timezone.utc).date() - timedelta(days=1))
    day_start = body.day_start or (day_end - timedelta(days=29))
    try:
        results = await service.rebuild_range(
            session, day_start, day_end, domains=body.domains, triggered_by="MANUAL",
        )
    except ValueError as exc:
        raise AnalyticsValidationError(str(exc)) from exc
    await audit.log(
        session, action="analytics.aggregate_rebuilt", actor_user_id=user.id,
        resource_type="analytics_aggregates",
        metadata={"day_start": day_start.isoformat(), "day_end": day_end.isoformat(),
                  "domains": body.domains},
    )
    return {"success": True, "data": {"results": results}}


@router.post("/cache/clear")
async def cache_clear(
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user: User = Depends(require_permission("analytics.manage")),
):
    cache = AnalyticsCache(request.app.state.redis)
    removed = await cache.clear_all()
    await audit.log(
        session, action="analytics.cache_cleared", actor_user_id=user.id,
        resource_type="analytics_cache", metadata={"entries_removed": removed},
    )
    return {"success": True, "data": {"entries_removed": removed}}


@router.get("/diagnostics")
async def diagnostics(
    request: Request,
    session: DbSession,
    user: User = Depends(require_permission("analytics.manage")),
):
    """Data-quality checks (spec §29): flag anomalies, never modify data."""
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    return {"success": True, "data": await run_diagnostics(session, dialect)}
