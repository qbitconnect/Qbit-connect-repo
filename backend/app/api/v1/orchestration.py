"""QBIT CONNECT — Orchestration REST API (Brief §8, §9, §10, §12, §18).

Endpoints for tool registry discovery, execution planning, source selection,
deterministic job execution, real progress snapshots, and structured diagnostics.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_permission
from app.models.scrape import ScrapeJob
from app.models.user import User
from app.services.orchestration.orchestrator import ScrapingOrchestrator

router = APIRouter(prefix="/orchestration", tags=["orchestration"])

# RBAC dependencies
scraping_view = require_permission("scraping.view")
scraping_run = require_permission("scraping.run")


class PlanRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500, description="Natural language search or instruction")
    source: str | None = Field(default=None, description="Optional forced or preferred source")
    source_lock: bool | None = Field(default=None, description="Whether to lock execution strictly to the chosen source")
    target_count: int | None = Field(default=None, ge=1, le=100000, description="Target record count")


class ExecutePlanRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)
    primary_tool: str = Field(..., min_length=1, max_length=100)
    source_locked: bool = Field(default=False)
    target_count: int = Field(default=100, ge=1, le=100000)
    input_payload: dict[str, Any] = Field(default_factory=dict)
    max_runtime_seconds: int = Field(default=3600, ge=30, le=86400)


def _get_orchestrator(request: Request) -> ScrapingOrchestrator:
    registry = request.app.state.scraper_registry
    queue = getattr(request.app.state, "queue", None)
    return ScrapingOrchestrator(actor_registry=registry, queue=queue)


@router.get("/tools", summary="List registered scraping tools & capabilities")
async def list_tools(
    request: Request,
    current_user: Annotated[User, Depends(scraping_view)],
):
    orchestrator = _get_orchestrator(request)
    tools = orchestrator.tool_registry.list_tools()
    return {
        "success": True,
        "data": [t.to_dict() for t in tools],
        "count": len(tools),
    }


@router.post("/interpret", summary="Interpret task query and parameters")
async def interpret_task(
    payload: PlanRequest,
    request: Request,
    current_user: Annotated[User, Depends(scraping_view)],
):
    orchestrator = _get_orchestrator(request)
    interpreted = orchestrator.interpreter.interpret(
        payload.query,
        forced_source=payload.source,
        forced_source_lock=payload.source_lock,
        target_count=payload.target_count,
    )
    return {
        "success": True,
        "data": interpreted.to_dict(),
    }


@router.post("/plan", summary="Generate structured execution plan")
async def generate_plan(
    payload: PlanRequest,
    request: Request,
    current_user: Annotated[User, Depends(scraping_view)],
):
    orchestrator = _get_orchestrator(request)
    plan = orchestrator.plan_task(
        payload.query,
        source=payload.source,
        source_lock=payload.source_lock,
        target_count=payload.target_count,
    )
    return {
        "success": True,
        "data": plan.to_dict(),
    }


@router.post("/execute", summary="Execute orchestration plan through JobEngine")
async def execute_plan(
    payload: ExecutePlanRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(scraping_run)],
):
    orchestrator = _get_orchestrator(request)
    plan = orchestrator.plan_task(
        payload.query,
        source=payload.primary_tool,
        source_lock=payload.source_locked,
        target_count=payload.target_count,
    )
    if payload.input_payload:
        plan.input_payload.update(payload.input_payload)

    job = await orchestrator.execute_plan(
        plan,
        session,
        user_id=current_user.id,
        organization_id=getattr(current_user, "organization_id", None),
    )

    return {
        "success": True,
        "data": {
            "job_id": str(job.id),
            "status": job.status,
            "actor_id": job.actor_id,
            "target": plan.target_count,
            "source_locked": plan.source_locked,
            "mode": plan.mode,
            "created_at": job.created_at.isoformat() if job.created_at else None,
        },
    }


@router.get("/jobs/{job_id}/progress", summary="Get deterministic progress & completion snapshot")
async def job_progress(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(scraping_view)],
):
    from sqlalchemy import select

    res = await session.execute(select(ScrapeJob).where(ScrapeJob.id == job_id))
    job = res.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    orchestrator = _get_orchestrator(request)
    snapshot = await orchestrator.evaluate_job_progress(job)
    return {
        "success": True,
        "data": snapshot.to_dict(),
    }


@router.get("/jobs/{job_id}/diagnostics", summary="Get structured diagnostics for job")
async def job_diagnostics(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(scraping_view)],
):
    from sqlalchemy import select

    res = await session.execute(select(ScrapeJob).where(ScrapeJob.id == job_id))
    job = res.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    orchestrator = _get_orchestrator(request)
    diagnostics = orchestrator.diagnose_job_failure(job)
    return {
        "success": True,
        "data": diagnostics.to_dict(),
    }
