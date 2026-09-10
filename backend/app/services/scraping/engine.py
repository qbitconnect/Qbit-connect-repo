"""JobEngine — job lifecycle authority (brief §11, §12, §19; doc 09 §2).

API-side operations only; execution lives in the runner (worker). Every state
transition is validated against the legal-transition map, evented, and — where
operators act — audited by the callers.

Control plane (pause/cancel): the API writes `stop_requested` in the DB (the
durable source of truth) AND the queue control key (fast path). The running
job observes it cooperatively at safe points. QUEUED jobs can be cancelled
directly; PAUSED jobs move to CANCELLED immediately.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger, log_with
from app.models.scrape import (
    LEGAL_TRANSITIONS,
    JobStatus,
    JobTrigger,
    ScrapeJob,
    ScrapeJobEvent,
)
from app.scrapers.core.base import ScraperActor
from app.scrapers.core.exceptions import ScraperError
from app.services.audit import AuditService
from app.services.scraping.queue import QueueBackend

logger = get_logger("qbit.scrapers.engine")

#: Advanced job-config keys accepted from the API (brief §32, §34 "Advanced").
#: Everything else is rejected — the job config is NOT an arbitrary bag.
CONFIG_LIMITS: dict[str, tuple[type, float, float]] = {
    "max_runtime_seconds": (int, 30, 86_400),
    "max_pages": (int, 1, 100_000),
    "max_records": (int, 1, 1_000_000),
    "request_timeout": (int, 1, 300),
    "requests_per_second": (float, 0.1, 50.0),
    "concurrency": (int, 1, 16),
    "max_retries": (int, 0, 10),
    "respect_robots": (bool, 0, 1),
    "dedup_policy": (str, 0, 1),  # "auto" | "strict"
}


def sanitize_job_config(cfg: dict | None) -> dict:
    """Whitelist + range-check advanced job config (security: a job must not
    disable SSRF policy or request absurd resources)."""
    if not cfg:
        return {}
    if not isinstance(cfg, dict):
        raise ValidationError("config must be an object")
    unknown = sorted(set(cfg) - set(CONFIG_LIMITS))
    if unknown:
        raise ValidationError(f"Unknown config keys: {', '.join(unknown)}")
    clean: dict = {}
    for key, value in cfg.items():
        expected, low, high = CONFIG_LIMITS[key]
        if expected is bool:
            if not isinstance(value, bool):
                raise ValidationError(f"config.{key} must be a boolean")
            clean[key] = value
            continue
        if expected is str:
            if key == "dedup_policy" and value not in ("auto", "strict"):
                raise ValidationError("config.dedup_policy must be 'auto' or 'strict'")
            clean[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"config.{key} must be a number")
        value = float(value)
        if not low <= value <= high:
            raise ValidationError(f"config.{key} must be between {low} and {high}")
        clean[key] = int(value) if expected is int else value
    return clean


class JobEngine:
    def __init__(self, session: AsyncSession, queue: QueueBackend, audit: AuditService | None = None) -> None:
        self.session = session
        self.queue = queue
        self.audit = audit

    # ------------------------------------------------------------------ create
    async def create_job(
        self,
        *,
        actor: ScraperActor,
        validated_input: dict,
        config: dict | None,
        created_by: uuid.UUID | None,
        max_attempts: int = 3,
        name: str | None = None,
        trigger: str | None = None,
        task_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> ScrapeJob:
        from app.core.config import Settings

        config = config or {}
        now = datetime.now(timezone.utc)
        job = ScrapeJob(
            actor_id=actor.id,
            actor_version=actor.version,
            status=JobStatus.QUEUED,
            input=validated_input,
            config=config,
            max_attempts=max(1, max_attempts),
            created_by=created_by,
            name=name,
            trigger=trigger or JobTrigger.MANUAL.value,
            task_id=task_id,
            organization_id=organization_id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(job)
        await self.session.flush()
        await self._event(
            job.id, "JOB_CREATED", f"Job created for actor {actor.id}",
            {"actor": actor.id, "version": actor.version},
        )
        await _emit_webhook(
            self.session, event="RUN_CREATED", job_id=job.id,
            actor_id=actor.id, payload={"actor": actor.id, "version": actor.version},
        )
        await self.session.commit()
        await self.session.refresh(job)
        # enqueue AFTER the DB row is durable (DB-first, doc 09 §1)
        await self.queue.enqueue(str(job.id))
        log_with(logger, 20, "Scrape job created", job_id=str(job.id), actor=actor.id)
        return job

    async def retry_job(self, job: ScrapeJob, *, actor_id: str) -> ScrapeJob:
        """Operator retry of a FAILED job (doc 09: FAILED → QUEUED)."""
        if job.status != JobStatus.FAILED:
            raise ConflictError("Only failed jobs can be retried")
        job.status = JobStatus.QUEUED
        job.stop_requested = "NONE"
        job.error = None
        job.error_code = None
        job.attempt = 0
        job.progress = 0.0
        job.updated_at = datetime.now(timezone.utc)
        await self._event(job.id, "JOB_CREATED", "Job re-queued by operator", {})
        await self.session.commit()
        await self.session.refresh(job)
        await self.queue.enqueue(str(job.id))
        return job

    # ------------------------------------------------------------- transitions
    async def transition(
        self,
        job: ScrapeJob,
        to_status: JobStatus,
        *,
        event_type: str,
        message: str | None = None,
        metadata: dict | None = None,
        set_timestamps: bool = True,
        commit: bool = True,
    ) -> ScrapeJob:
        current = JobStatus(job.status)
        if to_status not in LEGAL_TRANSITIONS.get(current, set()):
            raise ConflictError(f"Illegal job transition {current} → {to_status}")
        job.status = to_status.value
        now = datetime.now(timezone.utc)
        job.updated_at = now
        if set_timestamps:
            if to_status is JobStatus.RUNNING and job.started_at is None:
                job.started_at = now
            if to_status is JobStatus.PAUSED:
                job.paused_at = now
            if to_status is JobStatus.CANCELLED:
                job.cancelled_at = now
            if to_status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                job.completed_at = now
        await self._event(job.id, event_type, message, metadata or {})
        if commit:
            await self.session.commit()
            await self.session.refresh(job)
        return job

    # ------------------------------------------------------------- controls
    async def pause(self, job: ScrapeJob) -> ScrapeJob:
        if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            raise ConflictError(f"Job in status {job.status} cannot be paused")
        if job.status == JobStatus.QUEUED:
            # Nobody picked it up yet — flip straight to PAUSED.
            job = await self.transition(
                job, JobStatus.PAUSED, event_type="JOB_PAUSED", message="Paused before start"
            )
            await self.queue.clear_control(str(job.id))
            return job
        job.stop_requested = "PAUSE"
        job.updated_at = datetime.now(timezone.utc)
        await self.session.commit()
        await self.queue.set_control(str(job.id), "PAUSE")
        await self._event(job.id, "JOB_PAUSED", "Pause requested", {})
        await self.session.commit()
        return job

    async def resume(self, job: ScrapeJob) -> ScrapeJob:
        """Resume a PAUSED job: clear controls + re-enqueue.

        The status transition PAUSED → RUNNING happens when a worker actually
        re-claims the job (JobRunner.claim), so the status always reflects a
        worker reality, never an intention (doc 09 §2).
        """
        if job.status != JobStatus.PAUSED:
            raise ConflictError("Only paused jobs can be resumed")
        job.stop_requested = "NONE"
        job.updated_at = datetime.now(timezone.utc)
        await self._event(job.id, "JOB_RESUMED", "Resumed by operator", {})
        await self.session.commit()
        await self.session.refresh(job)
        await self.queue.clear_control(str(job.id))
        await self.queue.enqueue(str(job.id))
        return job

    async def cancel(self, job: ScrapeJob) -> ScrapeJob:
        if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
            raise ConflictError(f"Job already terminal ({job.status})")
        if job.status == JobStatus.FAILED:
            raise ConflictError("Failed jobs cannot be cancelled")
        if job.status == JobStatus.QUEUED:
            job = await self.transition(
                job, JobStatus.CANCELLED, event_type="JOB_CANCELLED", message="Cancelled before start"
            )
            await self.queue.clear_control(str(job.id))
            return job
        if job.status == JobStatus.PAUSED:
            job = await self.transition(
                job, JobStatus.CANCELLED, event_type="JOB_CANCELLED", message="Cancelled while paused"
            )
            await self.queue.clear_control(str(job.id))
            return job
        job.stop_requested = "CANCEL"
        job.updated_at = datetime.now(timezone.utc)
        await self.session.commit()
        await self.queue.set_control(str(job.id), "CANCEL")
        await self._event(job.id, "JOB_CANCELLED", "Cancellation requested", {})
        await self.session.commit()
        return job

    # --------------------------------------------------------------- queries
    async def get_job(self, job_id: uuid.UUID) -> ScrapeJob:
        job = await self.session.get(ScrapeJob, job_id)
        if job is None:
            raise NotFoundError("Scrape job not found")
        return job

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        actor_id: str | None = None,
        search: str | None = None,
        created_by: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[ScrapeJob], int]:
        query = select(ScrapeJob)
        if status:
            try:
                query = query.where(ScrapeJob.status == JobStatus(status.upper()).value)
            except ValueError as exc:
                raise ValidationError(f"Unknown status filter: {status}") from exc
        if actor_id:
            query = query.where(ScrapeJob.actor_id == actor_id)
        if created_by is not None:
            query = query.where(ScrapeJob.created_by == created_by)
        if organization_id is not None:
            # Tenant scope enforced in SQL (NULL org rows = legacy, visible)
            query = query.where(
                or_(ScrapeJob.organization_id.is_(None),
                    ScrapeJob.organization_id == organization_id)
            )
        if search:
            like = f"%{search.strip()}%"
            query = query.where(
                or_(
                    ScrapeJob.actor_id.ilike(like),
                    ScrapeJob.id.cast(String).ilike(like),
                    ScrapeJob.error.ilike(like) if search else None,
                )
            )
        total = await self.session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await self.session.execute(
            query.order_by(ScrapeJob.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def jobs_for_recovery(self, *, lease_ttl_seconds: int) -> list[ScrapeJob]:
        """RUNNING jobs with an expired lease (worker crash)."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=lease_ttl_seconds)
        rows = await session_scalars(
            self.session,
            select(ScrapeJob).where(
                ScrapeJob.status == JobStatus.RUNNING,
                or_(ScrapeJob.leased_at.is_(None), ScrapeJob.leased_at < cutoff),
            ),
        )
        return list(rows)

    async def recover_stalled(
        self, *, lease_ttl_seconds: int, stale_queued_seconds: int = 600
    ) -> int:
        """Crash/broker-loss recovery sweep (brief §14, §18).

        - RUNNING with expired lease → back to QUEUED (resumes from its last
          checkpoint on next claim; resumed_count incremented for observability).
        - QUEUED rows older than `stale_queued_seconds` never claimed → the
          enqueue may have been lost (broker outage); re-enqueue.

        Returns the number of jobs re-enqueued.
        """
        now = datetime.now(timezone.utc)
        requeued = 0
        crashed = await self.jobs_for_recovery(lease_ttl_seconds=lease_ttl_seconds)
        for job in crashed:
            job.status = JobStatus.QUEUED
            job.resumed_count = (job.resumed_count or 0) + 1
            job.lease_owner = None
            job.leased_at = None
            job.stop_requested = "NONE"
            job.updated_at = now
            await self._event(
                job.id, "JOB_RESUMED_FROM_CRASH",
                "Worker lease expired; job re-queued from checkpoint",
                {"resumed_count": job.resumed_count},
            )
            requeued += 1
        await self.session.commit()

        cutoff = now - timedelta(seconds=stale_queued_seconds)
        stale = await session_scalars(
            self.session,
            select(ScrapeJob).where(
                ScrapeJob.status == JobStatus.QUEUED,
                ScrapeJob.updated_at < cutoff,
                ScrapeJob.leased_at.is_(None),
            ),
        )
        for job in stale:
            job.updated_at = now
            await self._event(
                job.id, "JOB_REQUEUED",
                "Stale QUEUED job re-enqueued (possible broker loss)", {},
            )
            requeued += 1
        await self.session.commit()

        # Re-enqueue AFTER the rows are durable (DB-first, doc 09 §1)
        for job in [*crashed, *stale]:
            await self.queue.enqueue(str(job.id))
        return requeued

    # -------------------------------------------------------------- internals
    async def _event(self, job_id, event_type, message, metadata) -> None:
        self.session.add(
            ScrapeJobEvent(
                job_id=job_id,
                event_type=event_type,
                message=(message or "")[:1000] or None,
                metadata_json=metadata or {},
            )
        )


# --- small helpers ------------------------------------------------------------
async def session_scalars(session: AsyncSession, stmt):
    return (await session.scalars(stmt)).all()


def job_error_from_exception(exc: Exception) -> tuple[str, str]:
    """Map an exception → (error_code, message) for job rows (brief §40)."""
    if isinstance(exc, ScraperError):
        return exc.code, exc.message[:2000]
    return "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"[:2000]


async def _emit_webhook(
    session: AsyncSession,
    *,
    event: str,
    job_id: uuid.UUID,
    actor_id: str,
    payload: dict,
) -> None:
    """Best-effort run-webhook emission (spec §22) — never breaks the caller."""
    try:
        from app.services.scraping.run_webhooks import RunWebhookService

        await RunWebhookService(session).emit(
            event=event, job_id=job_id, actor_id=actor_id, payload=payload
        )
    except Exception:  # noqa: BLE001 — webhooks must never break runs
        pass
