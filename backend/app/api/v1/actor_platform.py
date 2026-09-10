"""QBIT ACTOR PLATFORM — public API surface (spec §23).

Actor-centric REST API on top of the job engine:

  GET    /actors                        catalog (metadata + stats + health)
  GET    /actors/{slug}                 detail (input schema, examples, stats)
  POST   /actors/{slug}/validate        strict input check
  POST   /actors/{slug}/runs            create + enqueue a run
  GET    /actors/{slug}/runs            run history for one actor
  GET    /runs                          run list (filters)
  GET    /runs/{run_id}                 run detail
  POST   /runs/{run_id}/pause|resume|cancel|retry
  GET    /runs/{run_id}/logs            run logs (event filter)
  GET    /runs/{run_id}/dataset         the run's dataset
  GET    /datasets                      dataset list
  GET    /datasets/{id}                 dataset detail
  GET    /datasets/{id}/items           search / filter / sort / paginate
  POST   /datasets/{id}/export          export ALL | SELECTED | FILTERED
  GET    /datasets/{id}/changes         snapshot change detection (§7.B)
  GET    /tasks  POST /tasks  GET|PATCH|DELETE /tasks/{id}
  POST   /tasks/{id}/run   POST /tasks/{id}/duplicate
  GET|POST /run-webhooks  PATCH|DELETE /run-webhooks/{id}
  GET    /run-webhooks/{id}/deliveries   POST /run-webhooks/{id}/test
  GET    /storage/kv   PUT|GET|DELETE /storage/kv/{scope}/{key}   (§14)
  GET    /storage/queues  GET /storage/queues/{name}            (§13)

Permissions reuse the scraping.* capability set (scraping.view / run /
pause / cancel / export / manage) — no parallel permission universe.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession, get_client_ip, get_user_agent, require_permission
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import log_with
from app.models.actor_platform import (
    ActorDataset,
    ActorTask,
    RunWebhook,
)
from app.models.user import User
from app.schemas.scraping import ScrapeJobActionOut, ScrapeJobOut, ValidationReportOut
from app.services.audit import AuditService
from app.services.scraping.change_detection import compare_snapshots
from app.services.scraping.datasets import DatasetService
from app.services.scraping.engine import JobEngine, sanitize_job_config
from app.services.scraping.health_monitor import ActorStats, HealthMonitor
from app.services.scraping.storage_services import KVStore, RequestQueue
from app.services.scraping.run_webhooks import RUN_EVENTS, RunWebhookService

router = APIRouter()


def _registry(request: Request):
    registry = getattr(request.app.state, "scraper_registry", None)
    if registry is None:  # pragma: no cover — create_app always wires it
        raise NotFoundError("Scraper registry unavailable")
    return registry


def _get_actor(request: Request, slug: str):
    entry = _registry(request).entry(slug)
    if entry is None:
        raise NotFoundError(f"Actor not found: {slug}")
    return entry


# ------------------------------------------------------------------ metadata
def _actor_detail(request: Request, slug: str) -> dict[str, Any]:
    entry = _get_actor(request, slug)
    actor = entry.actor
    meta = actor.metadata()
    return {
        **meta,
        "status": entry.public_status.value,
        "status_detail": entry.detail,
        "enabled": entry.enabled,
        "supported_urls": list(getattr(actor, "supported_urls", ()) or ()),
        "examples": list(getattr(actor, "examples", ()) or ()),
        "modes": list(getattr(actor, "modes", ()) or ()),
    }


@router.get("/actors")
async def list_actors(
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    category: str | None = Query(default=None, max_length=40),
    q: str | None = Query(default=None, max_length=200),
):
    registry = _registry(request)
    stats = ActorStats(session)
    items = []
    for slug in registry.discover():
        entry = registry.entry(slug)
        detail = _actor_detail(request, slug)
        if category and detail["category"] != category:
            continue
        if q:
            haystack = f"{detail['name']} {detail['description']} {' '.join(detail['capabilities'])}".lower()
            if q.lower() not in haystack and q.lower() != slug:
                continue
        actor_stats = await stats.per_actor(slug)
        monitor = HealthMonitor(session)
        latest_health = await monitor.latest(slug)
        detail["stats"] = actor_stats
        detail["health"] = latest_health.to_public_dict() if latest_health else None
        items.append(detail)
    return {"success": True, "data": items, "meta": {"total": len(items)}}


@router.get("/actors/{slug}")
async def get_actor(
    slug: str,
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    detail = _actor_detail(request, slug)
    stats = ActorStats(session)
    detail["stats"] = await stats.per_actor(slug)
    detail["health_history"] = [
        row.to_public_dict() for row in await HealthMonitor(session).history(slug, limit=10)
    ]
    return {"success": True, "data": detail}


@router.post("/actors/{slug}/validate")
async def validate_actor_input(
    slug: str,
    body: dict,
    request: Request,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
) -> ValidationReportOut:
    entry = _get_actor(request, slug)
    if not entry.enabled:
        raise ConflictError(f"Actor {slug} is disabled by configuration")
    report = entry.actor.validate_input(body.get("input") or {})
    config_errors: dict[str, str] = {}
    try:
        sanitize_job_config(body.get("config") or {})
    except ValidationError as exc:
        config_errors["config"] = exc.message
    return ValidationReportOut(
        data={
            "valid": report.valid and not config_errors,
            "errors": {**report.errors, **config_errors},
            "normalized_input": report.normalized_input,
        }
    )


async def _enqueue_run(
    request: Request,
    session: AsyncSession,
    *,
    slug: str,
    input_data: dict,
    config: dict | None,
    user: User,
    name: str | None = None,
    trigger: str = "API",
    task_id: uuid.UUID | None = None,
    max_attempts: int = 3,
    config_from_task: dict | None = None,
):
    entry = _get_actor(request, slug)
    if not entry.enabled:
        raise ConflictError(f"Actor {slug} is disabled by configuration")
    report = entry.actor.validate_input(input_data)
    if not report.valid:
        raise ValidationError("Invalid actor input", details={"fields": report.errors})
    clean_config = sanitize_job_config(config or config_from_task or {})
    engine = JobEngine(session, request.app.state.queue)
    job = await engine.create_job(
        actor=entry.actor,
        validated_input=report.normalized_input,
        config=clean_config,
        created_by=user.id,
        max_attempts=max_attempts,
        name=name,
        trigger=trigger,
        task_id=task_id,
    )
    audit: AuditService = request.app.state.audit
    await audit.log(
        session,
        action="scrape_job.created",
        actor_user_id=user.id,
        resource_type="scrape_job",
        resource_id=str(job.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"actor": slug, "trigger": trigger},
    )
    return job


@router.post("/actors/{slug}/runs")
async def create_actor_run(
    slug: str,
    body: dict,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> ScrapeJobActionOut:
    job = await _enqueue_run(
        request, session, slug=slug,
        input_data=body.get("input") or {},
        config=body.get("config"),
        user=user,
        name=body.get("name"),
        trigger="API",
        max_attempts=body.get("max_attempts") or 3,
    )
    return ScrapeJobActionOut(data=ScrapeJobOut(**job.to_public_dict()))


@router.get("/actors/{slug}/runs")
async def list_actor_runs(
    slug: str,
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    status: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    _get_actor(request, slug)  # 404 for unknown actors
    from app.models.scrape import JobStatus, ScrapeJob

    stmt = select(ScrapeJob).where(ScrapeJob.actor_id == slug)
    count = select(func.count(ScrapeJob.id)).where(ScrapeJob.actor_id == slug)
    if status:
        try:
            stmt = stmt.where(ScrapeJob.status == JobStatus(status.upper()).value)
            count = count.where(ScrapeJob.status == JobStatus(status.upper()).value)
        except ValueError as exc:
            raise ValidationError(f"Unknown status filter: {status}") from exc
    total = (await session.execute(count)).scalar_one()
    rows = (await session.execute(stmt.order_by(ScrapeJob.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {
        "success": True,
        "data": [ScrapeJobOut(**j.to_public_dict()) for j in rows],
        "meta": {"total": int(total), "actor_id": slug},
    }


# ------------------------------------------------------------------ runs
@router.get("/runs")
async def list_runs(
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    actor_id: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, max_length=20),
    trigger: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    from app.models.scrape import JobStatus, ScrapeJob

    member = getattr(request.state, "member_context", None)
    stmt = select(ScrapeJob)
    count = select(func.count(ScrapeJob.id))
    conds = []
    if member is not None and member.organization_id is not None:
        conds.append(or_(ScrapeJob.organization_id.is_(None), ScrapeJob.organization_id == member.organization_id))
    if actor_id:
        conds.append(ScrapeJob.actor_id == actor_id)
    if status:
        try:
            conds.append(ScrapeJob.status == JobStatus(status.upper()).value)
        except ValueError as exc:
            raise ValidationError(f"Unknown status filter: {status}") from exc
    if trigger:
        conds.append(ScrapeJob.trigger == trigger.upper())
    if conds:
        stmt = stmt.where(*conds)
        count = count.where(*conds)
    total = (await session.execute(count)).scalar_one()
    rows = (await session.execute(stmt.order_by(ScrapeJob.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {
        "success": True,
        "data": [ScrapeJobOut(**j.to_public_dict()) for j in rows],
        "meta": {"total": int(total)},
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    engine = JobEngine(session, request.app.state.queue)
    job = await engine.get_job(run_id)  # raises 404 when missing
    dataset = await DatasetService(session).for_job(run_id)
    data = job.to_public_dict()
    data["dataset_id"] = str(dataset.id) if dataset else None
    return {"success": True, "data": data}


@router.post("/runs/{run_id}/{action}")
async def run_control(
    run_id: uuid.UUID,
    action: str,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.view"))],
) -> ScrapeJobActionOut:
    """pause / resume / cancel / retry — permission-mapped (spec §23)."""
    permission_for = {
        "pause": "scraping.pause",
        "resume": "scraping.pause",
        "cancel": "scraping.cancel",
        "retry": "scraping.run",
    }
    if action not in permission_for:
        raise NotFoundError(f"Unknown run action: {action}")
    # The route-level dep guarantees auth; the per-action code is enforced
    # here against the cached permission set (spec §32 — every endpoint
    # verifies the exact capability it exercises).
    permissions = getattr(request.state, "permissions", None) or set()
    needed = permission_for[action]
    if needed not in permissions:
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError(f"Missing required permission: {needed}")
    engine = JobEngine(session, request.app.state.queue)
    job = await engine.get_job(run_id)  # raises 404 when missing
    method = getattr(engine, "retry_job" if action == "retry" else action)
    updated = (await method(job, actor_id=job.actor_id) if action == "retry" else await method(job))
    audit: AuditService = request.app.state.audit
    await audit.log(
        session,
        action=f"scrape_job.{action}",
        actor_user_id=user.id,
        resource_type="scrape_job",
        resource_id=str(run_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return ScrapeJobActionOut(data=ScrapeJobOut(**updated.to_public_dict()))


@router.get("/runs/{run_id}/logs")
async def run_logs(
    run_id: uuid.UUID,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    event_type: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=200, ge=1, le=1000),
):
    from app.models.scrape import ScrapeJobEvent

    stmt = (
        select(ScrapeJobEvent)
        .where(ScrapeJobEvent.job_id == run_id)
        .order_by(ScrapeJobEvent.created_at.asc())
        .limit(limit)
    )
    if event_type:
        stmt = stmt.where(ScrapeJobEvent.event_type == event_type)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "success": True,
        "data": [row.to_public_dict() if hasattr(row, "to_public_dict") else {
            "id": str(row.id), "event_type": row.event_type,
            "message": row.message, "metadata": row.metadata_json or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        } for row in rows],
        "meta": {"count": len(rows)},
    }


@router.get("/runs/{run_id}/dataset")
async def run_dataset(
    run_id: uuid.UUID,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    dataset = await DatasetService(session).for_job(run_id)
    if dataset is None:
        raise NotFoundError(f"No dataset for run {run_id}")
    return {"success": True, "data": dataset.to_public_dict()}


# ------------------------------------------------------------------ datasets
@router.get("/datasets")
async def list_datasets(
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    actor_id: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, max_length=12),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    svc = DatasetService(session)
    datasets, total = await svc.list(
        actor_id=actor_id, status=status, limit=limit, offset=offset
    )
    return {
        "success": True,
        "data": [d.to_public_dict() for d in datasets],
        "meta": {"total": total},
    }


async def _dataset_or_404(session: AsyncSession, dataset_id: uuid.UUID) -> ActorDataset:
    dataset = await session.get(ActorDataset, dataset_id)
    if dataset is None:
        raise NotFoundError(f"Dataset not found: {dataset_id}")
    return dataset


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: uuid.UUID,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    from app.models.scrape import ScrapeJob

    dataset = await _dataset_or_404(session, dataset_id)
    data = dataset.to_public_dict()
    if dataset.job_id:
        job = await session.get(ScrapeJob, dataset.job_id)
        data["run"] = job.to_public_dict() if job else None
    return {"success": True, "data": data}


@router.get("/datasets/{dataset_id}/items")
async def dataset_items(
    dataset_id: uuid.UUID,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    q: str | None = Query(default=None, max_length=200),
    field: str | None = Query(default=None, max_length=100),
    value: str | None = Query(default=None, max_length=200),
    sort: str | None = Query(default=None, max_length=100),
    order: str = Query(default="asc", max_length=4),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    await _dataset_or_404(session, dataset_id)
    svc = DatasetService(session)
    items, total = await svc.items_page(
        dataset_id, search=q, field=field, value=value,
        sort_field=sort, sort_dir="desc" if order == "desc" else "asc",
        offset=offset, limit=limit,
    )
    return {
        "success": True,
        "data": [{"id": str(item.id), "idx": item.idx, "data": item.data} for item in items],
        "meta": {"total": total, "offset": offset, "limit": limit},
    }


@router.post("/datasets/{dataset_id}/export")
async def export_dataset(
    dataset_id: uuid.UUID,
    body: dict,
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.export"))],
):
    dataset = await _dataset_or_404(session, dataset_id)
    svc = DatasetService(session)
    fmt = (body.get("format") or "json").lower()
    try:
        filename, content = await svc.export(
            dataset, fmt,
            ids=body.get("ids"),
            search=body.get("search"),
            field=body.get("field"),
            value=body.get("value"),
        )
    except ValueError as exc:
        raise ValidationError(str(exc))
    media = {
        "json": "application/json", "jsonl": "application/x-ndjson",
        "csv": "text/csv", "xml": "application/xml",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    audit: AuditService = request.app.state.audit
    await audit.log(
        session, action="dataset.exported", actor_user_id=None,
        resource_type="actor_dataset", resource_id=str(dataset_id),
        ip_address=get_client_ip(request), user_agent=get_user_agent(request),
        metadata={"format": fmt},
    )
    return Response(
        content=content,
        media_type=media.get(fmt, "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/datasets/{dataset_id}/changes")
async def dataset_changes(
    dataset_id: uuid.UUID,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    against: uuid.UUID | None = Query(default=None),
    key_field: str = Query(default="change_key", max_length=100),
):
    """Ad-change detection (spec §7.B): current dataset vs a previous snapshot.
    `against` defaults to the actor's immediately-preceding READY dataset."""
    current = await _dataset_or_404(session, dataset_id)
    svc = DatasetService(session)
    previous_id = against
    if previous_id is None:
        datasets, _total = await svc.list(actor_id=current.actor_id, status="READY", limit=20)
        earlier = [d for d in datasets if d.id != current.id and d.created_at < current.created_at]
        if not earlier:
            return {"success": True, "data": {"changes": [], "note": "no earlier snapshot to compare"}}
        earlier.sort(key=lambda d: d.created_at, reverse=True)
        previous_id = earlier[0].id
    previous = await _dataset_or_404(session, previous_id)
    changes = await compare_snapshots(session, previous.id, current.id, key_field=key_field)
    return {
        "success": True,
        "data": {
            "changes": changes,
            "previous_dataset_id": str(previous_id),
            "current_dataset_id": str(current.id),
            "key_field": key_field,
        },
    }


