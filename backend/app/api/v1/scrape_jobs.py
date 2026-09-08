"""Scrape job endpoints (brief §35–§37, §51).

GET    /api/v1/scrape-jobs                  list + filters      — scraping.view
GET    /api/v1/scrape-jobs/{id}             detail              — scraping.view
POST   /api/v1/scrape-jobs/{id}/pause       cooperative pause   — scraping.pause
POST   /api/v1/scrape-jobs/{id}/resume      resume from cp      — scraping.pause
POST   /api/v1/scrape-jobs/{id}/cancel      cooperative cancel  — scraping.cancel
POST   /api/v1/scrape-jobs/{id}/retry       requeue failed      — scraping.run
GET    /api/v1/scrape-jobs/{id}/logs        event stream        — scraping.view
GET    /api/v1/scrape-jobs/{id}/results     paged leads         — scraping.view
GET    /api/v1/scrape-jobs/{id}/results/export  CSV/XLSX/JSON   — scraping.export
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import DbSession, get_client_ip, get_user_agent, require_permission
from app.core.errors import NotFoundError, ValidationError
from app.models.scrape import JobStatus, ScrapeJobEvent
from app.models.user import User
from app.schemas.common import PageMeta
from app.schemas.scraping import (
    LeadListOut,
    LeadOut,
    ScrapeJobActionOut,
    ScrapeJobEventsOut,
    ScrapeJobEventOut,
    ScrapeJobListOut,
    ScrapeJobOut,
)
from app.services.audit import AuditService
from app.services.export import ExportService
from app.services.leads import LeadService
from app.services.scraping.engine import JobEngine

router = APIRouter(prefix="/scrape-jobs", tags=["scrape-jobs"])


async def _ctx_for(session, user):
    """Resolve (or reuse) the Phase 11 member context for this request."""
    from app.services import authorization as authz
    from app.services import rbac as rbac_service

    perms = await rbac_service.load_user_permissions(session, user.id)
    return await authz.resolve_context(session, user, perms)


async def _visible_job(session, job_id: uuid.UUID, user):
    """IDOR-safe job fetch: organization + visibility scope, 404 on foreign."""
    from app.services import authorization as authz
    from app.models.scrape import ScrapeJob

    ctx = await _ctx_for(session, user)
    return await authz.get_visible_or_404(session, ScrapeJob, job_id, ctx)


def _engine(request: Request, session) -> JobEngine:
    return JobEngine(session, request.app.state.queue)


async def _job_or_404(engine: JobEngine, job_id: uuid.UUID):
    try:
        return await engine.get_job(job_id)
    except ValueError:
        raise NotFoundError("Scrape job not found")


@router.get("")
async def list_jobs(
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    status: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> ScrapeJobListOut:
    engine = _engine(request, session)
    jobs, total = await engine.list_jobs(
        status=status,
        actor_id=actor_id,
        search=search,
        page=page,
        page_size=page_size,
    )
    # Phase 11 §9: organization + visibility scope, enforced backend-side
    from app.services import authorization as authz

    ctx = await _ctx_for(session, _)

    def _in_tenant(job) -> bool:
        org = getattr(job, "organization_id", None)
        if org is not None and org != ctx.organization_id:
            return False
        return authz._passes_scope(job, ctx)

    visible = [j for j in jobs if _in_tenant(j)]
    return ScrapeJobListOut(
        data=[ScrapeJobOut(**job.to_public_dict()) for job in visible],
        meta=PageMeta(page=page, page_size=page_size, total=total).model_dump(),
    )


@router.get("/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.view"))],
) -> ScrapeJobActionOut:
    job = await _visible_job(session, job_id, user)
    return ScrapeJobActionOut(data=ScrapeJobOut(**job.to_public_dict()))


async def _control_action(
    request: Request,
    session: DbSession,
    job_id: uuid.UUID,
    user: User,
    action: str,
    permission: str,
):
    job = await _visible_job(session, job_id, user)
    engine = _engine(request, session)
    method = getattr(engine, action)
    updated = await method(job)
    audit: AuditService = request.app.state.audit
    await audit.log(
        session,
        action=f"scrape_job.{action}",
        actor_user_id=user.id,
        resource_type="scrape_job",
        resource_id=str(job_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return ScrapeJobActionOut(data=ScrapeJobOut(**updated.to_public_dict()))


@router.post("/{job_id}/pause")
async def pause_job(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.pause"))],
) -> ScrapeJobActionOut:
    return await _control_action(request, session, job_id, user, "pause", "scraping.pause")


@router.post("/{job_id}/resume")
async def resume_job(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.pause"))],
) -> ScrapeJobActionOut:
    return await _control_action(request, session, job_id, user, "resume", "scraping.pause")


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.cancel"))],
) -> ScrapeJobActionOut:
    return await _control_action(request, session, job_id, user, "cancel", "scraping.cancel")


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> ScrapeJobActionOut:
    return await _control_action(request, session, job_id, user, "retry_job", "scraping.run")


@router.get("/{job_id}/logs")
async def job_logs(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    event_type: str | None = Query(default=None, max_length=50),
    after: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> ScrapeJobEventsOut:
    from datetime import datetime

    job = await _visible_job(session, job_id, _)
    query = (
        ScrapeJobEvent.__table__.select()
        .where(ScrapeJobEvent.job_id == job_id)
        .order_by(ScrapeJobEvent.created_at.asc())
        .limit(limit)
    )
    if event_type:
        query = query.where(ScrapeJobEvent.event_type == event_type)
    if after:
        try:
            after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError("Invalid 'after' timestamp")
        query = query.where(ScrapeJobEvent.created_at > after_dt)
    rows = (await session.execute(query)).mappings().all()
    events = [
        ScrapeJobEventOut(
            id=str(row["id"]),
            event_type=row["event_type"],
            message=row["message"],
            metadata=row["metadata_json"] or {},
            created_at=row["created_at"].isoformat() if row["created_at"] else "",
        )
        for row in rows
    ]
    return ScrapeJobEventsOut(
        data=events,
        meta={"count": len(events), "job_id": str(job_id)},
    )


@router.get("/{job_id}/results")
async def job_results(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> LeadListOut:
    job = await _visible_job(session, job_id, _)
    leads_service = LeadService()
    leads, total = await leads_service.list_for_job(
        session, job.id, page=page, page_size=page_size
    )
    return LeadListOut(
        data=[LeadOut(**lead.to_public_dict()) for lead in leads],
        meta=PageMeta(page=page, page_size=page_size, total=total).model_dump(),
    )


@router.get("/{job_id}/results/export")
async def export_results(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.export"))],
    format: str = Query(default="csv", pattern=r"^(csv|xlsx|json)$"),
):
    """Export handoff to the Phase 2 ExportService (brief §37 — no duplicate
    export implementation). Renders the job's leads and stores the file in
    the EXPORT category; clients download via the Phase 2 files API."""
    job = await _visible_job(session, job_id, user)
    if job.status not in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        raise ValidationError("Job has not finished; results are not final yet")

    leads_service = LeadService()
    rows: list[dict] = []
    page = 1
    while True:
        batch, _total = await leads_service.list_for_job(
            session, job.id, page=page, page_size=500
        )
        if not batch:
            break
        for lead in batch:
            data = lead.to_public_dict()
            rows.append(
                {
                    "business_name": data.get("business_name"),
                    "contact_name": data.get("contact_name"),
                    "email": data.get("email"),
                    "phone": data.get("phone"),
                    "website": data.get("website"),
                    "address": data.get("address"),
                    "city": data.get("city"),
                    "state": data.get("state"),
                    "country": data.get("country"),
                    "category": data.get("category"),
                    "rating": data.get("rating"),
                    "review_count": data.get("review_count"),
                    "source": data.get("source"),
                    "source_url": data.get("source_url"),
                    "scraped_at": data.get("scraped_at"),
                }
            )
        page += 1

    files_service = request.app.state.files
    exporter = ExportService(files_service)
    _ctx11 = await _ctx_for(session, user)
    record = await exporter.export(
        session,
        format_name=format,
        rows=rows,
        base_name=f"scrape-job-{str(job.id)[:8]}-results",
        created_by=user.id,
        organization_id=_ctx11.organization_id,
        metadata={"job_id": str(job.id), "actor_id": job.actor_id},
    )

    audit: AuditService = request.app.state.audit
    await audit.log(
        session,
        action="scrape_job.results_exported",
        actor_user_id=user.id,
        resource_type="scrape_job",
        resource_id=str(job.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"format": format},
    )
    return {
        "success": True,
        "data": {
            "file_id": str(record.id),
            "filename": record.name,
            "size": record.size,
            "format": format,
            "download": f"/api/v1/files/{record.id}/download",
        },
    }
