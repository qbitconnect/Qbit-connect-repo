"""Scraper registry endpoints (brief §51).

GET  /api/v1/scrapers               list (cards)          — scraping.view
GET  /api/v1/scrapers/health        refresh + health map  — scraping.view
GET  /api/v1/scrapers/{id}          detail                — scraping.view
POST /api/v1/scrapers/{id}/validate strict input check    — scraping.view
POST /api/v1/scrapers/{id}/jobs     create + enqueue job  — scraping.run
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import DbSession, get_client_ip, get_user_agent, require_permission
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.user import User
from app.schemas.scraping import (
    ActorActionOut,
    ActorListOut,
    ActorOut,
    CreateJobRequest,
    ScrapeJobActionOut,
    ValidateJobRequest,
    ValidationReportOut,
)
from app.scrapers.core.base import ScraperActor
from app.services.audit import AuditService
from app.services.scraping.engine import JobEngine, sanitize_job_config

router = APIRouter(prefix="/scrapers", tags=["scrapers"])


def _registry(request: Request):
    registry = getattr(request.app.state, "scraper_registry", None)
    if registry is None:  # pragma: no cover — create_app always wires it
        raise NotFoundError("Scraper registry unavailable")
    return registry


def _actor_out(registry, actor_id: str) -> ActorOut:
    try:
        entry = registry.entry(actor_id)
        actor = registry.get(actor_id)
    except KeyError:
        raise NotFoundError(f"Scraper not found: {actor_id}")
    meta = actor.metadata()
    return ActorOut(
        id=meta["id"],
        name=meta["name"],
        slug=meta["slug"],
        version=meta["version"],
        description=meta["description"],
        category=meta["category"],
        author=meta["author"],
        capabilities=meta["capabilities"],
        supports_pause=meta["supports_pause"],
        status=entry.public_status.value if entry else "REGISTERED",
        status_detail=entry.detail if entry else None,
        dependencies=entry.dependencies if entry else {},
        input_schema=meta["input_schema"],
        output_fields=meta["output_fields"],
    )


def _get_runnable_actor(registry, actor_id: str) -> ScraperActor:
    entry = registry.entry(actor_id)
    if entry is None:
        raise NotFoundError(f"Scraper not found: {actor_id}")
    if not entry.enabled:
        raise ConflictError(f"Scraper {actor_id} is disabled by configuration")
    return entry.actor


@router.get("")
async def list_scrapers(
    request: Request,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
) -> ActorListOut:
    registry = _registry(request)
    actors = [_actor_out(registry, actor_id) for actor_id in registry.discover()]
    return ActorListOut(data=actors, meta={"total": len(actors)})


@router.get("/health")
async def scrapers_health(
    request: Request,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
):
    registry = _registry(request)
    report = await registry.health_check()
    return {
        "success": True,
        "data": {
            actor_id: {
                "status": health.status.value,
                "detail": health.detail,
                "dependencies": health.dependencies,
            }
            for actor_id, health in report.items()
        },
        "meta": registry.summary(),
    }


@router.get("/{actor_id}")
async def get_scraper(
    actor_id: str,
    request: Request,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
) -> ActorActionOut:
    return ActorActionOut(data=_actor_out(_registry(request), actor_id))


@router.post("/{actor_id}/validate")
async def validate_input(
    actor_id: str,
    body: ValidateJobRequest,
    request: Request,
    _: Annotated[User, Depends(require_permission("scraping.view"))],
) -> ValidationReportOut:
    actor = _get_runnable_actor(_registry(request), actor_id)
    report = actor.validate_input(body.input or {})
    config_errors: dict[str, str] = {}
    try:
        sanitize_job_config(body.config)
    except ValidationError as exc:
        config_errors["config"] = exc.message
    valid = report.valid and not config_errors
    return ValidationReportOut(
        data={
            "valid": valid,
            "errors": {**report.errors, **config_errors},
            "normalized_input": report.normalized_input,
        }
    )


@router.post("/{actor_id}/jobs")
async def create_job(
    actor_id: str,
    body: CreateJobRequest,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> ScrapeJobActionOut:
    registry = _registry(request)
    actor = _get_runnable_actor(registry, actor_id)

    report = actor.validate_input(body.input or {})
    if not report.valid:
        raise ValidationError("Invalid scraper input", details={"fields": report.errors})
    config = sanitize_job_config(body.config)

    queue = request.app.state.queue
    engine = JobEngine(session, queue)
    job = await engine.create_job(
        actor=actor,
        validated_input=report.normalized_input,
        config=config,
        created_by=user.id,
        max_attempts=body.max_attempts or 3,
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
        metadata={"actor": actor_id, "version": actor.version},
    )
    return ScrapeJobActionOut(data=job.to_public_dict())
