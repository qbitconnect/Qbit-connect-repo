"""Scrape schedule endpoints (spec §SCHEDULING).

POST   /api/v1/scrape-schedules              create                — scraping.run
GET    /api/v1/scrape-schedules              list (actor filter)   — scraping.view
GET    /api/v1/scrape-schedules/{id}         detail                — scraping.view
POST   /api/v1/scrape-schedules/{id}/enable  enable                — scraping.run
POST   /api/v1/scrape-schedules/{id}/disable disable               — scraping.run
POST   /api/v1/scrape-schedules/{id}/run-now enqueue a job now     — scraping.run
DELETE /api/v1/scrape-schedules/{id}         delete                — scraping.run

Schedules inherit the actor's `scraping.run` permission: a schedule is a
stored intention to run an actor, so it must never grant more than the
operator could do directly. Org scope is enforced in SQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import DbSession, require_permission
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.user import User
from app.schemas.common import PageMeta
from app.services.scraping.scheduling import (
    MAX_CONSECUTIVE_FAILURES,
    MIN_INTERVAL_SECONDS,
    ScrapeScheduleService,
)

router = APIRouter(prefix="/scrape-schedules", tags=["scrape-schedules"])


# ------------------------------------------------------------------ schemas


class ScheduleOut(BaseModel):
    id: str
    actor_id: str
    name: str | None
    input: dict
    config: dict
    schedule_type: str
    interval_seconds: int | None
    daily_time: str | None
    timezone: str
    enabled: bool
    next_run_at: str | None
    last_run_at: str | None
    last_job_id: str | None
    run_count: int
    failure_count: int
    max_runs: int | None
    last_error: str | None
    created_at: str | None


class ScheduleActionOut(BaseModel):
    success: bool = True
    data: ScheduleOut


class ScheduleListOut(BaseModel):
    success: bool = True
    data: list[ScheduleOut]
    meta: dict


class ScheduleCreateRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=200)
    input: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)
    schedule_type: str = Field(min_length=3, max_length=10)
    interval_seconds: int | None = Field(default=None, ge=MIN_INTERVAL_SECONDS, le=31_536_000)
    daily_time: str | None = Field(default=None, max_length=5)
    timezone: str = Field(default="UTC", max_length=64)
    max_runs: int | None = Field(default=None, ge=1, le=1_000_000)
    start_at: datetime | None = None


def _out(schedule) -> ScheduleOut:
    return ScheduleOut(**schedule.to_public_dict())


def _service(session) -> ScrapeScheduleService:
    return ScrapeScheduleService(session)


async def _ctx_org(session, user) -> uuid.UUID | None:
    from app.services import authorization as authz
    from app.services import rbac as rbac_service

    perms = await rbac_service.load_user_permissions(session, user.id)
    ctx = await authz.resolve_context(session, user, perms)
    return getattr(ctx, "organization_id", None)


# ------------------------------------------------------------------ routes


@router.post("", response_model=ScheduleActionOut, status_code=201)
async def create_schedule(
    payload: ScheduleCreateRequest,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> ScheduleActionOut:
    registry = getattr(request.app.state, "scraper_registry", None)
    if registry is not None and registry.entry(payload.actor_id) is None:
        raise NotFoundError(f"Scraper not found: {payload.actor_id}")
    org = await _ctx_org(session, user)
    schedule = await _service(session).create(
        actor_id=payload.actor_id,
        input=payload.input,
        schedule_type=payload.schedule_type,
        config=payload.config,
        name=payload.name,
        interval_seconds=payload.interval_seconds,
        daily_time=payload.daily_time,
        timezone_name=payload.timezone,
        max_runs=payload.max_runs,
        start_at=payload.start_at,
        created_by=user.id,
        organization_id=org,
    )
    return ScheduleActionOut(data=_out(schedule))


@router.get("", response_model=ScheduleListOut)
async def list_schedules(
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.view"))],
    actor_id: str | None = Query(default=None, max_length=100),
    enabled: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> ScheduleListOut:
    org = await _ctx_org(session, user)
    rows, total = await _service(session).list_schedules(
        actor_id=actor_id,
        organization_id=org,
        enabled=enabled,
        page=page,
        page_size=page_size,
    )
    return ScheduleListOut(
        data=[_out(r) for r in rows],
        meta=PageMeta(page=page, page_size=page_size, total=total).model_dump(),
    )


async def _visible_schedule(session, schedule_id: uuid.UUID, user):
    org = await _ctx_org(session, user)
    schedule = await _service(session).get(schedule_id)
    if (
        schedule.organization_id is not None
        and org is not None
        and schedule.organization_id != org
    ):
        raise NotFoundError("Schedule not found")
    return schedule


@router.get("/{schedule_id}", response_model=ScheduleActionOut)
async def get_schedule(
    schedule_id: uuid.UUID,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.view"))],
) -> ScheduleActionOut:
    schedule = await _visible_schedule(session, schedule_id, user)
    return ScheduleActionOut(data=_out(schedule))


@router.post("/{schedule_id}/enable", response_model=ScheduleActionOut)
async def enable_schedule(
    schedule_id: uuid.UUID,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> ScheduleActionOut:
    schedule = await _visible_schedule(session, schedule_id, user)
    updated = await _service(session).set_enabled(schedule.id, True)
    return ScheduleActionOut(data=_out(updated))


@router.post("/{schedule_id}/disable", response_model=ScheduleActionOut)
async def disable_schedule(
    schedule_id: uuid.UUID,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> ScheduleActionOut:
    schedule = await _visible_schedule(session, schedule_id, user)
    updated = await _service(session).set_enabled(schedule.id, False)
    return ScheduleActionOut(data=_out(updated))


@router.post("/{schedule_id}/run-now", response_model=ScheduleActionOut)
async def run_schedule_now(
    schedule_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> ScheduleActionOut:
    """Pull next_run_at to now so the worker's next tick fires immediately."""
    from datetime import datetime, timezone

    schedule = await _visible_schedule(session, schedule_id, user)
    if not schedule.enabled:
        raise ConflictError("Schedule is disabled — enable it first")
    registry = getattr(request.app.state, "scraper_registry", None)
    if registry is not None and registry.entry(schedule.actor_id) is None:
        raise NotFoundError(f"Scraper not found: {schedule.actor_id}")
    schedule.next_run_at = datetime.now(timezone.utc)
    schedule.enabled = True
    await session.commit()
    await session.refresh(schedule)
    return ScheduleActionOut(data=_out(schedule))


@router.delete("/{schedule_id}")
async def delete_schedule(
    schedule_id: uuid.UUID,
    session: DbSession,
    user: Annotated[User, Depends(require_permission("scraping.run"))],
) -> dict[str, Any]:
    schedule = await _visible_schedule(session, schedule_id, user)
    await _service(session).delete(schedule.id)
    return {"success": True, "data": {"deleted": str(schedule_id)}}