# ------------------------------------------------------------------ tasks
@router.get("/tasks")
async def list_tasks(
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.view"))],
    actor_id: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(ActorTask).order_by(ActorTask.updated_at.desc())
    count = select(func.count(ActorTask.id))
    if actor_id:
        stmt = stmt.where(ActorTask.actor_id == actor_id)
        count = count.where(ActorTask.actor_id == actor_id)
    total = (await session.execute(count)).scalar_one()
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"success": True, "data": [t.to_public_dict() for t in rows], "meta": {"total": int(total)}}


@router.post("/tasks")
async def create_task(
    body: dict,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
):
    actor_id = body.get("actor_id")
    if not actor_id or _registry(request).entry(actor_id) is None:
        raise ValidationError("actor_id must reference a registered actor")
    name = (body.get("name") or "").strip()
    if not name:
        raise ValidationError("name is required")
    input_data = body.get("input") or {}
    entry = _registry(request).entry(actor_id)
    report = entry.actor.validate_input(input_data)
    if not report.valid:
        raise ValidationError("Invalid task input", details={"fields": report.errors})
    task = ActorTask(
        actor_id=actor_id,
        name=name[:200],
        description=(body.get("description") or "")[:2000] or None,
        input=report.normalized_input,
        config=sanitize_job_config(body.get("config") or {}),
        created_by=user.id,
    )
    session.add(task)
    await session.commit()
    audit: AuditService = request.app.state.audit
    await audit.log(session, action="actor_task.created", actor_user_id=user.id,
                    resource_type="actor_task", resource_id=str(task.id),
                    ip_address=get_client_ip(request), user_agent=get_user_agent(request),
                    metadata={"actor": actor_id})
    return {"success": True, "data": task.to_public_dict()}


