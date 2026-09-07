"""Automation worker loop (§58) — one isolated loop next to the campaign and
inbox loops in `app/worker.py` (isolation rule §15).

Responsibilities per cycle:
1. SCHEDULED triggers (§11, §57): compute due slots for ACTIVE scheduled
   workflows and dispatch internal `schedule.tick` events (deterministic
   per-slot event ids — restart-safe, never double-fires a slot).
2. `ExecutionEngine.process_cycle`: resume due WAITING executions → claim
   QUEUED batch (guarded UPDATE) → run nodes → persist results.
3. Crash recovery via the engine's stale-lease sweep (§77).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.services.analytics import global_counters  # noqa: F401 (monitor reuse)
from app.automation.services.event_dispatcher import dispatch_event
from app.automation.services.execution_engine import ExecutionEngine
from app.automation.triggers.definitions import build_trigger_registry
from app.automation.triggers.schedule import current_slot, tick_event_id
from app.core.logging import get_logger, log_with
from app.models.automation import (
    Workflow,
    WorkflowStatus,
    WorkflowVersion,
    WorkflowVersionStatus,
)

logger = get_logger("qbit.automation.worker")


class AutomationWorker:
    def __init__(self, settings, audit=None, *, owner: str | None = None) -> None:
        self.settings = settings
        self.owner = owner or f"automation-{uuid.uuid4().hex[:8]}"
        self.engine = ExecutionEngine(owner=self.owner, settings=settings, audit=audit)

    async def process_cycle(self, session: AsyncSession, *, batch: int | None = None) -> int:
        now = datetime.now(timezone.utc)
        actions = await self._fire_scheduled(session, now=now)
        actions += await self.engine.process_cycle(session, batch=batch)
        return actions

    # -------------------------------------------------------------- schedules
    async def _fire_scheduled(self, session: AsyncSession, *, now: datetime) -> int:
        """SCHEDULED trigger support (§11) using the worker as the ONLY
        scheduler (§57 — no second scheduler system)."""
        registry = build_trigger_registry()
        trigger = registry.get("SCHEDULED")
        if trigger is None:
            return 0

        rows = (await session.execute(
            select(Workflow, WorkflowVersion)
            .where(
                Workflow.status == WorkflowStatus.ACTIVE,
                Workflow.trigger_type == "SCHEDULED",
                WorkflowVersion.status == WorkflowVersionStatus.PUBLISHED,
            )
        )).all()

        fired = 0
        seen: dict[uuid.UUID, int] = {}
        for workflow, version in rows:
            if seen.get(workflow.id, -1) >= version.version:
                continue
            seen[workflow.id] = max(seen.get(workflow.id, 0), version.version)

            definition = version.definition or {}
            trigger_node = next(
                (n for n in definition.get("nodes") or [] if n.get("type") == "TRIGGER"),
                None,
            )
            config = (trigger_node or {}).get("trigger_config") or {}
            slot = current_slot(config, now=now)
            if slot is None:
                continue
            published_at = version.published_at or workflow.published_at
            if published_at is not None:
                published_at = _aware(published_at)
                if slot < published_at:
                    continue  # never backfill slots from before publication

            event_id = tick_event_id(str(workflow.id), str(version.id), slot)
            created = await dispatch_event(
                session,
                event_type="schedule.tick",
                entity_type="schedule",
                entity_id=None,
                payload={"workflow_id": str(workflow.id), "slot": slot.isoformat()},
                event_id=event_id,
                settings=self.settings,
            )
            fired += len(created)
            if created:
                await session.commit()
                log_with(logger, 20, "Scheduled workflow tick dispatched", **{
                    "workflow_id": str(workflow.id), "slot": slot.isoformat()})
        return fired


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
