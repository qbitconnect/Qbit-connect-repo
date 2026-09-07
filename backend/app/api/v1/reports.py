"""Reports API (Phase 10 §17–§19, §24, §26, §27).

- saved-report CRUD with strict allowlisted configuration validation
- background execution via ReportRun + ReportWorker (QUEUED → RUNNING →
  COMPLETED/FAILED) — never executed inline on the request path
- visibility + ownership enforced server-side; foreign PRIVATE reports 404
  (no existence leak, spec §32)
- every mutating action is audit-logged (spec §27)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app.analytics.core.exceptions import ReportConfigError
from app.analytics.reports.exporter import ReportExporter
from app.analytics.reports.service import ReportService
from app.api.deps import AuditDep, DbSession, require_permission
from app.core.errors import NotFoundError, ValidationError
from app.core.path_safety import validate_storage_key
from app.models.analytics import ReportRun
from app.models.user import User

router = APIRouter(prefix="/reports", tags=["reports"])

service = ReportService()


def _permissions(request: Request) -> set[str]:
    perms = getattr(request.state, "permissions", None)
    if perms is None:  # pragma: no cover — require_permission always caches
        return set()
    return perms


def _page_envelope(items: list, total: int, page: int, page_size: int) -> dict:
    total_pages = (total + page_size - 1) // page_size if page_size else 1
    return {
        "items": items, "total": total, "page": page, "page_size": page_size,
        "total_pages": max(total_pages, 1),
    }


# ------------------------------------------------------------------ schemas
class ReportCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    domain: str = Field(min_length=2, max_length=20)
    config: dict = Field(default_factory=dict)
    visibility: str = Field(default="PRIVATE", max_length=10)
    timezone: str = Field(default="UTC", max_length=64)


class ReportUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    config: dict | None = None
    visibility: str | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=64)


class ReportRunIn(BaseModel):
    format: str = Field(default="json", pattern=r"^(json|csv|xlsx)$")


# --------------------------------------------------------------------- CRUD
@router.get("")
async def list_reports(
    request: Request,
    session: DbSession,
    user: User = Depends(require_permission("reports.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    include_archived: bool = Query(default=False),
):
    rows, total = await service.list(
        session, user=user, permissions=_permissions(request),
        page=page, page_size=page_size, include_archived=include_archived,
    )
    return {
        "success": True,
        "data": _page_envelope([r.to_public_dict() for r in rows], total, page, page_size),
    }


@router.post("", status_code=201)
async def create_report(
    request: Request,
    session: DbSession,
    body: ReportCreate,
    audit: AuditDep,
    user: User = Depends(require_permission("reports.create")),
):
    # config carries the authoritative domain; keep the body field consistent
    body.config["domain"] = body.config.get("domain", body.domain)
    try:
        report = await service.create(
            session, name=body.name, description=body.description,
            config=body.config, visibility=body.visibility, owner=user,
            timezone=body.timezone,
        )
    except ReportConfigError as exc:
        raise ValidationError(str(exc)) from exc
    await audit.log(
        session, action="reports.report_created", actor_user_id=user.id,
        resource_type="report", resource_id=str(report.id),
        metadata={"name": report.name, "domain": report.domain,
                  "visibility": report.visibility},
    )
    return {"success": True, "data": report.to_public_dict()}


@router.get("/{report_id}")
async def get_report(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    user: User = Depends(require_permission("reports.view")),
):
    report = await service.get_visible(session, report_id, user=user,
                                       permissions=_permissions(request))
    data = report.to_public_dict()
    runs, runs_total = await service.list_runs(session, report, page=1, page_size=5)
    data["recent_runs"] = [run.to_public_dict() for run in runs]
    return {"success": True, "data": data}


@router.put("/{report_id}")
async def update_report(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    body: ReportUpdate,
    audit: AuditDep,
    user: User = Depends(require_permission("reports.edit")),
):
    report = await service.get_manageable(session, report_id, user=user,
                                          permissions=_permissions(request))
    previous_version = report.config_version
    try:
        report = await service.update(
            session, report, name=body.name, description=body.description,
            config=body.config, visibility=body.visibility, timezone=body.timezone,
        )
    except ReportConfigError as exc:
        raise ValidationError(str(exc)) from exc
    await audit.log(
        session, action="reports.report_edited", actor_user_id=user.id,
        resource_type="report", resource_id=str(report.id),
        metadata={"config_version": report.config_version,
                  "previous_config_version": previous_version},
    )
    return {"success": True, "data": report.to_public_dict()}


@router.delete("/{report_id}")
async def delete_report(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("reports.delete")),
):
    report = await service.get_manageable(session, report_id, user=user,
                                          permissions=_permissions(request))
    data = report.to_public_dict()
    await service.delete(session, report)
    await audit.log(
        session, action="reports.report_deleted", actor_user_id=user.id,
        resource_type="report", resource_id=str(data["id"]),
        metadata={"name": data["name"]},
    )
    return {"success": True, "data": {"deleted": True}}


@router.post("/{report_id}/duplicate", status_code=201)
async def duplicate_report(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("reports.create")),
):
    report = await service.get_visible(session, report_id, user=user,
                                       permissions=_permissions(request))
    copy = await service.duplicate(session, report, user=user)
    await audit.log(
        session, action="reports.report_duplicated", actor_user_id=user.id,
        resource_type="report", resource_id=str(copy.id),
        metadata={"source_report_id": str(report.id)},
    )
    return {"success": True, "data": copy.to_public_dict()}


@router.post("/{report_id}/archive")
async def archive_report(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("reports.edit")),
):
    report = await service.get_manageable(session, report_id, user=user,
                                          permissions=_permissions(request))
    report = await service.archive(session, report)
    await audit.log(
        session, action="reports.report_archived", actor_user_id=user.id,
        resource_type="report", resource_id=str(report.id),
    )
    return {"success": True, "data": report.to_public_dict()}


@router.post("/{report_id}/restore")
async def restore_report(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("reports.edit")),
):
    report = await service.get_manageable(session, report_id, user=user,
                                          permissions=_permissions(request))
    report = await service.restore(session, report)
    await audit.log(
        session, action="reports.report_restored", actor_user_id=user.id,
        resource_type="report", resource_id=str(report.id),
    )
    return {"success": True, "data": report.to_public_dict()}


# --------------------------------------------------------------------- runs
@router.post("/{report_id}/run", status_code=202)
async def run_report(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    body: ReportRunIn,
    audit: AuditDep,
    user: User = Depends(require_permission("reports.run")),
):
    report = await service.get_visible(session, report_id, user=user,
                                       permissions=_permissions(request))
    run = await service.queue_run(session, report, user=user, format_name=body.format)
    await audit.log(
        session, action="reports.report_executed", actor_user_id=user.id,
        resource_type="report_run", resource_id=str(run.id),
        metadata={"report_id": str(report.id), "format": run.format},
    )
    return {"success": True, "data": run.to_public_dict()}


@router.get("/{report_id}/runs")
async def list_report_runs(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    user: User = Depends(require_permission("reports.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    report = await service.get_visible(session, report_id, user=user,
                                       permissions=_permissions(request))
    runs, total = await service.list_runs(session, report, page=page, page_size=page_size)
    return {
        "success": True,
        "data": _page_envelope([r.to_public_dict() for r in runs], total, page, page_size),
    }


@router.get("/{report_id}/runs/{run_id}")
async def get_report_run(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    run_id: uuid.UUID,
    user: User = Depends(require_permission("reports.view")),
):
    report = await service.get_visible(session, report_id, user=user,
                                       permissions=_permissions(request))
    run = await session.get(ReportRun, run_id)
    if run is None or run.report_id != report.id:
        raise NotFoundError("Run not found")
    snapshot = await service.snapshot_for_run(session, report, run_id)
    data = run.to_public_dict()
    data["snapshot"] = snapshot.to_public_dict(include_data=True) if snapshot else None
    return {"success": True, "data": data}


@router.get("/{report_id}/export")
async def export_report(
    request: Request,
    session: DbSession,
    report_id: uuid.UUID,
    audit: AuditDep,
    format: str = Query(default="csv", pattern=r"^(csv|xlsx|json)$"),
    run_id: uuid.UUID | None = Query(default=None),
    user: User = Depends(require_permission("reports.export")),
):
    report = await service.get_visible(session, report_id, user=user,
                                       permissions=_permissions(request))
    if run_id is not None:
        snapshot = await service.snapshot_for_run(session, report, run_id)
    else:
        snapshot = await service.latest_snapshot(session, report)
    if snapshot is None:
        raise NotFoundError("No completed run to export yet — execute the report first")
    record = await ReportExporter(request.app.state.files).export_snapshot(
        session, report, snapshot, format_name=format, requested_by=user.id,
    )
    await audit.log(
        session, action="reports.report_exported", actor_user_id=user.id,
        resource_type="report", resource_id=str(report.id),
        metadata={"format": format, "snapshot_id": str(snapshot.id),
                  "file_id": str(record.id)},
    )
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
