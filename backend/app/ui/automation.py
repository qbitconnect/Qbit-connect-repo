"""Automation UI (Phase 9 §43–§50).

Server-rendered workflow workspace in the established QBIT dark theme:
list, builder (lightweight, dependency-free — §43/§44), detail, execution
monitor and execution timeline. Cookie-session auth reusing the same service
layer as the REST API; permissions enforced here too (the UI is NEVER the
security boundary).

Realtime strategy (§71): lightweight polling on the execution monitor/detail
pages — consistent with the project's existing badge-polling mechanism; no new
realtime architecture is introduced.
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from typing import Annotated

from app.api.deps import get_db
from app.automation.conditions.catalog import FIELD_CATALOG
from app.automation.services.analytics import global_counters, workflow_stats
from app.automation.services.execution_engine import ExecutionEngine  # noqa: F401 (monitor link)
from app.automation.services.workflow_service import WorkflowService
from app.automation.templates import TEMPLATE_CATALOG, template_definition
from app.automation.triggers.definitions import build_trigger_registry
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, QBITError, ValidationError
from app.models.automation import (
    ExecutionStatus,
    Workflow,
    WorkflowExecution,
    WorkflowExecutionStep,
)
from app.models.user import User
from app.services.audit import AuditService
from app.ui import _ctx, templates
from app.ui import ui_user_for as require_ui_permission

SessionDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(tags=["automation-ui"])

service = WorkflowService()

_TRIGGER_LABELS = {t.TRIGGER_TYPE: t.LABEL for t in build_trigger_registry().all().values()}
_ACTION_KEYS = [
    "add_tag", "remove_tag", "change_status", "update_lead", "add_note",
    "assign_user", "assign_conversation", "unassign_conversation", "assign_team",
    "change_conversation_status", "change_priority", "add_internal_note",
    "send_whatsapp", "send_email", "start_campaign",
]

#: workflow-usable operators for the builder dropdown (§14)
_OPERATORS = [
    "equals", "not_equals", "contains", "not_contains", "starts_with",
    "ends_with", "is_empty", "is_not_empty", "greater_than", "less_than",
    "greater_or_equal", "less_or_equal", "between", "in", "not_in",
]


def _perms(request: Request) -> set[str]:
    return getattr(request.state, "ui_permissions", None) or set()


def _flash(url: str, message: str, kind: str = "ok") -> RedirectResponse:
    return RedirectResponse(
        f"{url}?flash={quote(message)}&flash_kind={kind}", status_code=303
    )


async def _load_workflow(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError("Workflow not found")
    return workflow


# --------------------------------------------------------------------- list
@router.get("/automation", response_class=HTMLResponse)
async def automation_home(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("automation.view")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    items, total = await service.list(session, page=1, page_size=100)
    return templates.TemplateResponse(
        request, "automation/index.html", _ctx(
            request, user, items=items, total=total,
            trigger_labels=_TRIGGER_LABELS, templates=TEMPLATE_CATALOG,
            perms=_perms(request), flash=flash, flash_kind=flash_kind,
        )
    )


# ------------------------------------------------------------------ builder
@router.get("/automation/new", response_class=HTMLResponse)
async def automation_new(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("automation.create")),
    from_template: str | None = None,
):
    definition = None
    name, description, trigger_type = "", "", "LEAD_CREATED"
    if from_template:
        spec = TEMPLATE_CATALOG.get(from_template)
        if spec is not None:
            definition = template_definition(from_template)
            name, trigger_type = spec["name"], spec["trigger_type"]
            description = spec["description"]
    return _builder_response(
        request, user, session, workflow=None, definition=definition or _default_definition(),
        name=name, description=description, trigger_type=trigger_type,
    )


@router.post("/automation/new")
async def automation_create(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("automation.create")),
    name: str = Form(...),
    description: str = Form(""),
    trigger_type: str = Form(...),
    definition_json: str = Form(...),
):
    try:
        definition = json.loads(definition_json)
        workflow = await service.create(
            session, name=name, description=description or None,
            trigger_type=trigger_type, definition=definition, user_id=user.id,
        )
        audit = AuditService()
        await audit.log(
            session, action="automation.workflow_created", actor_user_id=user.id,
            resource_type="workflow", resource_id=str(workflow.id),
            metadata={"name": workflow.name, "via": "ui"},
        )
        return _flash(f"/automation/{workflow.id}", "Workflow draft created")
    except (ValidationError, QBITError) as exc:
        message = getattr(exc, "issues", None) or [str(exc)]
        return _builder_response(
            request, user, session, workflow=None,
            definition=json.loads(definition_json) if definition_json else _default_definition(),
            name=name, description=description, trigger_type=trigger_type,
            error="; ".join(str(m) for m in message[:5]),
        )


@router.get("/automation/{workflow_id}/edit", response_class=HTMLResponse)
async def automation_edit(
    request: Request,
    session: SessionDep,
    workflow_id: uuid.UUID,
    user: User = Depends(require_ui_permission("automation.edit")),
):
    workflow = await _load_workflow(session, workflow_id)
    definition = await service.definition_of(session, workflow)
    return _builder_response(
        request, user, session, workflow=workflow, definition=definition,
        name=workflow.name, description=workflow.description or "",
        trigger_type=workflow.trigger_type,
    )


@router.post("/automation/{workflow_id}/edit")
async def automation_save(
    request: Request,
    session: SessionDep,
    workflow_id: uuid.UUID,
    user: User = Depends(require_ui_permission("automation.edit")),
    name: str = Form(...),
    description: str = Form(""),
    trigger_type: str = Form(...),
    definition_json: str = Form(...),
):
    workflow = await _load_workflow(session, workflow_id)
    try:
        definition = json.loads(definition_json)
        await service.update(
            session, workflow, name=name, description=description or None,
            definition=definition, user_id=user.id,
        )
        audit = AuditService()
        await audit.log(
            session, action="automation.workflow_edited", actor_user_id=user.id,
            resource_type="workflow", resource_id=str(workflow.id),
            metadata={"via": "ui"},
        )
        return _flash(f"/automation/{workflow.id}", "Draft saved")
    except (ValidationError, QBITError) as exc:
        message = getattr(exc, "issues", None) or [str(exc)]
        return _builder_response(
            request, user, session, workflow=workflow,
            definition=json.loads(definition_json),
            name=name, description=description, trigger_type=trigger_type,
            error="; ".join(str(m) for m in message[:5]),
        )


def _builder_response(request, user, session, *, workflow, definition, name,
                      description, trigger_type, error: str | None = None):
    return templates.TemplateResponse(
        request, "automation/builder.html", _ctx(
            request, user, workflow=workflow, definition=definition,
            name=name, description=description, trigger_type=trigger_type,
            trigger_types=sorted(_TRIGGER_LABELS), trigger_labels=_TRIGGER_LABELS,
            fields=sorted(FIELD_CATALOG.keys()), operators=_OPERATORS,
            action_keys=_ACTION_KEYS, error=error, perms=_perms(request),
        )
    )


def _default_definition() -> dict:
    return {
        "nodes": [
            {"id": "trigger", "type": "TRIGGER", "trigger_config": {"type": "LEAD_CREATED"},
             "next_node_id": "end"},
            {"id": "end", "type": "END"},
        ],
    }


# ------------------------------------------------------------------- detail
@router.get("/automation/{workflow_id}", response_class=HTMLResponse)
async def automation_detail(
    request: Request,
    session: SessionDep,
    workflow_id: uuid.UUID,
    user: User = Depends(require_ui_permission("automation.view")),
    flash: str | None = None,
    flash_kind: str = "ok",
):
    workflow = await _load_workflow(session, workflow_id)
    definition = await service.definition_of(session, workflow)
    versions = await service.list_versions(session, workflow)
    stats = await workflow_stats(session, workflow.id)
    executions = (await session.execute(
        select(WorkflowExecution)
        .where(WorkflowExecution.workflow_id == workflow.id)
        .order_by(desc(WorkflowExecution.created_at)).limit(10)
    )).scalars().all()
    validation = await service.validate(session, workflow)
    return templates.TemplateResponse(
        request, "automation/detail.html", _ctx(
            request, user, workflow=workflow, definition=definition,
            versions=versions, stats=stats, executions=executions,
            validation=validation, trigger_labels=_TRIGGER_LABELS,
            perms=_perms(request), flash=flash, flash_kind=flash_kind,
        )
    )


# -------------------------------------------------------- lifecycle actions
def _workflow_action(permission: str, method_name: str, success_message: str):
    async def endpoint(
        request: Request,
        session: SessionDep,
        workflow_id: uuid.UUID,
        user: User = Depends(require_ui_permission(permission)),
    ):
        workflow = await _load_workflow(session, workflow_id)
        try:
            await getattr(service, method_name)(session, workflow, user_id=user.id) \
                if method_name in ("publish", "duplicate") \
                else await getattr(service, method_name)(session, workflow)
        except ConflictError as exc:
            return _flash(f"/automation/{workflow.id}", str(exc), "error")
        except ValidationError as exc:
            return _flash(f"/automation/{workflow.id}", "; ".join(exc.issues[:3]), "error")
        audit = AuditService()
        await audit.log(
            session, action=f"automation.workflow_{method_name}d", actor_user_id=user.id,
            resource_type="workflow", resource_id=str(workflow.id), metadata={"via": "ui"},
        )
        return _flash(f"/automation/{workflow.id}", success_message)
    endpoint.__name__ = f"automation_{method_name}"
    return endpoint


router.add_api_route(
    "/automation/{workflow_id}/publish", _workflow_action(
        "automation.publish", "publish", "Workflow published — now ACTIVE"),
    methods=["POST"], response_class=RedirectResponse,
)
router.add_api_route(
    "/automation/{workflow_id}/pause", _workflow_action(
        "automation.pause", "pause", "Workflow paused"),
    methods=["POST"], response_class=RedirectResponse,
)
router.add_api_route(
    "/automation/{workflow_id}/resume", _workflow_action(
        "automation.resume", "resume", "Workflow resumed"),
    methods=["POST"], response_class=RedirectResponse,
)
router.add_api_route(
    "/automation/{workflow_id}/duplicate", _workflow_action(
        "automation.create", "duplicate", "Workflow duplicated as draft"),
    methods=["POST"], response_class=RedirectResponse,
)
router.add_api_route(
    "/automation/{workflow_id}/archive", _workflow_action(
        "automation.edit", "archive", "Workflow archived"),
    methods=["POST"], response_class=RedirectResponse,
)


@router.post("/automation/{workflow_id}/delete")
async def automation_delete(
    request: Request,
    session: SessionDep,
    workflow_id: uuid.UUID,
    user: User = Depends(require_ui_permission("automation.delete")),
):
    workflow = await _load_workflow(session, workflow_id)
    try:
        await service.delete(session, workflow)
    except ConflictError as exc:
        return _flash("/automation", str(exc), "error")
    audit = AuditService()
    await audit.log(
        session, action="automation.workflow_deleted", actor_user_id=user.id,
        resource_type="workflow", resource_id=str(workflow.id), metadata={"via": "ui"},
    )
    return _flash("/automation", "Draft deleted")


# -------------------------------------------------------------- executions
@router.get("/automation/executions", response_class=HTMLResponse)
async def automation_executions(
    request: Request,
    session: SessionDep,
    user: User = Depends(require_ui_permission("automation.view_executions")),
    page: int = Query(default=1, ge=1),
    status: str | None = None,
    workflow_id: str | None = None,
):
    from sqlalchemy import func as _func

    query = select(WorkflowExecution).order_by(desc(WorkflowExecution.created_at))
    count_query = select(_func.count()).select_from(WorkflowExecution)
    if status:
        query = query.where(WorkflowExecution.status == status.upper())
        count_query = count_query.where(WorkflowExecution.status == status.upper())
    if workflow_id:
        try:
            wid = uuid.UUID(workflow_id)
        except ValueError:
            wid = None
        if wid:
            query = query.where(WorkflowExecution.workflow_id == wid)
            count_query = count_query.where(WorkflowExecution.workflow_id == wid)
    total = await session.scalar(count_query)
    rows = (await session.execute(
        query.offset((page - 1) * 25).limit(25)
    )).scalars().all()
    items = []
    for e in rows:
        data = e.to_public_dict()
        if e.started_at and e.completed_at:
            data["duration_s"] = round((e.completed_at - e.started_at).total_seconds(), 1)
        else:
            data["duration_s"] = None
        items.append((e, data))
    counters = await global_counters(session)
    workflows = (await session.execute(select(Workflow).order_by(Workflow.name))).scalars().all()
    return templates.TemplateResponse(
        request, "automation/executions.html", _ctx(
            request, user, executions=[e for e, _ in items],
            execution_rows=items, total=int(total or 0), page=page,
            counters=counters, workflows=workflows, status=status,
            workflow_id=workflow_id, perms=_perms(request),
        )
    )


@router.get("/automation/executions/{execution_id}", response_class=HTMLResponse)
async def automation_execution_detail(
    request: Request,
    session: SessionDep,
    execution_id: uuid.UUID,
    user: User = Depends(require_ui_permission("automation.view_executions")),
):
    execution = await session.get(WorkflowExecution, execution_id)
    if execution is None:
        raise NotFoundError("Execution not found")
    workflow = await session.get(Workflow, execution.workflow_id)
    steps = (await session.execute(
        select(WorkflowExecutionStep)
        .where(WorkflowExecutionStep.execution_id == execution.id)
        .order_by(WorkflowExecutionStep.created_at, WorkflowExecutionStep.id)
    )).scalars().all()
    return templates.TemplateResponse(
        request, "automation/execution_detail.html", _ctx(
            request, user, execution=execution, workflow=workflow, steps=steps,
            perms=_perms(request),
        )
    )


@router.post("/automation/executions/{execution_id}/cancel")
async def automation_cancel_execution(
    request: Request,
    session: SessionDep,
    execution_id: uuid.UUID,
    user: User = Depends(require_ui_permission("automation.execute")),
):
    execution = await session.get(WorkflowExecution, execution_id)
    if execution is None:
        raise NotFoundError("Execution not found")
    terminal = {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
    if execution.status in terminal:
        return _flash(f"/automation/executions/{execution.id}",
                      f"Execution already finished ({execution.status})", "error")
    execution.status = ExecutionStatus.CANCELLED
    execution.completed_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    await session.commit()
    audit = AuditService()
    await audit.log(
        session, action="automation.execution_cancelled", actor_user_id=user.id,
        resource_type="workflow_execution", resource_id=str(execution.id),
        metadata={"via": "ui"},
    )
    return _flash(f"/automation/executions/{execution.id}", "Execution cancelled")
