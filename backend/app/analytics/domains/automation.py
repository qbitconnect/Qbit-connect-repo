"""Automation analytics (spec §15) — WorkflowExecution / step aggregates.

Reuses the Phase 9 `automation.services.analytics` definitions where they
exist (per-workflow stats, global counters) and adds period-scoped series,
per-workflow failure reasons and step/action counts. Analytics is read-only:
nothing here executes or modifies workflows (spec §15).
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.filters import AnalyticsFilters
from app.analytics.core.math import safe_rate
from app.analytics.core.query_builder import base_workflow_executions_query
from app.analytics.core.time import Period, day_bucket
from app.automation.services.analytics import workflow_stats as phase9_workflow_stats
from app.models.automation import ExecutionStatus, Workflow, WorkflowExecution, WorkflowExecutionStep

FAILED = ExecutionStatus.FAILED
COMPLETED = ExecutionStatus.COMPLETED


async def automation_kpis(session: AsyncSession, filters: AnalyticsFilters) -> dict:
    rows = (await session.execute(
        base_workflow_executions_query(filters).with_only_columns(
            WorkflowExecution.status, func.count(WorkflowExecution.id),
        ).group_by(WorkflowExecution.status)
    )).all()
    by_status = {status: int(n) for status, n in rows}

    avg_duration = await session.scalar(
        base_workflow_executions_query(filters).with_only_columns(
            func.avg(
                func.extract("epoch", WorkflowExecution.completed_at)
                - func.extract("epoch", WorkflowExecution.started_at)
            )
        ).where(
            WorkflowExecution.started_at.is_not(None),
            WorkflowExecution.completed_at.is_not(None),
        )
    )

    step_query = select(
        WorkflowExecutionStep.node_type,
        func.count(WorkflowExecutionStep.id),
    ).join(
        WorkflowExecution, WorkflowExecution.id == WorkflowExecutionStep.execution_id
    )
    if filters.period is not None:
        step_query = step_query.where(
            WorkflowExecution.created_at >= filters.period.start,
            WorkflowExecution.created_at < filters.period.end,
        )
    step_rows = (await session.execute(step_query.group_by(WorkflowExecutionStep.node_type))).all()
    steps = {node_type: int(n) for node_type, n in step_rows}

    workflows_active = int(await session.scalar(
        select(func.count(Workflow.id)).where(Workflow.status == "ACTIVE")
    ) or 0)

    total = sum(by_status.values())
    completed = by_status.get(COMPLETED, 0)
    failed = by_status.get(FAILED, 0)
    return {
        "workflows_active": workflows_active,
        "executions": total,
        "executions_completed": completed,
        "executions_failed": failed,
        "executions_cancelled": by_status.get(ExecutionStatus.CANCELLED, 0),
        "executions_waiting": by_status.get(ExecutionStatus.WAITING, 0)
        + by_status.get(ExecutionStatus.QUEUED, 0)
        + by_status.get(ExecutionStatus.RUNNING, 0)
        + by_status.get(ExecutionStatus.PAUSED, 0),
        "by_status": by_status,
        "action_steps_executed": sum(
            count for node_type, count in steps.items() if node_type == "ACTION"
        ),
        "steps_by_type": steps,
        "avg_duration_seconds": round(float(avg_duration), 2) if avg_duration is not None else None,
        "success_rate": safe_rate(completed, total),
        "failure_rate": safe_rate(failed, total),
    }


async def executions_timeseries(
    session: AsyncSession, filters: AnalyticsFilters, tz, dialect: str,
) -> list[dict]:
    period: Period = filters.period
    bucket = day_bucket(WorkflowExecution.created_at, str(tz), period.start, period.end,
                        dialect=dialect)
    rows = (await session.execute(
        base_workflow_executions_query(filters).with_only_columns(
            bucket.label("day"),
            func.count(WorkflowExecution.id).label("executions"),
            func.sum(case((WorkflowExecution.status == COMPLETED, 1), else_=0)).label("completed"),
            func.sum(case((WorkflowExecution.status == FAILED, 1), else_=0)).label("failed"),
        ).group_by(bucket).order_by(bucket)
    )).all()
    return [
        {
            "day": str(row.day),
            "executions": int(row.executions),
            "completed": int(row.completed or 0),
            "failed": int(row.failed or 0),
        }
        for row in rows
    ]


async def most_executed(session: AsyncSession, filters: AnalyticsFilters,
                        limit: int = 10) -> list[dict]:
    """Top workflows by executions in range, with Phase 9 per-workflow stats."""
    rows = (await session.execute(
        base_workflow_executions_query(filters).with_only_columns(
            WorkflowExecution.workflow_id.label("workflow_id"),
            func.count(WorkflowExecution.id).label("executions"),
        ).group_by(WorkflowExecution.workflow_id)
        .order_by(func.count(WorkflowExecution.id).desc())
        .limit(limit)
    )).all()

    out = []
    for row in rows:
        name = await session.scalar(
            select(Workflow.name).where(Workflow.id == row.workflow_id)
        )
        stats = await phase9_workflow_stats(session, row.workflow_id)
        out.append({
            "workflow_id": str(row.workflow_id) if row.workflow_id else "unknown",
            "name": name or "Unknown workflow",
            "executions": int(row.executions),
            "completed": stats["executions_completed"],
            "failed": stats["executions_failed"],
            "success_rate": stats["success_rate"],
            "avg_duration_seconds": stats["avg_duration_seconds"],
        })
    return out


async def failure_reasons(session: AsyncSession, filters: AnalyticsFilters,
                          limit: int = 5) -> list[dict]:
    """Most common failure reasons (error text, first line, truncated)."""
    base = base_workflow_executions_query(filters).where(
        WorkflowExecution.status == FAILED,
        WorkflowExecution.error.is_not(None),
    )
    # portable truncation: slice in Python from the grouped candidates
    rows = (await session.execute(
        base.with_only_columns(
            WorkflowExecution.error.label("error"),
            func.count(WorkflowExecution.id).label("count"),
        ).group_by(WorkflowExecution.error)
        .order_by(func.count(WorkflowExecution.id).desc())
        .limit(limit * 4)
    )).all()
    grouped: dict[str, int] = {}
    for row in rows:
        reason = (row.error or "unknown").splitlines()[0][:120]
        grouped[reason] = grouped.get(reason, 0) + int(row.count)
    top = sorted(grouped.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"value": reason, "count": count} for reason, count in top]


async def action_frequency(session: AsyncSession, filters: AnalyticsFilters,
                           limit: int = 10) -> list[dict]:
    """Step counts per node type (ACTION/CONDITION/WAIT/TRIGGER) — real step
    rows; per-action-key breakdown is not persisted on steps and is therefore
    not fabricated."""
    step_query = select(
        WorkflowExecutionStep.node_type,
        func.count(WorkflowExecutionStep.id),
    ).join(
        WorkflowExecution, WorkflowExecution.id == WorkflowExecutionStep.execution_id
    )
    if filters.period is not None:
        step_query = step_query.where(
            WorkflowExecution.created_at >= filters.period.start,
            WorkflowExecution.created_at < filters.period.end,
        )
    rows = (await session.execute(
        step_query.group_by(WorkflowExecutionStep.node_type)
    )).all()
    out = [
        {"value": node_type or "UNKNOWN", "count": int(n)}
        for node_type, n in rows
    ]
    return sorted(out, key=lambda item: item["count"], reverse=True)[:limit]
