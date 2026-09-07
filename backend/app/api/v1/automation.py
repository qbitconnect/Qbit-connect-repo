"""Workflow Automation API (Phase 9 §69).

All routes are JWT-authenticated and enforce `automation.*` permissions
SERVER-SIDE (§61). Workflow definitions are declarative data only — the API
rejects anything that is not a validated node graph (§63). Mutations are
audit-logged (§62). Responses never contain secrets (§76).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditDep, DbSession, require_permission
from app.automation.services.analytics import global_counters, workflow_stats
from app.automation.core.exceptions import (
    AutomationError,
    ValidationError as AutomationValidationError,
)
from app.automation.services.workflow_service import WorkflowService
from app.automation.templates import TEMPLATE_CATALOG, template_definition
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.automation import (
    ExecutionStatus,
    Workflow,
    WorkflowEvent,
    WorkflowExecution,
    WorkflowExecutionStep,
)
from app.models.user import User

router = APIRouter(prefix="/automation", tags=["automation"])

service = WorkflowService()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _page_envelope(items: list, total: int, page: int, page_size: int) -> dict:
    total_pages = (total + page_size - 1) // page_size if page_size else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(total_pages, 1),
    }


# ------------------------------------------------------------------ schemas
class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    trigger_type: str = Field(min_length=1, max_length=50)
    definition: dict = Field(min_length=1)


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    definition: dict | None = None


class FromTemplateIn(BaseModel):
    template_id: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=200)


async def _load_workflow(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError("Workflow not found")
    return workflow


# ------------------------------------------------------------------ workflows
@router.get("/workflows")
async def list_workflows(
    request: Request,
    session: DbSession,
    settings: Settings = Depends(_settings),
    user: User = Depends(require_permission("automation.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    status: str | None = Query(default=None),
    trigger_type: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
):
    items, total = await service.list(
        session, page=page, page_size=page_size, status=status,
        trigger_type=trigger_type, search=search,
    )
    return {"success": True, "data": _page_envelope(items, total, page, page_size)}


@router.post("/workflows", status_code=201)
async def create_workflow(
    request: Request,
    session: DbSession,
    body: WorkflowCreate,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.create")),
):
    try:
        workflow = await service.create(
            session,
            name=body.name,
            description=body.description,
            trigger_type=body.trigger_type,
            definition=body.definition,
            user_id=user.id,
        )
    except AutomationValidationError as exc:
        raise ValidationError(str(exc), details={"issues": exc.issues})
    await audit.log(
        session, action="automation.workflow_created", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id),
        metadata={"name": workflow.name, "trigger_type": workflow.trigger_type},
    )
    return {"success": True, "data": workflow.to_public_dict()}


@router.get("/workflows/{workflow_id}")
async def get_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    user: User = Depends(require_permission("automation.view")),
):
    workflow = await _load_workflow(session, workflow_id)
    data = workflow.to_public_dict()
    data["stats"] = await workflow_stats(session, workflow.id)
    data["definition"] = await service.definition_of(session, workflow)
    versions = await service.list_versions(session, workflow)
    data["versions"] = [{"version": v.version, "status": v.status,
                         "checksum": v.checksum,
                         "published_at": v.published_at.isoformat() if v.published_at else None}
                        for v in versions]
    return {"success": True, "data": data}


@router.patch("/workflows/{workflow_id}")
async def update_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    body: WorkflowUpdate,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.edit")),
):
    workflow = await _load_workflow(session, workflow_id)
    if body.name is None and body.description is None and body.definition is None:
        raise ValidationError(["Nothing to update"])
    changed_fields = [k for k, v in (("name", body.name), ("description", body.description),
                                     ("definition", body.definition)) if v is not None]
    workflow = await service.update(
        session, workflow, name=body.name, description=body.description,
        definition=body.definition, user_id=user.id,
    )
    await audit.log(
        session, action="automation.workflow_edited", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id),
        metadata={"changed_fields": changed_fields},
    )
    return {"success": True, "data": workflow.to_public_dict()}


@router.delete("/workflows/{workflow_id}", status_code=200)
async def delete_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.delete")),
):
    workflow = await _load_workflow(session, workflow_id)
    await audit.log(
        session, action="automation.workflow_deleted", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id),
        metadata={"name": workflow.name},
    )
    await service.delete(session, workflow)
    return {"success": True, "data": {"deleted": True}}


@router.post("/workflows/{workflow_id}/validate")
async def validate_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    user: User = Depends(require_permission("automation.view")),
):
    workflow = await _load_workflow(session, workflow_id)
    report = await service.validate(session, workflow)
    return {"success": True, "data": report}


@router.post("/workflows/{workflow_id}/publish")
async def publish_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.publish")),
):
    workflow = await _load_workflow(session, workflow_id)
    version = await service.publish(session, workflow, user_id=user.id)
    await audit.log(
        session, action="automation.workflow_published", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id),
        metadata={"version": version.version, "checksum": version.checksum},
    )
    return {"success": True, "data": {
        "workflow": workflow.to_public_dict(), "version": version.to_public_dict()}}


@router.post("/workflows/{workflow_id}/pause")
async def pause_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.pause")),
):
    workflow = await _load_workflow(session, workflow_id)
    workflow = await service.pause(session, workflow)
    await audit.log(
        session, action="automation.workflow_paused", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id),
    )
    return {"success": True, "data": workflow.to_public_dict()}


@router.post("/workflows/{workflow_id}/resume")
async def resume_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.resume")),
):
    workflow = await _load_workflow(session, workflow_id)
    workflow = await service.resume(session, workflow)
    await audit.log(
        session, action="automation.workflow_resumed", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id),
    )
    return {"success": True, "data": workflow.to_public_dict()}


@router.post("/workflows/{workflow_id}/duplicate", status_code=201)
async def duplicate_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.create")),
):
    workflow = await _load_workflow(session, workflow_id)
    copy = await service.duplicate(session, workflow, user_id=user.id)
    await audit.log(
        session, action="automation.workflow_duplicated", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(copy.id),
        metadata={"source_id": str(workflow.id)},
    )
    return {"success": True, "data": copy.to_public_dict()}


@router.post("/workflows/{workflow_id}/archive")
async def archive_workflow(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.edit")),
):
    workflow = await _load_workflow(session, workflow_id)
    workflow = await service.archive(session, workflow)
    await audit.log(
        session, action="automation.workflow_archived", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id),
    )
    return {"success": True, "data": workflow.to_public_dict()}


@router.get("/workflows/{workflow_id}/versions")
async def list_versions(
    request: Request,
    session: DbSession,
    workflow_id: uuid.UUID,
    user: User = Depends(require_permission("automation.view")),
):
    workflow = await _load_workflow(session, workflow_id)
    versions = await service.list_versions(session, workflow)
    return {"success": True, "data": {
        "items": [v.to_public_dict() for v in versions]}}


@router.post("/workflows/from-template", status_code=201)
async def create_from_template(
    request: Request,
    session: DbSession,
    body: FromTemplateIn,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.create")),
):
    """Starter templates create DRAFT workflows — never auto-activated (§52)."""
    spec = TEMPLATE_CATALOG.get(body.template_id)
    if spec is None:
        raise NotFoundError(f"Unknown template: {body.template_id!r}")
    workflow = await service.create(
        session,
        name=(body.name or spec["name"])[:200],
        description=spec["description"],
        trigger_type=spec["trigger_type"],
        definition=template_definition(body.template_id),
        user_id=user.id,
    )
    await audit.log(
        session, action="automation.workflow_created", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id),
        metadata={"from_template": body.template_id},
    )
    return {"success": True, "data": workflow.to_public_dict()}


# ----------------------------------------------------------------- templates
@router.get("/templates")
async def list_templates(
    request: Request,
    user: User = Depends(require_permission("automation.view")),
):
    items = [
        {"id": tid, "name": spec["name"], "description": spec["description"],
         "trigger_type": spec["trigger_type"]}
        for tid, spec in TEMPLATE_CATALOG.items()
    ]
    return {"success": True, "data": {"items": items, "total": len(items)}}


# ---------------------------------------------------------------- executions
@router.get("/executions")
async def list_executions(
    request: Request,
    session: DbSession,
    user: User = Depends(require_permission("automation.view_executions")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    workflow_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    entity_id: uuid.UUID | None = Query(default=None),
    trigger_type: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
):
    query = select(WorkflowExecution).order_by(
        desc(WorkflowExecution.created_at), WorkflowExecution.id
    )
    count_query = select(func.count()).select_from(WorkflowExecution)
    filters = []
    if workflow_id:
        filters.append(WorkflowExecution.workflow_id == workflow_id)
    if status:
        filters.append(WorkflowExecution.status == status.upper())
    if entity_type:
        filters.append(WorkflowExecution.entity_type == entity_type)
    if entity_id:
        filters.append(WorkflowExecution.entity_id == entity_id)
    if date_from:
        filters.append(WorkflowExecution.created_at >= date_from)
    if date_to:
        filters.append(WorkflowExecution.created_at <= date_to)
    if trigger_type:
        query = query.join(Workflow, Workflow.id == WorkflowExecution.workflow_id)
        count_query = count_query.join(Workflow, Workflow.id == WorkflowExecution.workflow_id)
        filters.append(Workflow.trigger_type == trigger_type)
    for f in filters:
        query = query.where(f)
        count_query = count_query.where(f)

    total = await session.scalar(count_query)
    rows = (await session.execute(
        query.offset(max(0, page - 1) * page_size).limit(page_size)
    )).scalars().all()
    items = [e.to_public_dict() for e in rows]
    return {"success": True, "data": _page_envelope(items, int(total or 0), page, page_size)}


@router.get("/executions/counters")
async def execution_counters(
    request: Request,
    session: DbSession,
    user: User = Depends(require_permission("automation.view_executions")),
):
    return {"success": True, "data": await global_counters(session)}


@router.get("/executions/{execution_id}")
async def get_execution(
    request: Request,
    session: DbSession,
    execution_id: uuid.UUID,
    user: User = Depends(require_permission("automation.view_executions")),
):
    execution = await session.get(WorkflowExecution, execution_id)
    if execution is None:
        raise NotFoundError("Execution not found")
    steps = (await session.execute(
        select(WorkflowExecutionStep)
        .where(WorkflowExecutionStep.execution_id == execution.id)
        .order_by(WorkflowExecutionStep.created_at, WorkflowExecutionStep.id)
    )).scalars().all()
    data = execution.to_public_dict()
    data["steps"] = [s.to_public_dict() for s in steps]
    return {"success": True, "data": data}


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(
    request: Request,
    session: DbSession,
    execution_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.execute")),
):
    execution = await session.get(WorkflowExecution, execution_id)
    if execution is None:
        raise NotFoundError("Execution not found")
    if execution.status in (
        ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED,
    ):
        raise ConflictError(f"Execution already finished ({execution.status})")
    execution.status = ExecutionStatus.CANCELLED
    execution.completed_at = datetime.now(timezone.utc)
    execution.locked_at = None
    execution.lease_owner = None
    await session.commit()
    await audit.log(
        session, action="automation.execution_cancelled", actor_user_id=user.id,
        resource_type="workflow_execution", resource_id=str(execution.id),
        metadata={"workflow_id": str(execution.workflow_id)},
    )
    return {"success": True, "data": execution.to_public_dict()}


@router.post("/executions/{execution_id}/retry")
async def retry_execution(
    request: Request,
    session: DbSession,
    execution_id: uuid.UUID,
    audit: AuditDep,
    user: User = Depends(require_permission("automation.execute")),
):
    """Requeue a FAILED execution from its current node (§37/§38 operator fix)."""
    execution = await session.get(WorkflowExecution, execution_id)
    if execution is None:
        raise NotFoundError("Execution not found")
    if execution.status not in (ExecutionStatus.FAILED, ExecutionStatus.CANCELLED):
        raise ConflictError("Only FAILED/CANCELLED executions can be retried")
    execution.status = ExecutionStatus.QUEUED
    execution.next_execution_at = datetime.now(timezone.utc)
    execution.error = None
    execution.error_class = None
    execution.locked_at = None
    execution.lease_owner = None
    await session.commit()
    await audit.log(
        session, action="automation.execution_retry_requested", actor_user_id=user.id,
        resource_type="workflow_execution", resource_id=str(execution.id),
        metadata={"workflow_id": str(execution.workflow_id)},
    )
    return {"success": True, "data": execution.to_public_dict()}


@router.get("/events")
async def list_events(
    request: Request,
    session: DbSession,
    user: User = Depends(require_permission("automation.view_executions")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    """Intake event log (§41 debugging aid) — IDs + types only."""
    total = await session.scalar(select(func.count()).select_from(WorkflowEvent))
    rows = (await session.execute(
        select(WorkflowEvent).order_by(desc(WorkflowEvent.created_at), WorkflowEvent.id)
        .offset(max(0, page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {"success": True, "data": _page_envelope(
        [e.to_public_dict() for e in rows], int(total or 0), page, page_size)}
