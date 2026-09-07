"""Automation analytics foundation (§51) — real aggregates only."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import ExecutionStatus, WorkflowExecution, WorkflowExecutionStep


async def workflow_stats(session: AsyncSession, workflow_id: uuid.UUID) -> dict:
    """Per-workflow execution aggregates (§51): counts, rates, avg duration,
    last execution — all computed from real rows."""
    rows = (await session.execute(
        select(
            WorkflowExecution.status,
            func.count(WorkflowExecution.id),
        ).where(WorkflowExecution.workflow_id == workflow_id)
        .group_by(WorkflowExecution.status)
    )).all()
    counts = {status: int(n) for status, n in rows}

    total = sum(counts.values())
    completed = counts.get(ExecutionStatus.COMPLETED, 0)
    failed = counts.get(ExecutionStatus.FAILED, 0)

    avg_duration = await session.scalar(
        select(
            func.avg(
                func.extract("epoch", WorkflowExecution.completed_at)
                - func.extract("epoch", WorkflowExecution.started_at)
            )
        ).where(
            WorkflowExecution.workflow_id == workflow_id,
            WorkflowExecution.completed_at.is_not(None),
            WorkflowExecution.started_at.is_not(None),
        )
    )
    last_execution = await session.scalar(
        select(func.max(WorkflowExecution.created_at))
        .where(WorkflowExecution.workflow_id == workflow_id)
    )

    return {
        "executions_total": total,
        "executions_completed": completed,
        "executions_failed": failed,
        "executions_waiting": counts.get(ExecutionStatus.WAITING, 0),
        "executions_cancelled": counts.get(ExecutionStatus.CANCELLED, 0),
        "executions_queued": counts.get(ExecutionStatus.QUEUED, 0),
        "executions_running": counts.get(ExecutionStatus.RUNNING, 0),
        "success_rate": round(completed / total, 4) if total else None,
        "failure_rate": round(failed / total, 4) if total else None,
        "avg_duration_seconds": round(float(avg_duration), 2) if avg_duration is not None else None,
        "last_execution_at": _iso(last_execution),
    }


async def global_counters(session: AsyncSession) -> dict:
    """Execution monitor counters (§51)."""
    rows = (await session.execute(
        select(WorkflowExecution.status, func.count(WorkflowExecution.id))
        .group_by(WorkflowExecution.status)
    )).all()
    counts = {status: int(n) for status, n in rows}
    steps = (await session.execute(
        select(WorkflowExecutionStep.status, func.count(WorkflowExecutionStep.id))
        .group_by(WorkflowExecutionStep.status)
    )).all()
    step_counts = {status: int(n) for status, n in steps}
    return {
        "total": sum(counts.values()),
        "queued": counts.get(ExecutionStatus.QUEUED, 0),
        "running": counts.get(ExecutionStatus.RUNNING, 0),
        "waiting": counts.get(ExecutionStatus.WAITING, 0),
        "completed": counts.get(ExecutionStatus.COMPLETED, 0),
        "failed": counts.get(ExecutionStatus.FAILED, 0),
        "cancelled": counts.get(ExecutionStatus.CANCELLED, 0),
        "skipped_steps": step_counts.get("SKIPPED", 0),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