async def _task_or_404(session: AsyncSession, task_id: uuid.UUID) -> ActorTask:
    task = await session.get(ActorTask, task_id)
    if task is None:
        raise NotFoundError(f"Task not found: {task_id}")
    return task


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    return {"success": True, "data": (await _task_or_404(session, task_id)).to_public_dict()}


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: uuid.UUID,
    body: dict,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
):
    task = await _task_or_404(session, task_id)
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name:
            raise ValidationError("name cannot be empty")
        task.name = name[:200]
    if "description" in body:
        task.description = (body.get("description") or "")[:2000] or None
    if "input" in body:
        entry = _registry(request).entry(task.actor_id)
        report = entry.actor.validate_input(body.get("input") or {})
        if not report.valid:
            raise ValidationError("Invalid task input", details={"fields": report.errors})
        task.input = report.normalized_input
    if "config" in body:
        task.config = sanitize_job_config(body.get("config") or {})
    if "enabled" in body:
        task.enabled = bool(body.get("enabled"))
    from datetime import datetime, timezone

    task.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return {"success": True, "data": task.to_public_dict()}


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
):
    task = await _task_or_404(session, task_id)
    await session.delete(task)
    await session.commit()
    audit: AuditService = request.app.state.audit
    await audit.log(session, action="actor_task.deleted", actor_user_id=user.id,
                    resource_type="actor_task", resource_id=str(task_id),
                    ip_address=get_client_ip(request), user_agent=get_user_agent(request))
    return {"success": True, "data": {"deleted": True}}


