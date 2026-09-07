"""Analytics & Reports UI (Phase 10 §30, §31).

Server-rendered dark dashboard pages consistent with the existing QBIT
workspace: card-based KPI grids, dependency-free SVG charts (static/js/
qbit-charts.js fed by embedded JSON — no new framework, §30), filter bar with
period presets + comparison, and the saved-report builder workspace.

Permission per page is enforced server-side via the shared UI dependency;
the UI is never the security boundary (spec §30, §32).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import FILTER_KEYS
from app.analytics.reports.exporter import ReportExporter
from app.analytics.reports.executor import ReportExecutor
from app.analytics.reports.schemas import DIMENSION_CATALOG, METRIC_CATALOG
from app.analytics.reports.service import ReportService, can_manage
from app.analytics.service import AnalyticsRequest, AnalyticsService
from app.api.deps import get_db
from app.core.errors import QBITError
from app.core.path_safety import validate_storage_key
from app.models.user import User
from app.services.audit import AuditService
from app.ui import _ctx, templates
from app.ui import ui_user_for as require_ui_permission

SessionDep = Annotated[AsyncSession, Depends(get_db)]
router = APIRouter(tags=["analytics-ui"])

report_service = ReportService()

#: page slug → required analytics permission (§31 navigation)
PAGE_PERMISSIONS = {
    "": "analytics.view",
    "leads": "analytics.view_leads",
    "scraping": "analytics.view_scraping",
    "marketing": "analytics.view_marketing",
    "whatsapp": "analytics.view_whatsapp",
    "email": "analytics.view_email",
    "inbox": "analytics.view_inbox",
    "automation": "analytics.view_automation",
    "team": "analytics.view_team",
}


def _perms(request: Request) -> set[str]:
    return getattr(request.state, "ui_permissions", None) or set()


def _analytics_request(
    request: Request, *, compare_default: bool = False,
) -> AnalyticsRequest:
    qp = request.query_params
    filter_params: dict[str, list[str]] = {}
    for key in FILTER_KEYS:
        if key in qp:
            filter_params[key] = qp.getlist(key)
    return AnalyticsRequest(
        period=qp.get("period"),
        date_from=qp.get("date_from"),
        date_to=qp.get("date_to"),
        timezone=qp.get("timezone"),
        compare=qp.get("compare", "1" if compare_default else "0") == "1",
        filter_params=filter_params,
        scope="ui",
    )


def _querystring(request: Request) -> str:
    """Preserve current filters in pagination/export links."""
    params = [(k, v) for k, v in request.query_params.multi_items()
              if k != "flash"]
    if not params:
        return ""
    from urllib.parse import urlencode

    return "&" + urlencode(params)


def _error_redirect(url: str, exc: Exception) -> RedirectResponse:
    from urllib.parse import quote

    message = getattr(exc, "message", None) or str(exc)
    return RedirectResponse(f"{url}?flash={quote(message[:200])}&flash_kind=err",
                            status_code=303)


# ------------------------------------------------------------------ overview
@router.get("/analytics", response_class=HTMLResponse)
async def analytics_home(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    data = await AnalyticsService(request.app.state.redis).overview(
        session, _analytics_request(request, compare_default=True),
    )
    return templates.TemplateResponse(
        request, "analytics/overview.html", _ctx(
            request, user, data=data, active_page="",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


# ------------------------------------------------------------ domain pages
@router.get("/analytics/leads", response_class=HTMLResponse)
async def analytics_leads(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view_leads")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    data = await AnalyticsService(request.app.state.redis).leads(
        session, _analytics_request(request, compare_default=True),
    )
    funnel = await AnalyticsService(request.app.state.redis).leads_funnel(
        session, _analytics_request(request),
    )
    return templates.TemplateResponse(
        request, "analytics/leads.html", _ctx(
            request, user, data=data, funnel=funnel, active_page="leads",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


@router.get("/analytics/scraping", response_class=HTMLResponse)
async def analytics_scraping(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view_scraping")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    data = await AnalyticsService(request.app.state.redis).scraping(
        session, _analytics_request(request, compare_default=True),
    )
    return templates.TemplateResponse(
        request, "analytics/scraping.html", _ctx(
            request, user, data=data, active_page="scraping",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


@router.get("/analytics/marketing", response_class=HTMLResponse)
async def analytics_marketing(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view_marketing")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    data = await AnalyticsService(request.app.state.redis).marketing(
        session, _analytics_request(request, compare_default=True),
    )
    return templates.TemplateResponse(
        request, "analytics/marketing.html", _ctx(
            request, user, data=data, active_page="marketing",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


@router.get("/analytics/whatsapp", response_class=HTMLResponse)
async def analytics_whatsapp(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view_whatsapp")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    data = await AnalyticsService(request.app.state.redis).whatsapp(
        session, _analytics_request(request),
    )
    return templates.TemplateResponse(
        request, "analytics/whatsapp.html", _ctx(
            request, user, data=data, active_page="whatsapp",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


@router.get("/analytics/email", response_class=HTMLResponse)
async def analytics_email(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view_email")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    data = await AnalyticsService(request.app.state.redis).email(
        session, _analytics_request(request),
    )
    return templates.TemplateResponse(
        request, "analytics/email.html", _ctx(
            request, user, data=data, active_page="email",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


@router.get("/analytics/inbox", response_class=HTMLResponse)
async def analytics_inbox(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view_inbox")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    data = await AnalyticsService(request.app.state.redis).inbox(
        session, _analytics_request(request, compare_default=True),
    )
    return templates.TemplateResponse(
        request, "analytics/inbox.html", _ctx(
            request, user, data=data, active_page="inbox",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


@router.get("/analytics/automation", response_class=HTMLResponse)
async def analytics_automation(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view_automation")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    data = await AnalyticsService(request.app.state.redis).automation(
        session, _analytics_request(request),
    )
    return templates.TemplateResponse(
        request, "analytics/automation.html", _ctx(
            request, user, data=data, active_page="automation",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


@router.get("/analytics/team", response_class=HTMLResponse)
async def analytics_team(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("analytics.view_team")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    from sqlalchemy import select as sa_select

    from app.models.user import User as UserModel

    visibility = request.app.state.settings.QBIT_INBOX_VISIBILITY
    if visibility == "ASSIGNED_ONLY":
        allowed = [user.id]
    else:
        allowed = list((await session.execute(sa_select(UserModel.id))).scalars().all())
    data = await AnalyticsService(request.app.state.redis).team(
        session, _analytics_request(request), allowed_user_ids=allowed,
    )
    return templates.TemplateResponse(
        request, "analytics/team.html", _ctx(
            request, user, data=data, active_page="team",
            querystring=_querystring(request), flash=flash, flash_kind=flash_kind,
            perms=_perms(request),
        ),
    )


# ------------------------------------------------------------- report pages
@router.get("/reports", response_class=HTMLResponse)
async def reports_home(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("reports.view")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    rows, total = await report_service.list(session, user=user,
                                            permissions=_perms(request), page=1,
                                            page_size=100)
    return templates.TemplateResponse(
        request, "analytics/reports.html", _ctx(
            request, user, reports=rows, total=total,
            metric_catalog=METRIC_CATALOG, dimension_catalog=DIMENSION_CATALOG,
            flash=flash, flash_kind=flash_kind, perms=_perms(request),
        ),
    )


@router.post("/reports", response_class=HTMLResponse)
async def reports_create(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("reports.create")),
):
    form = await request.form()
    audit = AuditService()
    try:
        config = _report_config_from_form(form)
        report = await report_service.create(
            session, name=str(form.get("name", "")),
            description=str(form.get("description", "")) or None,
            config=config,
            visibility=str(form.get("visibility", "PRIVATE")),
            owner=user, timezone=str(form.get("timezone", "UTC")),
        )
    except QBITError as exc:
        return _error_redirect("/reports", exc)
    await audit.log(session, action="reports.report_created", actor_user_id=user.id,
                    resource_type="report", resource_id=str(report.id),
                    metadata={"name": report.name, "domain": report.domain,
                              "via": "ui"})
    return RedirectResponse(f"/reports/{report.id}", status_code=303)


def _report_config_from_form(form) -> dict:
    """Build a validated-able config payload from the builder form."""
    domain = str(form.get("domain", "")).upper()
    metrics = form.getlist("metrics")
    dimensions = form.getlist("dimensions")
    visualization = str(form.get("visualization", "table"))
    period = str(form.get("period", "30d"))
    config: dict = {
        "domain": domain,
        "metrics": [m for m in metrics if m],
        "dimensions": [d for d in dimensions if d],
        "visualization": visualization,
        "period": period,
        "grouping": str(form.get("grouping", "day")),
        "sort_dir": str(form.get("sort_dir", "desc")),
        "limit": int(str(form.get("limit", "100")) or 100),
    }
    if period == "custom":
        config["date_from"] = str(form.get("date_from", ""))
        config["date_to"] = str(form.get("date_to", ""))
    # optional single-value filters from the form (allowlist-checked downstream)
    for key in FILTER_KEYS:
        raw = str(form.get(f"filter_{key}", "") or "").strip()
        if raw:
            config.setdefault("filters", {})[key] = [raw]
    return config


@router.get("/reports/new", response_class=HTMLResponse)
async def reports_new(
    request: Request,
    user: User = Depends(require_ui_permission("reports.create")),
):
    return templates.TemplateResponse(
        request, "analytics/report_form.html", _ctx(
            request, user, report=None, metric_catalog=METRIC_CATALOG,
            dimension_catalog=DIMENSION_CATALOG, perms=_perms(request),
        ),
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse)
async def report_detail(
    request: Request,
    session: SessionDep,
    report_id: uuid.UUID,
    user: User = Depends(require_ui_permission("reports.view")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    report = await report_service.get_visible(session, report_id, user=user,
                                              permissions=_perms(request))
    runs, total = await report_service.list_runs(session, report, page=1, page_size=15)
    snapshot = await report_service.latest_snapshot(session, report)
    return templates.TemplateResponse(
        request, "analytics/report_detail.html", _ctx(
            request, user, report=report, runs=runs, runs_total=total,
            snapshot=snapshot,
            can_manage=can_manage(report, user, _perms(request)),
            can_run="reports.run" in _perms(request),
            can_export="reports.export" in _perms(request),
            flash=flash, flash_kind=flash_kind, perms=_perms(request),
        ),
    )


@router.post("/reports/{report_id}/run")
async def report_run_ui(
    request: Request,
    session: SessionDep,
    report_id: uuid.UUID,
    user: User = Depends(require_ui_permission("reports.run")),
):
    audit = AuditService()
    try:
        report = await report_service.get_visible(session, report_id, user=user,
                                                  permissions=_perms(request))
        run = await report_service.queue_run(session, report, user=user)
    except QBITError as exc:
        return _error_redirect(f"/reports/{report_id}", exc)
    await audit.log(session, action="reports.report_executed", actor_user_id=user.id,
                    resource_type="report_run", resource_id=str(run.id),
                    metadata={"report_id": str(report.id), "via": "ui"})
    return RedirectResponse(f"/reports/{report_id}", status_code=303)


@router.post("/reports/{report_id}/edit")
async def report_edit_ui(
    request: Request,
    session: SessionDep,
    report_id: uuid.UUID,
    user: User = Depends(require_ui_permission("reports.edit")),
):
    form = await request.form()
    audit = AuditService()
    try:
        report = await report_service.get_manageable(session, report_id, user=user,
                                                     permissions=_perms(request))
        config = _report_config_from_form(form)
        report = await report_service.update(
            session, report,
            name=str(form.get("name", report.name)),
            description=str(form.get("description", "")) or None,
            config=config,
            visibility=str(form.get("visibility", report.visibility)),
            timezone=str(form.get("timezone", report.timezone)) or None,
        )
    except QBITError as exc:
        return _error_redirect(f"/reports/{report_id}", exc)
    await audit.log(session, action="reports.report_edited", actor_user_id=user.id,
                    resource_type="report", resource_id=str(report.id),
                    metadata={"via": "ui"})
    return RedirectResponse(f"/reports/{report.id}", status_code=303)


@router.get("/reports/{report_id}/edit", response_class=HTMLResponse)
async def report_edit_page(
    request: Request,
    session: SessionDep,
    report_id: uuid.UUID,
    user: User = Depends(require_ui_permission("reports.edit")),
):
    report = await report_service.get_manageable(session, report_id, user=user,
                                                 permissions=_perms(request))
    return templates.TemplateResponse(
        request, "analytics/report_form.html", _ctx(
            request, user, report=report, metric_catalog=METRIC_CATALOG,
            dimension_catalog=DIMENSION_CATALOG, perms=_perms(request),
        ),
    )


@router.post("/reports/{report_id}/duplicate")
async def report_duplicate_ui(
    request: Request,
    session: SessionDep,
    report_id: uuid.UUID,
    user: User = Depends(require_ui_permission("reports.create")),
):
    audit = AuditService()
    try:
        report = await report_service.get_visible(session, report_id, user=user,
                                                  permissions=_perms(request))
        copy = await report_service.duplicate(session, report, user=user)
    except QBITError as exc:
        return _error_redirect("/reports", exc)
    await audit.log(session, action="reports.report_duplicated",
                    actor_user_id=user.id, resource_type="report",
                    resource_id=str(copy.id), metadata={"via": "ui"})
    return RedirectResponse(f"/reports/{copy.id}", status_code=303)


async def _report_status_change(
    request: Request, session: AsyncSession, report_id: uuid.UUID, user: User,
    action: str,
):
    audit = AuditService()
    try:
        report = await report_service.get_manageable(session, report_id, user=user,
                                                     permissions=_perms(request))
        if action == "archive":
            await report_service.archive(session, report)
        elif action == "restore":
            await report_service.restore(session, report)
        elif action == "delete":
            await report_service.delete(session, report)
    except QBITError as exc:
        return _error_redirect("/reports", exc)
    await audit.log(session, action=f"reports.report_{action}d",
                    actor_user_id=user.id, resource_type="report",
                    resource_id=str(report_id), metadata={"via": "ui"})
    return RedirectResponse("/reports", status_code=303)


@router.post("/reports/{report_id}/archive")
async def report_archive_ui(request: Request, session: SessionDep,
                            report_id: uuid.UUID,
                            user: User = Depends(require_ui_permission("reports.edit"))):
    return await _report_status_change(request, session, report_id, user, "archive")


@router.post("/reports/{report_id}/restore")
async def report_restore_ui(request: Request, session: SessionDep,
                            report_id: uuid.UUID,
                            user: User = Depends(require_ui_permission("reports.edit"))):
    return await _report_status_change(request, session, report_id, user, "restore")


@router.post("/reports/{report_id}/delete")
async def report_delete_ui(request: Request, session: SessionDep,
                           report_id: uuid.UUID,
                           user: User = Depends(require_ui_permission("reports.delete"))):
    return await _report_status_change(request, session, report_id, user, "delete")


@router.get("/reports/{report_id}/export")
async def report_export_ui(
    request: Request,
    session: SessionDep,
    report_id: uuid.UUID,
    user: User = Depends(require_ui_permission("reports.export")),
    format: str = Query(default="csv", pattern=r"^(csv|xlsx|json)$"),
    run_id: uuid.UUID | None = None,
):
    from fastapi.responses import Response

    audit = AuditService()
    try:
        report = await report_service.get_visible(session, report_id, user=user,
                                                  permissions=_perms(request))
        if run_id is not None:
            snapshot = await report_service.snapshot_for_run(session, report, run_id)
        else:
            snapshot = await report_service.latest_snapshot(session, report)
        if snapshot is None:
            return _error_redirect(f"/reports/{report_id}", QBITError(
                "No completed run to export yet — run the report first"))
        record = await ReportExporter(request.app.state.files).export_snapshot(
            session, report, snapshot, format_name=format, requested_by=user.id,
        )
    except QBITError as exc:
        return _error_redirect(f"/reports/{report_id}", exc)
    await audit.log(session, action="reports.report_exported", actor_user_id=user.id,
                    resource_type="report", resource_id=str(report.id),
                    metadata={"format": format, "via": "ui"})
    root = request.app.state.storage.root_for("EXPORT")
    path = validate_storage_key(record.path, root)
    media = {
        "csv": "text/csv",
        "json": "application/json",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return Response(
        content=path.read_bytes(),
        media_type=media.get(format, "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{record.name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# ------------------------------------------------------------ preview JSON
@router.post("/reports/preview")
async def report_preview(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("reports.create")),
):
    """Live builder preview: validates the form config and returns computed
    rows WITHOUT persisting anything."""
    form = await request.form()
    try:
        config = _report_config_from_form(form)
        from app.analytics.reports.schemas import validate_report_config

        config = validate_report_config(config)
    except QBITError as exc:
        return {"success": False, "error": getattr(exc, "message", str(exc))}
    run_like = _preview_run(config, user)
    try:
        executor = ReportExecutor(
            dialect=session.bind.dialect.name if session.bind is not None else "sqlite"
        )
        result = await executor.compute(session, run_like)
    except QBITError as exc:
        return {"success": False, "error": getattr(exc, "message", str(exc))}
    return {"success": True, "data": result}


def _preview_run(config: dict, user: User):
    from app.models.analytics import ReportRun, ReportRunStatus

    return ReportRun(
        report_id=uuid.uuid4(),
        status=ReportRunStatus.RUNNING,
        requested_by=user.id,
        config_snapshot=config,
        config_version=1,
        timezone=str(config.get("timezone", "UTC")) if config.get("timezone") else "UTC",
        format="json",
    )
