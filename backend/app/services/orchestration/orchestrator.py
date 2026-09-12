"""QBIT CONNECT — Scraping Orchestrator (Brief §7, §9, §10, §20, §26).

Main orchestration coordinator linking TaskInterpreter, ToolRegistry,
ExecutionPlanner, CompletionEngine, Validation, Diagnostics, and the
underlying JobEngine / ActorRunner without bypassing or replacing existing systems.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger, log_with
from app.models.scrape import JobStatus, ScrapeJob
from app.services.orchestration.completion import CompletionEngine, ProgressSnapshot
from app.services.orchestration.diagnostics import DiagnosticsEngine, StructuredDiagnostics
from app.services.orchestration.interpreter import InterpretedTask, TaskInterpreter
from app.services.orchestration.planner import ExecutionPlan, ExecutionPlanner
from app.services.orchestration.tool_registry import ToolRegistry
from app.services.orchestration.validation import RecordValidator, ValidationResult
from app.services.scraping.engine import JobEngine
from app.services.scraping.queue import QueueBackend
from app.services.scraping.registry import ActorRegistry

logger = get_logger("qbit.orchestration")


class ScrapingOrchestrator:
    def __init__(
        self,
        actor_registry: ActorRegistry,
        queue: QueueBackend | None = None,
    ) -> None:
        self.actor_registry = actor_registry
        self.queue = queue
        self.tool_registry = ToolRegistry(actor_registry)
        self.interpreter = TaskInterpreter()
        self.planner = ExecutionPlanner(self.tool_registry)

    def interpret(
        self,
        query: str,
        *,
        forced_source: str | None = None,
        forced_source_lock: bool | None = None,
        target_count: int | None = None,
        **kwargs,
    ) -> InterpretedTask:
        src = forced_source or kwargs.get("explicit_source")
        return self.interpreter.interpret(
            query,
            forced_source=src,
            forced_source_lock=forced_source_lock,
            target_count=target_count,
        )

    def create_plan(self, interpreted: InterpretedTask) -> ExecutionPlan:
        return self.planner.create_plan(interpreted)

    def plan_task(
        self,
        query: str,
        *,
        source: str | None = None,
        source_lock: bool | None = None,
        target_count: int | None = None,
    ) -> ExecutionPlan:
        """Interprets instruction and generates a visible, deterministic execution plan."""
        interpreted = self.interpret(
            query,
            forced_source=source,
            forced_source_lock=source_lock,
            target_count=target_count,
        )
        return self.create_plan(interpreted)

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> ScrapeJob:
        """Dispatches an ExecutionPlan through the existing JobEngine into real background execution."""
        if self.queue is None:
            raise ConflictError("Queue backend not initialized on orchestrator")

        tool_defn = self.tool_registry.get_tool(plan.primary_tool)
        if not tool_defn:
            raise NotFoundError(f"Scraper tool '{plan.primary_tool}' not registered")

        actor = self.tool_registry.get_actor_instance(plan.primary_tool)

        # Validate input with actor's native validator
        report = actor.validate_input(plan.input_payload)
        if not report.valid:
            raise ValidationError(
                f"Generated input failed validation for {plan.primary_tool}: {report.errors}"
            )

        job_config = {
            "max_records": plan.target_count,
            "max_runtime_seconds": plan.max_runtime_seconds,
            "max_pages": plan.estimated_pages,
        }

        engine = JobEngine(session, self.queue)
        job = await engine.create_job(
            actor=actor,
            validated_input=report.normalized_input,
            config=job_config,
            created_by=user_id,
            name=f"[Agent] {plan.user_query[:100]}",
            trigger="TASK",
            organization_id=organization_id,
        )

        log_with(
            logger,
            20,
            "Orchestrator dispatched job",
            job_id=str(job.id),
            actor_id=plan.primary_tool,
            source_locked=plan.source_locked,
            target=plan.target_count,
        )
        return job

    async def evaluate_job_progress(
        self,
        job: ScrapeJob,
        *,
        target_override: int | None = None,
    ) -> ProgressSnapshot:
        """Evaluates factual database counters into a strict deterministic completion snapshot."""
        target = target_override or (job.config or {}).get("max_records") or 100
        collected = getattr(job, "records_found", 0) or 0
        unique = getattr(job, "records_saved", 0) or 0
        duplicates = getattr(job, "records_updated", 0) or getattr(job, "records_duplicate", 0) or 0
        invalid = getattr(job, "records_failed", 0) or 0
        pages = getattr(job, "pages_crawled", 0) or 0

        has_fatal = job.status == JobStatus.FAILED.value
        stop_req = getattr(job, "stop_requested", "NONE") != "NONE"

        return CompletionEngine.evaluate(
            target=target,
            collected=collected,
            unique=unique,
            duplicates=duplicates,
            invalid=invalid,
            pages_fetched=pages,
            job_status=job.status,
            has_fatal_error=has_fatal,
            stop_requested=stop_req,
        )

    def diagnose_job_failure(
        self,
        job: ScrapeJob,
    ) -> StructuredDiagnostics:
        """Generates rich structured diagnostics for a failed or degraded job."""
        tool = self.tool_registry.get_tool(job.actor_id)
        name = tool.name if tool else job.actor_id

        err_msg = job.error or "Unknown failure"
        code = job.error_code or "SCRAPER_FAILED"

        return DiagnosticsEngine.diagnose_error(
            actor_id=job.actor_id,
            actor_name=name,
            error=err_msg,
            error_code=code,
            attempts=job.attempt or 1,
            stage="runner_execution",
        )