@router.post("/tasks/{task_id}/run")
async def run_task(
    task_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> ScrapeJobActionOut:
    """Acceptance TEST 10 (spec §40): a saved Task re-runs with its saved input."""
    task = await _task_or_404(session, task_id)
    if not task.enabled:
        raise ConflictError("Task is disabled")
    job = await _enqueue_run(
        request, session, slug=task.actor_id,
        input_data=task.input or {}, config=task.config or {},
        user=user, name=task.name, trigger="TASK", task_id=task.id,
    )
    task.run_count = (task.run_count or 0) + 1
    from datetime import datetime, timezone

    task.last_run_at = datetime.now(timezone.utc)
    task.last_job_id = job.id
    await session.commit()
    return ScrapeJobActionOut(data=ScrapeJobOut(**job.to_public_dict()))


@router.post("/tasks/{task_id}/duplicate")
async def duplicate_task(
    task_id: uuid.UUID,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
):
    task = await _task_or_404(session, task_id)
    copy = ActorTask(
        actor_id=task.actor_id,
        name=f"{task.name} (copy)"[:200],
        description=task.description,
        input=task.input,
        config=task.config,
        created_by=user.id,
    )
    session.add(copy)
    await session.commit()
    return {"success": True, "data": copy.to_public_dict()}


# ------------------------------------------------------------------ run webhooks
@router.get("/run-webhooks")
async def list_run_webhooks(
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    hooks = await RunWebhookService(session).list()
    return {"success": True, "data": [h.to_public_dict() for h in hooks], "meta": {"total": len(hooks)}}


@router.post("/run-webhooks")
async def create_run_webhook(
    body: dict,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.manage"))],
):
    name = (body.get("name") or "").strip()
    url = (body.get("url") or "").strip()
    if not name or not url:
        raise ValidationError("name and url are required")
    if not url.lower().startswith(("http://", "https://")):
        raise ValidationError("url must be http(s)")
    events = body.get("events") or list(RUN_EVENTS)
    bad = [e for e in events if e not in RUN_EVENTS]
    if bad:
        raise ValidationError(f"Unknown events: {', '.join(bad)}")
    secret = body.get("secret") or (uuid.uuid4().hex + uuid.uuid4().hex)
    hook = RunWebhook(
        name=name[:200], url=url[:1000], secret=secret,
        events=events, actor_id=body.get("actor_id"),
        enabled=True, created_by=user.id,
    )
    session.add(hook)
    await session.commit()
    audit: AuditService = request.app.state.audit
    await audit.log(session, action="run_webhook.created", actor_user_id=user.id,
                    resource_type="run_webhook", resource_id=str(hook.id),
                    ip_address=get_client_ip(request), user_agent=get_user_agent(request))
    data = hook.to_public_dict()
    # one-time secret reveal (write-only storage thereafter, spec §32)
    data["secret_revealed_once"] = body.get("secret") is None
    if body.get("secret") is None:
        data["secret"] = secret
    return {"success": True, "data": data}


async def _hook_or_404(session: AsyncSession, hook_id: uuid.UUID) -> RunWebhook:
    hook = await session.get(RunWebhook, hook_id)
    if hook is None:
        raise NotFoundError(f"Run webhook not found: {hook_id}")
    return hook


@router.patch("/run-webhooks/{hook_id}")
async def update_run_webhook(
    hook_id: uuid.UUID,
    body: dict,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.manage"))],
):
    hook = await _hook_or_404(session, hook_id)
    if "name" in body:
        hook.name = (body.get("name") or hook.name)[:200]
    if "url" in body:
        url = (body.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValidationError("url must be http(s)")
        hook.url = url[:1000]
    if "events" in body:
        events = body.get("events") or []
        bad = [e for e in events if e not in RUN_EVENTS]
        if bad:
            raise ValidationError(f"Unknown events: {', '.join(bad)}")
        hook.events = events
    if "actor_id" in body:
        hook.actor_id = body.get("actor_id") or None
    if "enabled" in body:
        hook.enabled = bool(body.get("enabled"))
    from datetime import datetime, timezone

    hook.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return {"success": True, "data": hook.to_public_dict()}


@router.delete("/run-webhooks/{hook_id}")
async def delete_run_webhook(
    hook_id: uuid.UUID,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.manage"))],
):
    hook = await _hook_or_404(session, hook_id)
    await session.delete(hook)
    await session.commit()
    return {"success": True, "data": {"deleted": True}}


@router.get("/run-webhooks/{hook_id}/deliveries")
async def run_webhook_deliveries(
    hook_id: uuid.UUID,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    limit: int = Query(default=50, ge=1, le=200),
):
    await _hook_or_404(session, hook_id)
    rows = await RunWebhookService(session).deliveries(hook_id, limit=limit)
    return {"success": True, "data": [d.to_public_dict() for d in rows], "meta": {"count": len(rows)}}


@router.post("/run-webhooks/{hook_id}/test")
async def test_run_webhook(
    hook_id: uuid.UUID,
    request: Request,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.manage"))],
):
    """Queue a signed TEST event through the normal delivery path."""
    hook = await _hook_or_404(session, hook_id)
    service = RunWebhookService(session)
    queued = await service.emit(
        event="RUN_SUCCEEDED", actor_id=hook.actor_id,
        job_id=None,
        payload={"test": True, "message": "QBIT test delivery"},
    )
    await session.commit()
    log_with(__import__("logging").getLogger("qbit.api"), 20,
             "Run webhook test queued", webhook=str(hook.id), queued=queued)
    return {"success": True, "data": {"queued": queued, "poll": "worker delivers within seconds"}}


# ------------------------------------------------------------------ storage
@router.get("/storage/kv")
async def list_kv(
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    scope: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    entries, total = await KVStore(session).list(scope=scope, limit=limit, offset=offset)
    return {"success": True, "data": entries, "meta": {"total": total}}


@router.get("/storage/queues")
async def list_queues(
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    rq = RequestQueue(session)
    queues = await rq.list_queues()
    out = [await rq.stats(q["queue_name"]) for q in queues]
    return {"success": True, "data": out, "meta": {"total": len(out)}}


@router.get("/storage/queues/{queue_name}")
async def get_queue(
    queue_name: str,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
    status: str | None = Query(default=None, max_length=12),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rq = RequestQueue(session)
    items, total = await rq.list_items(queue_name, status=status, limit=limit, offset=offset)
    return {
        "success": True,
        "data": [i.to_public_dict() for i in items],
        "meta": {"total": total, "stats": await rq.stats(queue_name)},
    }


@router.put("/storage/kv/{scope}/{key}")
async def put_kv(
    scope: str,
    key: str,
    body: dict,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.manage"))],
):
    entry = await KVStore(session).set(key, body.get("value") or {}, scope=scope, updated_by=user.id)
    await session.commit()
    return {"success": True, "data": entry}


@router.get("/storage/kv/{scope}/{key}")
async def get_kv(
    scope: str,
    key: str,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    entry = await KVStore(session).get(key, scope=scope)
    if entry is None:
        raise NotFoundError(f"Key not found: {scope}/{key}")
    return {"success": True, "data": entry}


@router.delete("/storage/kv/{scope}/{key}")
async def delete_kv(
    scope: str,
    key: str,
    session: DbSession,
    _: Annotated[User, Depends(require_permission("scraping.manage"))],
):
    deleted = await KVStore(session).delete(key, scope=scope)
    await session.commit()
    if not deleted:
        raise NotFoundError(f"Key not found: {scope}/{key}")
    return {"success": True, "data": {"deleted": True}}
