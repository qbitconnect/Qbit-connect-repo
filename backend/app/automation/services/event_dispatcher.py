"""Event intake dispatcher (§12, §13, §40, §41).

System events arrive here from thin hooks in the existing service layer (lead
services, campaign EventService, inbox ConversationEngine, scrape runner).
The dispatcher:

1. records a deduplicated `WorkflowEvent` row (unique `event_id`, §13)
2. walks the causation chain and enforces the depth cap (§40)
3. enforces the per-(workflow, entity) execution window limit (§40)
4. creates QUEUED `WorkflowExecution` rows for every matching ACTIVE workflow
   version (idempotent via UNIQUE(workflow_id, trigger_event_id))

It NEVER executes workflows — execution happens only in the automation worker
(§12). The public `emit()` wrapper is best-effort: a dispatcher failure never
breaks the primary business flow (same contract as AuditService).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, log_with
from app.models.automation import (
    ExecutionStatus,
    Workflow,
    WorkflowEvent,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowVersion,
    WorkflowVersionStatus,
)

logger = get_logger("qbit.automation.dispatcher")

#: Contextvar set by the execution engine around ACTION nodes — any events
#: emitted by those actions carry the execution id as causation (§41).
_causation_ctx: "Any" = None


def _causation_contextvar():
    global _causation_ctx
    if _causation_ctx is None:
        import contextvars

        _causation_ctx = contextvars.ContextVar("qbit_automation_causation", default=None)
    return _causation_ctx


def current_causation_id() -> str | None:
    return _causation_contextvar().get()


def _settings():
    from app.core.config import get_settings

    return get_settings()


async def emit_system_event(
    session: AsyncSession,
    *,
    event_type: str,
    entity_type: str | None = None,
    entity_id: Any = None,
    payload: dict | None = None,
    event_id: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    settings: Any = None,
) -> list[WorkflowExecution]:
    """Best-effort intake: never raises (failures are logged and swallowed).

    Returns the list of executions created (usually 0 or 1; multiple when
    several workflows share the trigger).
    """
    try:
        return await dispatch_event(
            session,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            event_id=event_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            settings=settings,
        )
    except Exception:  # noqa: BLE001 — automation must never break business flow
        logger.exception("Automation event dispatch failed", extra={
            "extra_fields": {"event_type": event_type}})

        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


async def dispatch_event(
    session: AsyncSession,
    *,
    event_type: str,
    entity_type: str | None = None,
    entity_id: Any = None,
    payload: dict | None = None,
    event_id: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    settings: Any = None,
) -> list[WorkflowExecution]:
    """Core intake (raises; the emit() wrapper handles isolation)."""
    from app.automation.triggers.definitions import build_trigger_registry

    settings = settings or _settings()
    registry = build_trigger_registry()
    event_index = registry.event_index()

    trigger_types = event_index.get(event_type) or []
    if not trigger_types:
        return []

    now = datetime.now(timezone.utc)
    payload = payload or {}
    if causation_id is None:
        causation_id = current_causation_id()

    eid = event_id or f"{event_type}:{entity_type or 'x'}:{entity_id or 'x'}:{uuid.uuid4().hex}"

    # ---- causation depth (§40) ------------------------------------------------
    depth = 0
    if causation_id:
        depth = await _causation_depth(session, causation_id)
        max_depth = getattr(settings, "QBIT_AUTOMATION_MAX_CAUSATION_DEPTH", 2)
        if depth > max_depth:
            log_with(logger, 20, "Automation event blocked: causation depth", **{
                "event_type": event_type, "depth": depth})
            return []

    # ---- intake row (dedup, §13) ----------------------------------------------
    existing = (await session.execute(
        select(WorkflowEvent.id).where(WorkflowEvent.event_id == eid).limit(1)
    )).scalars().first()
    if existing is not None:
        return []  # same event already processed → never duplicate (§13)

    session.add(WorkflowEvent(
        event_id=eid[:160],
        event_type=event_type[:60],
        entity_type=entity_type,
        entity_id=_as_uuid(entity_id),
        payload=_safe_payload(payload),
        causation_id=str(causation_id)[:160] if causation_id else None,
        correlation_id=str(correlation_id)[:160] if correlation_id else None,
        created_at=now,
    ))

    # ---- matching ACTIVE workflows ---------------------------------------------
    rows = (await session.execute(
        select(Workflow, WorkflowVersion)
        .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
        .where(
            Workflow.status == WorkflowStatus.ACTIVE,
            Workflow.trigger_type.in_(trigger_types),
            WorkflowVersion.status == WorkflowVersionStatus.PUBLISHED,
        )
    )).all()
    if not rows:
        return []

    # keep only the LATEST published version per workflow
    latest: dict[uuid.UUID, tuple[Workflow, WorkflowVersion]] = {}
    for workflow, version in rows:
        cur = latest.get(workflow.id)
        if cur is None or version.version > cur[1].version:
            latest[workflow.id] = (workflow, version)

    created: list[WorkflowExecution] = []
    for workflow, version in latest.values():
        trigger = registry.require(workflow.trigger_type)

        # trigger configuration from THIS version's definition (§60)
        trigger_node = _trigger_node(version)
        config = (trigger_node or {}).get("trigger_config") or {}

        # trigger-specific event filtering (tag/status filters)
        if not trigger.matches(event_type, payload, config):
            continue

        refs = trigger.build_context(event_type, payload, entity_id)
        try:
            trigger.validate_config(config)
        except Exception:  # noqa: BLE001 — a bad config never fires
            continue

        # per-(workflow, entity) window limit (§40)
        entity_uuid = _as_uuid(entity_id)
        if entity_uuid is not None and not await _within_window(
            session, workflow.id, entity_uuid, settings, now
        ):
            log_with(logger, 20, "Automation execution skipped: window limit", **{
                "workflow_id": str(workflow.id), "entity_id": str(entity_uuid)})
            continue

        execution = WorkflowExecution(
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            trigger_event_id=eid[:160],
            entity_type=trigger.ENTITY_TYPE or entity_type or "entity",
            entity_id=entity_uuid,
            status=ExecutionStatus.QUEUED,
            current_node_id=None,
            context={
                "trigger_type": workflow.trigger_type,
                "event_type": event_type,
                "refs": refs,
                "variables": {},
                "depth": depth,
            },
            causation_id=str(causation_id)[:160] if causation_id else None,
            correlation_id=str(correlation_id)[:160] if correlation_id else None,
        )
        session.add(execution)
        created.append(execution)

    try:
        await session.flush()
    except Exception:  # noqa: BLE001 — concurrent duplicate; caller txn safe?
        raise
    return created


async def _causation_depth(session: AsyncSession, execution_id: str, *, max_hops: int = 10) -> int:
    """Walk execution → its triggering event → that event's causation → …
    Bounded walk (max_hops) — returns the chain depth (§41)."""
    depth = 0
    current = str(execution_id)
    seen: set[str] = set()
    while current and current not in seen and depth < max_hops:
        seen.add(current)
        row = (await session.execute(
            select(WorkflowExecution.trigger_event_id, WorkflowExecution.causation_id)
            .where(WorkflowExecution.id == _as_uuid(current))
            .limit(1)
        )).first()
        if row is None:
            break
        depth += 1
        current = row.causation_id or None
    return depth


async def _within_window(
    session: AsyncSession, workflow_id: uuid.UUID, entity_id: uuid.UUID,
    settings: Any, now: datetime,
) -> bool:
    limit = getattr(settings, "QBIT_AUTOMATION_MAX_EXECUTIONS_PER_WINDOW", 10)
    window_minutes = getattr(settings, "QBIT_AUTOMATION_WINDOW_MINUTES", 60)
    since = now - timedelta(minutes=window_minutes)
    count = await session.scalar(
        select(func.count()).select_from(WorkflowExecution).where(
            WorkflowExecution.workflow_id == workflow_id,
            WorkflowExecution.entity_id == entity_id,
            WorkflowExecution.created_at >= since,
        )
    )
    return int(count or 0) < limit


def _trigger_node(version: WorkflowVersion) -> dict | None:
    """Extract the TRIGGER node dict from a stored version definition."""
    definition = version.definition or {}
    for node in definition.get("nodes") or []:
        if node.get("type") == "TRIGGER":
            return node
    return None


def _safe_payload(payload: dict) -> dict:
    """Bounded, redacted payload snapshot (§73, §76)."""
    from app.core.logging import redact

    try:
        return redact(dict(payload or {}))
    except Exception:  # noqa: BLE001
        return {}


def _as_uuid(value: Any):
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
