"""Execution engine — claims executions and runs nodes (§28, §32, §37–§39,
§58, §59, §77).

Claiming uses the codebase's Postgres guarded-UPDATE lease pattern (never
Redis-only, §59): QUEUED + due rows are flipped to RUNNING with
`locked_at/lease_owner` in one statement; a stale-lease sweep recovers crashed
workers. Node outcomes:

    CONTINUE  → proceed to the next node immediately (same cycle)
    WAITING   → persist `next_execution_at`, release the worker (§28)
    RETRY     → transient error: exponential backoff via next_execution_at
    COMPLETED → END reached (or step budget consumed)
    FAILED    → permanent/configuration/permission error (§37)

Every node visit writes a `WorkflowExecutionStep` row (input/output snapshots
hold configuration and safe outputs only — never secrets, §33/§76). Actions
run with the automation causation contextvar set so any events they emit are
chained for loop protection (§40, §41).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.actions.registry import build_action_registry
from app.automation.core.business_hours import shift_to_business_hours
from app.automation.core.exceptions import (
    ActionSkipped,
    AutomationError,
    ConfigurationError,
    PermanentError,
    PermissionError as AutomationPermissionError,
    TransientError,
)
from app.automation.core.schemas import WorkflowDefinition
from app.automation.core.context import WorkflowContext
from app.automation.services import event_dispatcher
from app.automation.services.event_dispatcher import _causation_contextvar
from app.core.logging import get_logger, log_with
from app.models.automation import (
    ExecutionStatus,
    StepStatus,
    WorkflowExecution,
    WorkflowExecutionStep,
    WorkflowVersion,
)

logger = get_logger("qbit.automation.engine")

#: max nodes executed per execution per worker cycle (keeps cycles bounded;
#: executions continue next cycle if they need more steps)
STEPS_PER_CYCLE = 10

_OUTCOMES = ("completed", "running", "waiting", "retry", "failed")


class ExecutionEngine:
    def __init__(self, *, owner: str, settings: Any, audit: Any | None = None) -> None:
        self.owner = owner
        self.settings = settings
        self.actions = build_action_registry()
        self.audit = audit

    # ------------------------------------------------------------------ claim
    async def resume_due_waits(self, session: AsyncSession, *, now: datetime) -> int:
        """WAITING executions whose time has come → QUEUED (§29)."""
        result = await session.execute(
            update(WorkflowExecution)
            .where(
                WorkflowExecution.status == ExecutionStatus.WAITING,
                WorkflowExecution.next_execution_at.is_not(None),
                WorkflowExecution.next_execution_at <= now,
            )
            .values(status=ExecutionStatus.QUEUED, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return result.rowcount or 0

    async def recover_stale_leases(self, session: AsyncSession, *, now: datetime) -> int:
        """Crash recovery (§77): RUNNING rows with expired leases → QUEUED."""
        lease = timedelta(seconds=getattr(self.settings, "QBIT_AUTOMATION_LEASE_SECONDS", 300))
        result = await session.execute(
            update(WorkflowExecution)
            .where(
                WorkflowExecution.status == ExecutionStatus.RUNNING,
                WorkflowExecution.locked_at.is_not(None),
                WorkflowExecution.locked_at < now - lease,
            )
            .values(
                status=ExecutionStatus.QUEUED,
                locked_at=None,
                lease_owner=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return result.rowcount or 0

    async def claim_batch(self, session: AsyncSession, *, now: datetime, batch: int) -> list[uuid.UUID]:
        """Guarded-UPDATE claim (§59): only QUEUED + due rows, atomic flip."""
        candidates = (await session.execute(
            select(WorkflowExecution.id).where(
                WorkflowExecution.status == ExecutionStatus.QUEUED,
                or_(
                    WorkflowExecution.next_execution_at.is_(None),
                    WorkflowExecution.next_execution_at <= now,
                ),
            ).order_by(WorkflowExecution.created_at, WorkflowExecution.id)
            .limit(batch)
        )).scalars().all()
        if not candidates:
            return []
        claimed = (await session.execute(
            update(WorkflowExecution)
            .where(
                WorkflowExecution.id.in_(candidates),
                WorkflowExecution.status == ExecutionStatus.QUEUED,
            )
            .values(
                status=ExecutionStatus.RUNNING,
                locked_at=now,
                lease_owner=self.owner,
                attempts=WorkflowExecution.attempts + 1,
                started_at=func.coalesce(WorkflowExecution.started_at, now),
                updated_at=now,
            )
            .returning(WorkflowExecution.id)
            .execution_options(synchronize_session=False)
        )).scalars().all()
        await session.commit()
        return list(claimed)

    # ----------------------------------------------------------------- process
    async def process_cycle(self, session: AsyncSession, *, batch: int | None = None) -> int:
        """One worker cycle: resume waits → claim → run. Returns actions taken."""
        now = datetime.now(timezone.utc)
        actions = 0
        actions += await self.resume_due_waits(session, now=now)
        actions += await self.recover_stale_leases(session, now=now)
        batch = batch or getattr(self.settings, "QBIT_AUTOMATION_BATCH_SIZE", 10)
        claimed = await self.claim_batch(session, now=now, batch=batch)
        for execution_id in claimed:
            await self.run_execution(session, execution_id, now=now)
            actions += 1
        return actions

    async def run_execution(
        self, session: AsyncSession, execution_id: uuid.UUID, *, now: datetime | None = None,
    ) -> str:
        """Run one execution until WAITING/COMPLETED/FAILED or the per-cycle
        step budget is consumed. Returns the final outcome string."""
        now = now or datetime.now(timezone.utc)
        outcome = "running"
        steps_this_cycle = 0

        while outcome == "running" and steps_this_cycle < STEPS_PER_CYCLE:
            execution = (await session.execute(
                select(WorkflowExecution)
                .where(WorkflowExecution.id == execution_id)
                .execution_options(populate_existing=True)
            )).scalars().first()
            if execution is None:
                return "failed"
            # an operator may cancel while we are mid-flight (§49/§71)
            if execution.status not in (ExecutionStatus.RUNNING, ExecutionStatus.QUEUED):
                return "completed" if execution.status == ExecutionStatus.COMPLETED else outcome

            first_run = False
            total_steps = await session.scalar(
                select(func.count()).select_from(WorkflowExecutionStep)
                .where(WorkflowExecutionStep.execution_id == execution.id)
            )
            first_run = int(total_steps or 0) == 0
            await self._audit_once(session, execution, first_run)

            version = await session.get(WorkflowVersion, execution.workflow_version_id)
            if version is None:
                return await self._fail(session, execution, "Workflow version missing", "CONFIGURATION", now)

            try:
                definition = WorkflowDefinition.model_validate(version.definition)
            except Exception as exc:  # noqa: BLE001
                return await self._fail(session, execution, f"Invalid stored definition: {exc}", "CONFIGURATION", now)

            node_id = execution.current_node_id or definition.trigger_node.id
            node = definition.get_node(node_id)
            if node is None:
                return await self._fail(
                    session, execution,
                    f"Node {node_id!r} not found in version {version.version}",
                    "CONFIGURATION", now,
                )

            # total-step loop guard (§42, §40)
            max_steps = getattr(self.settings, "QBIT_AUTOMATION_MAX_STEPS_PER_EXECUTION", 100)
            if int(total_steps or 0) >= max_steps:
                return await self._fail(
                    session, execution,
                    f"Step limit exceeded ({max_steps}) — possible loop",
                    "PERMANENT", now,
                )

            context = WorkflowContext(session, execution=execution, settings=self.settings, now=now)
            context._current_node_id = node.id  # noqa: SLF001 — engine-owned hint
            await context.preload()  # fresh snapshots for this node visit (§34)
            step = WorkflowExecutionStep(
                execution_id=execution.id,
                node_id=node.id,
                node_type=node.type,
                status=StepStatus.RUNNING,
                attempt=int(execution.attempts or 1),
                input_snapshot={"node_id": node.id, "node_type": node.type},
                started_at=now,
            )
            session.add(step)
            await session.flush()

            next_id: str | None = None
            try:
                if node.type == "TRIGGER":
                    next_id = node.next_node_id
                    step.output_snapshot = {"trigger": "fired"}
                elif node.type == "CONDITION":
                    engine = context.condition_engine()
                    result = engine.evaluate(
                        node.condition.model_dump(by_alias=True, exclude_none=True) if node.condition else None
                    )
                    next_id = node.next_node_id if result else node.next_node_id_no
                    step.output_snapshot = {"result": bool(result),
                                            "branch": "yes" if result else "no"}
                elif node.type == "BRANCH":
                    matched = None
                    for i, branch in enumerate(node.branches or []):
                        if context.condition_engine().evaluate(branch.condition.model_dump(by_alias=True, exclude_none=True)):
                            matched = i
                            next_id = branch.next_node_id
                            break
                    if next_id is None:
                        next_id = node.next_node_id
                    step.output_snapshot = {"matched_branch": matched if matched is not None else "default"}
                elif node.type == "ACTION":
                    action = self.actions.require(node.action)
                    token = _causation_contextvar().set(str(execution.id))
                    try:
                        result = await action.execute(context, node.config or {})
                    finally:
                        _causation_contextvar().reset(token)
                    step.output_snapshot = {"action": node.action, **(result or {})}
                    next_id = node.next_node_id
                elif node.type == "WAIT":
                    wake_at = self._wake_at(node, now)
                    execution.status = ExecutionStatus.WAITING
                    execution.next_execution_at = wake_at
                    execution.current_node_id = node.next_node_id
                    step.status = StepStatus.COMPLETED
                    step.output_snapshot = {"wake_at": wake_at.isoformat(),
                                            "respect_business_hours": bool(node.respect_business_hours)}
                    step.completed_at = datetime.now(timezone.utc)
                    await self._commit_node(session, execution, step)
                    return "waiting"
                elif node.type == "END":
                    execution.status = ExecutionStatus.COMPLETED
                    execution.completed_at = now
                    execution.current_node_id = node.id
                    step.status = StepStatus.COMPLETED
                    step.output_snapshot = {"ended": True}
                    step.completed_at = datetime.now(timezone.utc)
                    await self._commit_node(session, execution, step)
                    await self._audit_end(session, execution, "automation.execution_completed")
                    return "completed"
                else:  # pragma: no cover — schema-validated
                    raise ConfigurationError(f"Unknown node type: {node.type!r}")
            except ActionSkipped as skip:
                step.status = StepStatus.SKIPPED
                step.output_snapshot = {"skipped": True, "reason": skip.reason}
                step.error = skip.reason
                step.completed_at = datetime.now(timezone.utc)
                next_id = node.next_node_id  # skips continue along the path (§27)
            except TransientError as exc:
                return await self._retry_or_fail(session, execution, step, str(exc), now)
            except AutomationPermissionError as exc:
                step.status = StepStatus.FAILED
                step.error = str(exc)
                step.completed_at = datetime.now(timezone.utc)
                await self._commit_node(session, execution, step)
                return await self._fail(session, execution, str(exc), "PERMISSION", now, step=step)
            except (ConfigurationError, PermanentError) as exc:
                reason = getattr(exc, "reason", None)
                detail = str(exc)
                step.status = StepStatus.FAILED
                step.error = detail
                step.completed_at = datetime.now(timezone.utc)
                await self._commit_node(session, execution, step)
                return await self._fail(session, execution, detail, "CONFIGURATION" if isinstance(exc, ConfigurationError) else "PERMANENT", now)

            # ---- persist node completion + advance ---------------------------
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.COMPLETED
                step.completed_at = datetime.now(timezone.utc)

            if next_id is None:
                # fell off the graph without END — honest configuration failure
                await self._commit_node(session, execution, step)
                return await self._fail(
                    session, execution,
                    f"Node {node.id!r} has no next node and is not an END node",
                    "CONFIGURATION", now,
                )

            execution.current_node_id = next_id
            await self._commit_node(session, execution, step)
            steps_this_cycle += 1
            outcome = "running"

        return outcome

    # ------------------------------------------------------------------ pieces
    def _wake_at(self, node, now: datetime) -> datetime:
        duration = node.duration
        seconds = (
            (duration.seconds or 0)
            + (duration.minutes or 0) * 60
            + (duration.hours or 0) * 3600
            + (duration.days or 0) * 86400
        )
        max_wait_hours = getattr(self.settings, "QBIT_AUTOMATION_MAX_WAIT_HOURS", 720)
        seconds = min(seconds, max_wait_hours * 3600)
        if seconds <= 0:
            seconds = 1  # a zero wait still yields to the scheduler
        wake_at = now + timedelta(seconds=seconds)
        if node.respect_business_hours:
            wake_at = shift_to_business_hours(wake_at, node.business_hours)
        return wake_at

    async def _retry_or_fail(
        self, session: AsyncSession, execution: WorkflowExecution,
        step: WorkflowExecutionStep, error: str, now: datetime,
    ) -> str:
        step.status = StepStatus.FAILED
        step.error = error[:2000]
        step.completed_at = datetime.now(timezone.utc)
        max_retries = getattr(self.settings, "QBIT_AUTOMATION_MAX_RETRIES", 3)
        if int(execution.attempts or 0) > max_retries:
            await self._commit_node(session, execution, step)
            return await self._fail(session, execution, error, "TRANSIENT", now)
        base = getattr(self.settings, "QBIT_AUTOMATION_RETRY_BASE_SECONDS", 30)
        top = getattr(self.settings, "QBIT_AUTOMATION_RETRY_MAX_SECONDS", 3600)
        delay = min(base * (2 ** max(0, int(execution.attempts or 1) - 1)), top)
        execution.status = ExecutionStatus.QUEUED
        execution.next_execution_at = now + timedelta(seconds=delay)
        execution.locked_at = None
        execution.lease_owner = None
        execution.error = error[:2000]
        execution.error_class = "TRANSIENT"
        await self._commit_node(session, execution, step)
        log_with(logger, 20, "Execution scheduled for retry", **{
            "execution_id": str(execution.id), "delay_seconds": delay})
        return "retry"

    async def _fail(
        self, session: AsyncSession, execution: WorkflowExecution,
        error: str, error_class: str, now: datetime, *,
        step: WorkflowExecutionStep | None = None,
    ) -> str:
        execution.status = ExecutionStatus.FAILED
        execution.error = (error or "unknown error")[:2000]
        execution.error_class = error_class
        execution.completed_at = now
        execution.locked_at = None
        execution.lease_owner = None
        if step is not None and step.status == StepStatus.RUNNING:
            step.status = StepStatus.FAILED
            step.completed_at = now
        await session.commit()
        await self._audit_end(session, execution, "automation.execution_failed")
        return "failed"

    async def _commit_node(
        self, session: AsyncSession, execution: WorkflowExecution,
        step: WorkflowExecutionStep,
    ) -> None:
        execution.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(execution)

    async def _audit_once(self, session: AsyncSession, execution: WorkflowExecution, first_run: bool) -> None:
        if not self.audit or not first_run:
            return
        await self.audit.log(
            session,
            action="automation.execution_started",
            resource_type="workflow_execution",
            resource_id=str(execution.id),
            metadata={"workflow_id": str(execution.workflow_id)},
            commit=False,
        )

    async def _audit_end(self, session: AsyncSession, execution: WorkflowExecution, action: str) -> None:
        if not self.audit:
            return
        await self.audit.log(
            session,
            action=action,
            resource_type="workflow_execution",
            resource_id=str(execution.id),
            metadata={
                "workflow_id": str(execution.workflow_id),
                "status": execution.status,
                "error_class": execution.error_class,
            },
            commit=False,
        )
        await session.commit()
