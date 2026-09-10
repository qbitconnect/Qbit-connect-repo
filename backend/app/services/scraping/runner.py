"""JobRunner — worker-side execution of one scrape job (brief §11, §15–§19).

Responsibilities:
- atomically claim QUEUED (or PAUSED-for-resume) jobs: status → RUNNING + lease
- build the ScraperContext (limits, http policy, checkpoint, progress, events,
  pipeline, async control reader) from job config + Settings
- consume the actor's async generator and stream items through ResultPipeline
- enforce resource limits: wall-clock deadline → PAUSE at checkpoint;
  max_records/max_pages → clean COMPLETED (partial) with LIMIT_REACHED event
- map exceptions → job outcomes (retryable → re-enqueue with backoff;
  non-retryable → FAILED; pause/cancel → checkpoint + suspend/finalize)
- cooperative controls: the context polls the queue control flag via
  ControlReader (async); the DB `stop_requested` column stays the durable truth
- crash recovery is lease-based: expired lease → job re-queued by the recovery
  sweep and resumes from its checkpoint (resumed_count++)

The FastAPI process NEVER runs actors (brief §11); only this runner (invoked by
`python -m app.worker` or tests) executes them.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.core.config import Settings
from app.core.logging import get_logger, log_with
from app.models.actor_platform import DatasetStatus
from app.models.scrape import JobStatus, RunOutcome, ScrapeJob
from app.scrapers.core.context import JobLimits, ScraperContext
from app.scrapers.core.exceptions import (
    ScraperCancelledError,
    ScraperError,
    ScraperLimitReachedError,
    ScraperPausedError,
    ScraperTimeoutError,
)
from app.scrapers.core.http import HttpPolicy
from app.scrapers.core.netguard import UrlPolicy
from app.services.scraping.checkpoints import CheckpointManager
from app.services.scraping.dedup import Deduplicator
from app.services.scraping.engine import job_error_from_exception
from app.services.scraping.events import EventReporter
from app.services.scraping.pipeline import ResultPipeline
from app.services.scraping.progress import ProgressReporter
from app.services.scraping.queue import QueueBackend
from app.services.scraping.result_files import JobResultFiles

logger = get_logger("qbit.scrapers.runner")

random = __import__("random")


class JobRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory,
        storage_root,
        queue: QueueBackend,
        owner: str,
        lease_seconds: int | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.storage_root = storage_root
        self.queue = queue
        self.owner = owner
        self.lease_seconds = lease_seconds or settings.QBIT_WORKER_LEASE_SECONDS

    # ------------------------------------------------------------------ claim
    async def claim(self, job_id: uuid.UUID) -> ScrapeJob | None:
        """Atomically claim a startable job: QUEUED/PAUSED → RUNNING + lease.

        Phase 12 (audit H1): a SINGLE guarded UPDATE with the status predicate
        in the WHERE clause — the row is claimed iff the UPDATE reports
        rowcount 1. Two workers racing on the same QUEUED row can never both
        win (SELECT-then-flush allowed a double-claim window). PAUSED jobs are
        claimed only for operator resumes (stop_requested cleared by
        engine.resume()).
        """
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(seconds=self._deadline_for(None))
        async with self.session_factory() as session:
            result = await session.execute(
                update(ScrapeJob)
                .where(
                    ScrapeJob.id == job_id,
                    ScrapeJob.status.in_([JobStatus.QUEUED, JobStatus.PAUSED]),
                )
                .values(
                    status=JobStatus.RUNNING,
                    started_at=func.coalesce(ScrapeJob.started_at, now),
                    paused_at=None,
                    leased_at=now,
                    lease_owner=self.owner,
                    attempt=func.coalesce(ScrapeJob.attempt, 0) + 1,
                    deadline_at=deadline,
                    stop_requested="NONE",
                    outcome=None,  # a resumed run re-earns its outcome honestly
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                # lost the race (another worker claimed it) or not claimable
                return None
            job = await session.get(ScrapeJob, job_id)
            session.add(
                _event_row(
                    job.id,
                    "JOB_STARTED",
                    f"Claimed by {self.owner} (attempt {job.attempt})",
                )
            )
            from app.services.scraping.engine import _emit_webhook

            await _emit_webhook(
                session, event="RUN_STARTED", job_id=job.id,
                actor_id=job.actor_id,
                payload={"actor": job.actor_id, "attempt": job.attempt},
            )
            await session.commit()
            await self.queue.renew_lease(str(job.id), self.owner, self.lease_seconds)
            return job

    # ---------------------------------------------------------------- execute
    async def execute(self, job_id: uuid.UUID, actor) -> str:
        """Run a claimed job to a terminal/controlled state. Returns final status."""
        job = await self.claim(job_id)
        if job is None:
            return "SKIP"

        checkpoint = CheckpointManager(
            self.session_factory,
            job.id,
            min_interval=self.settings.QBIT_SCRAPER_CHECKPOINT_INTERVAL_SECONDS,
        )
        await checkpoint.load()
        if checkpoint.records_processed or checkpoint.data:
            async with self.session_factory() as session:
                session.add(
                    _event_row(
                        job.id, "RESUMED_FROM_CHECKPOINT", "Loaded checkpoint", checkpoint.data
                    )
                )
                await session.commit()

        limits = self._limits_for(job)
        progress = ProgressReporter(
            self._progress_flusher(job.id),
            total=self._total_estimate(job),
            flush_interval=3.0,
            initial={
                "records_found": job.records_found or 0,
                "records_saved": job.records_saved or 0,
                "records_updated": getattr(job, "records_updated", 0) or 0,
                "records_duplicate": job.records_duplicate or 0,
                "records_failed": job.records_failed or 0,
            },
        )
        events = EventReporter(
            self.session_factory,
            job.id,
            progress,
            page_event_every=self.settings.QBIT_SCRAPER_PAGE_EVENT_EVERY,
        )
        result_files = JobResultFiles(self.storage_root, job.actor_id, str(job.id))
        dedup = Deduplicator(policy=(job.config or {}).get("dedup_policy", "auto"))

        async with self.session_factory() as pipeline_session:
            dataset_id: uuid.UUID | None = None
            try:
                from app.services.scraping.datasets import DatasetService

                dataset = await DatasetService(pipeline_session).create_for_job(
                    job_id=job.id,
                    actor_id=job.actor_id,
                    actor_version=job.actor_version,
                    name=job.name,
                    organization_id=getattr(job, "organization_id", None),
                    created_by=job.created_by,
                )
                dataset_id = dataset.id  # capture BEFORE commit expires the row
                await pipeline_session.commit()
            except Exception:  # noqa: BLE001 — dataset is an addition, not a gate
                dataset_id = None
                await pipeline_session.rollback()
            pipeline = ResultPipeline(
                pipeline_session,
                actor_id=job.actor_id,
                actor_version=job.actor_version,
                job_id=job.id,
                result_files=result_files,
                dedup=dedup,
                created_by=job.created_by,
                batch_size=self.settings.QBIT_SCRAPER_BATCH_SIZE,
                dataset_id=dataset_id,
            )
            ctx = ScraperContext(
                job_id=job.id,
                actor_id=job.actor_id,
                actor_version=job.actor_version,
                attempt=job.attempt,
                input=job.input,
                config=job.config,
                limits=limits,
                http_policy=HttpPolicy.from_settings(
                    self.settings, **self._http_overrides(job)
                ),
                url_policy=UrlPolicy(
                    allowed_ports=self.settings.scraper_allowed_ports(),
                    allow_private_targets=self.settings.QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS,
                ),
                progress=progress,
                events=events,
                checkpoint=checkpoint,
                control_reader=ControlReader(self.queue, job.id, self.session_factory),
                settings=self.settings,
            )
            pipeline.ctx = ctx  # counters + stop checks (§20, §38)
            heartbeat = asyncio.create_task(self._heartbeat(job.id))
            try:
                await actor.initialize(ctx)
                async for raw_item in actor.run(ctx):
                    await pipeline.add(raw_item)
                    await ctx.check_stopped()          # cooperative stop (§19)
                    ctx.check_deadline()               # wall-clock budget (§32)
                    ctx.check_record_limit(progress.records_found)
                    await progress.maybe_flush()
                await pipeline.finish()
                await progress.flush()
                await events.flush()
                await checkpoint.clear()
                await ctx.close()
                await self._finish_success(job)
                await self._finalize_dataset(dataset_id, DatasetStatus.READY, "clean")
                return JobStatus.COMPLETED
            except ScraperCancelledError:
                await self._safe_finalize(pipeline, progress, events, ctx, checkpoint)
                await self._finish_cancelled(job)
                await self._finalize_dataset(dataset_id, DatasetStatus.READY, "partial")
                return JobStatus.CANCELLED
            except ScraperPausedError:
                await self._safe_finalize(pipeline, progress, events, ctx, checkpoint)
                await self._finish_paused(job)
                return JobStatus.PAUSED
            except ScraperTimeoutError as exc:
                # job wall-clock budget exhausted: pause at checkpoint (§18, §32)
                await self._safe_finalize(pipeline, progress, events, ctx, checkpoint)
                await ctx.close()
                await self._finish_paused(
                    job, message=f"Paused: {exc.message}", event_code=exc.code,
                    outcome=RunOutcome.TIMED_OUT.value,
                )
                return JobStatus.PAUSED
            except ScraperLimitReachedError as exc:
                # configured max_records/max_pages reached: clean partial stop
                await self._safe_finalize(pipeline, progress, events, ctx, checkpoint)
                await ctx.close()
                await self._finish_success(
                    job, extra_event=("LIMIT_REACHED", exc.message),
                    outcome=RunOutcome.PARTIAL.value,
                )
                await self._finalize_dataset(dataset_id, DatasetStatus.READY, "partial")
                return JobStatus.COMPLETED
            except ScraperError as exc:
                await progress.flush()
                await events.flush()
                await ctx.save_checkpoint(force=True)
                await ctx.close()
                if exc.retryable:
                    return await self._schedule_retry(job, exc)
                await self._finish_failed(job, exc)
                await self._finalize_dataset(dataset_id, DatasetStatus.FAILED, "clean")
                return JobStatus.FAILED
            except asyncio.CancelledError:
                # graceful worker shutdown: checkpoint and pause (not fail)
                await progress.flush()
                await events.flush()
                await ctx.save_checkpoint(force=True)
                await ctx.close()
                await self._finish_paused(
                    job, message="Worker shutdown; job paused at checkpoint"
                )
                return JobStatus.PAUSED
            except Exception as exc:  # noqa: BLE001 - worker isolation (§15)
                log_with(
                    logger, 40, "Actor crashed",
                    job_id=str(job.id), actor=job.actor_id, error=repr(exc),
                )
                await progress.flush()
                await events.flush()
                await ctx.close()
                await self._finalize_dataset(dataset_id, DatasetStatus.FAILED, "clean")
                return await self._schedule_retry(job, exc)
            finally:
                heartbeat.cancel()
                await self.queue.release_lease(str(job.id))

    # -------------------------------------------------------------- outcomes
    async def _safe_finalize(
        self, pipeline, progress, events, ctx, checkpoint
    ) -> None:
        """Flush everything at a controlled stop (pause/cancel/limit)."""
        try:
            await pipeline.process_batch(stop_checks=False)
            pipeline.files.close()  # sync JSONL flush
        except Exception:  # noqa: BLE001
            logger.exception("Pipeline finalize failed", extra={"extra_fields": {}})
        await progress.flush()
        await events.flush()
        await ctx.save_checkpoint(force=True)
        await ctx.close()

    async def _finish_success(
        self, job: ScrapeJob, *, extra_event=None, outcome: str | None = None
    ) -> None:
        async with self.session_factory() as session:
            fresh = await session.get(ScrapeJob, job.id)
            if fresh.status not in (JobStatus.RUNNING,):
                return  # operator moved it meanwhile (e.g. CANCELLED)
            fresh.status = JobStatus.COMPLETED
            fresh.completed_at = datetime.now(timezone.utc)
            fresh.progress = 100.0
            fresh.stop_requested = "NONE"
            fresh.stage = "completed"
            fresh.outcome = outcome or RunOutcome.SUCCEEDED.value
            session.add(
                _event_row(
                    job.id, "JOB_COMPLETED",
                    f"Completed: found {fresh.records_found}, saved {fresh.records_saved}, "
                    f"updated {getattr(fresh, 'records_updated', 0)}, "
                    f"duplicates {fresh.records_duplicate}, failed {fresh.records_failed}",
                    {
                        "records_found": fresh.records_found,
                        "records_saved": fresh.records_saved,
                        "records_updated": getattr(fresh, "records_updated", 0),
                    },
                )
            )
            if extra_event:
                session.add(_event_row(job.id, extra_event[0], extra_event[1], {}))
            from app.services.scraping.engine import _emit_webhook

            await _emit_webhook(
                session,
                event="RUN_SUCCEEDED",
                job_id=job.id,
                actor_id=fresh.actor_id,
                payload={
                    "outcome": fresh.outcome,
                    "records_found": fresh.records_found,
                    "records_saved": fresh.records_saved,
                    "records_updated": getattr(fresh, "records_updated", 0),
                    "records_duplicate": fresh.records_duplicate,
                    "records_failed": fresh.records_failed,
                },
            )
            # Phase 9 §55: SCRAPE_JOB_COMPLETED event (best-effort intake)
            await _emit_automation(
                session, job_id=str(job.id),
                payload={
                    "records_found": fresh.records_found,
                    "records_saved": fresh.records_saved,
                    "records_updated": getattr(fresh, "records_updated", 0),
                },
            )
            # Phase 11 §25/§27: background tasks carry tenant context — the
            # notification is org-scoped and emitted with the same transaction
            if fresh.created_by:
                from app.services import notifications as notify

                await notify.emit(
                    session,
                    user_id=fresh.created_by,
                    organization_id=fresh.organization_id,
                    type="SCRAPE_JOB",
                    title=f"Scrape job completed: {_job_label(fresh)}",
                    body=f"Found {fresh.records_found}, saved {fresh.records_saved}, "
                         f"duplicates {fresh.records_duplicate}, failed {fresh.records_failed}",
                    resource_type="scrape_job",
                    resource_id=str(job.id),
                )
            await session.commit()

    async def _finish_failed(self, job: ScrapeJob, exc: Exception) -> None:
        code, message = job_error_from_exception(exc)
        async with self.session_factory() as session:
            fresh = await session.get(ScrapeJob, job.id)
            if fresh.status not in (JobStatus.RUNNING, JobStatus.QUEUED):
                return
            fresh.status = JobStatus.FAILED
            fresh.completed_at = datetime.now(timezone.utc)
            fresh.error = message
            fresh.error_code = code
            session.add(_event_row(job.id, "JOB_FAILED", message, {"code": code}))
            if fresh.created_by:
                from app.services import notifications as notify

                await notify.emit(
                    session,
                    user_id=fresh.created_by,
                    organization_id=fresh.organization_id,
                    type="SCRAPE_JOB",
                    title=f"Scrape job failed: {_job_label(fresh)}",
                    body=message[:300],
                    resource_type="scrape_job",
                    resource_id=str(job.id),
                )
            from app.services.scraping.engine import _emit_webhook

            await _emit_webhook(
                session, event="RUN_FAILED", job_id=job.id,
                actor_id=fresh.actor_id,
                payload={
                    "error": message,
                    "error_code": code,
                    "records_found": fresh.records_found,
                    "records_saved": fresh.records_saved,
                },
            )
            await session.commit()

    async def _finish_paused(
        self, job: ScrapeJob, *, message: str = "Job paused", event_code: str | None = None,
        outcome: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            fresh = await session.get(ScrapeJob, job.id)
            if fresh.status not in (JobStatus.RUNNING, JobStatus.QUEUED):
                return  # operator already moved it (e.g. CANCELLED while pausing)
            fresh.status = JobStatus.PAUSED
            fresh.paused_at = datetime.now(timezone.utc)
            fresh.stop_requested = "NONE"
            fresh.error = message if event_code else fresh.error
            if outcome:
                fresh.outcome = outcome  # TIMED_OUT — resumable, spec §11
            session.add(_event_row(job.id, "JOB_PAUSED", message, {}))
            if outcome == RunOutcome.TIMED_OUT.value:
                from app.services.scraping.engine import _emit_webhook

                await _emit_webhook(
                    session, event="RUN_TIMED_OUT", job_id=job.id,
                    actor_id=fresh.actor_id,
                    payload={"message": message, "resumable": True},
                )
            await session.commit()
            await self.queue.clear_control(str(job.id))

    async def _finish_cancelled(self, job: ScrapeJob) -> None:
        async with self.session_factory() as session:
            fresh = await session.get(ScrapeJob, job.id)
            if fresh.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
                return
            fresh.status = JobStatus.CANCELLED
            fresh.cancelled_at = datetime.now(timezone.utc)
            fresh.completed_at = datetime.now(timezone.utc)
            fresh.stop_requested = "NONE"
            session.add(_event_row(job.id, "JOB_CANCELLED", "Cancelled at a safe point", {}))
            from app.services.scraping.engine import _emit_webhook

            await _emit_webhook(
                session, event="RUN_ABORTED", job_id=job.id,
                actor_id=fresh.actor_id,
                payload={"records_found": fresh.records_found, "records_saved": fresh.records_saved},
            )
            await session.commit()
            await self.queue.clear_control(str(job.id))

    async def _finalize_dataset(self, dataset_id: uuid.UUID | None, status, clean_status: str) -> None:
        """Terminal dataset state from the REAL run outcome (spec §10/§42)."""
        if dataset_id is None:
            return
        try:
            from app.services.scraping.datasets import DatasetService

            async with self.session_factory() as session:
                svc = DatasetService(session)
                row = await svc.get(dataset_id)
                if row is None:
                    return
                if row.item_count == 0 and status == DatasetStatus.READY:
                    status = DatasetStatus.EMPTY
                await svc.finalize(dataset_id, status=status, clean_status=clean_status)
                await session.commit()
        except Exception:  # noqa: BLE001 — never mask the run's own outcome
            logger.warning("Dataset finalize failed", exc_info=True)

    async def _schedule_retry(self, job: ScrapeJob, exc: Exception) -> str:
        """Retryable failure: requeue with exponential backoff + jitter (§16)."""
        attempt = job.attempt or 1
        if attempt >= (job.max_attempts or 3):
            await self._finish_failed(job, exc)
            return JobStatus.FAILED
        code, message = job_error_from_exception(exc)
        base = self.settings.QBIT_SCRAPER_RETRY_BASE_SECONDS
        delay = min(
            base * (2 ** (attempt - 1)) * (1 + random.uniform(0, 0.3)),
            self.settings.QBIT_SCRAPER_RETRY_MAX_SECONDS,
        )
        async with self.session_factory() as session:
            fresh = await session.get(ScrapeJob, job.id)
            if fresh.status not in (JobStatus.RUNNING,):
                return JobStatus(fresh.status)
            fresh.status = JobStatus.QUEUED
            fresh.error = f"[retry {attempt}/{fresh.max_attempts}] {message}"
            fresh.error_code = code
            fresh.stop_requested = "NONE"
            fresh.lease_owner = None
            fresh.leased_at = None
            session.add(
                _event_row(
                    job.id, "RETRY_SCHEDULED",
                    f"Attempt {attempt} failed ({code}); retrying in {delay:.1f}s",
                    {"attempt": attempt, "delay_seconds": round(delay, 2)},
                )
            )
            await session.commit()
        await self.queue.enqueue(str(job.id), delay_seconds=delay)
        log_with(
            logger, 20, "Job requeued for retry",
            job_id=str(job.id), attempt=attempt, delay=round(delay, 2),
        )
        return JobStatus.QUEUED

    # -------------------------------------------------------------- plumbing
    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        try:
            while True:
                await asyncio.sleep(max(self.lease_seconds / 3, 5))
                await self.queue.renew_lease(str(job_id), self.owner, self.lease_seconds)
                await self._renew_db_lease(job_id)
        except asyncio.CancelledError:
            return

    async def _renew_db_lease(self, job_id: uuid.UUID) -> None:
        """Phase 12 (audit C1): keep the DB lease fresh while the job runs.

        The recovery sweep (`engine.recover_stalled`) judges a crashed worker
        by the DB `leased_at` column. It used to be written once at claim
        time, so every job outliving QBIT_WORKER_LEASE_SECONDS was re-queued
        WHILE STILL RUNNING (duplicate concurrent execution). The heartbeat
        now renews the DB lease with the same owner+RUNNING guard, so an
        expired lease genuinely means a dead worker.
        """
        now = datetime.now(timezone.utc)
        try:
            async with self.session_factory() as session:
                await session.execute(
                    update(ScrapeJob)
                    .where(
                        ScrapeJob.id == job_id,
                        ScrapeJob.status == JobStatus.RUNNING,
                        ScrapeJob.lease_owner == self.owner,
                    )
                    .values(leased_at=now, updated_at=now)
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — a failed renewal must never kill the job
            log_with(
                logger, 30, "DB lease renewal failed",
                job_id=str(job_id),
            )

    def _limits_for(self, job: ScrapeJob) -> JobLimits:
        cfg = job.config or {}
        return JobLimits(
            max_runtime_seconds=float(
                cfg.get("max_runtime_seconds", self.settings.QBIT_SCRAPER_JOB_TIMEOUT_SECONDS)
            ),
            max_pages=cfg.get("max_pages") or None,
            max_records=cfg.get("max_records") or None,
        )

    def _deadline_for(self, job: ScrapeJob | None) -> float:
        cfg = (job.config if job else None) or {}
        return float(cfg.get("max_runtime_seconds", self.settings.QBIT_SCRAPER_JOB_TIMEOUT_SECONDS))

    def _total_estimate(self, job: ScrapeJob) -> int | None:
        inp = job.input or {}
        cfg = job.config or {}
        for key in ("max_results", "max_records", "max_pages"):
            if inp.get(key):
                return int(inp[key])
        return cfg.get("max_pages") or None

    def _http_overrides(self, job: ScrapeJob) -> dict:
        cfg = job.config or {}
        inp = job.input or {}

        def _pick(key: str):
            """Advanced-settings panel (job.config) wins; actor input field is
            the fallback so per-run input knobs are no longer dead."""
            if cfg.get(key) is not None:
                return cfg.get(key)
            return inp.get(key)

        return {
            "request_timeout": _pick("request_timeout"),
            "requests_per_second": cfg.get("requests_per_second"),
            "concurrency": cfg.get("concurrency"),
            "max_retries": cfg.get("max_retries"),
            "respect_robots": _pick("respect_robots"),
        }

    def _progress_flusher(self, job_id: uuid.UUID):
        async def flush(counters: dict) -> None:
            async with self.session_factory() as session:
                fresh = await session.get(ScrapeJob, job_id)
                if fresh is None or fresh.status != JobStatus.RUNNING:
                    return
                fresh.records_found = counters["records_found"]
                fresh.records_saved = counters["records_saved"]
                if "records_updated" in counters:
                    fresh.records_updated = counters["records_updated"]
                fresh.records_duplicate = counters["records_duplicate"]
                fresh.records_failed = counters["records_failed"]
                fresh.progress = counters["progress"]
                fresh.stage = counters["stage"]
                fresh.updated_at = datetime.now(timezone.utc)
                await session.commit()
        return flush


def _job_label(job: ScrapeJob) -> str:
    """Human-readable notification label: actor id + primary input hint.
    (ScrapeJob has no `name` column — deriving one honestly here.)"""
    inp = job.input if isinstance(job.input, dict) else {}
    hint = inp.get("query") or inp.get("url") or inp.get("website") or inp.get("domain") or ""
    hint = str(hint).strip()[:60]
    return f"{job.actor_id} · {hint}" if hint else str(job.actor_id)


class ControlReader:
    """Async control-flag reader handed to the ScraperContext (fast path).

    Reads the queue control key first; if the queue backend cannot see the
    API process's control write (e.g. InProcessQueueBackend across API and
    worker processes, i.e. Redis-less deployments), falls back to the
    durable DB `stop_requested` column, TTL-cached so safe-point polling
    stays cheap.
    """

    _DB_TTL_SECONDS = 4.0

    def __init__(self, queue: QueueBackend, job_id: uuid.UUID, session_factory=None) -> None:
        self._queue = queue
        self._job_id = str(job_id)
        self._session_factory = session_factory
        self._db_checked_at: float = 0.0
        self._db_value: str | None = None

    async def _from_db(self) -> str | None:
        import time as _time

        now = _time.monotonic()
        if now - self._db_checked_at < self._DB_TTL_SECONDS:
            return self._db_value
        self._db_checked_at = now
        if self._session_factory is None:
            return None
        try:
            from sqlalchemy import select

            from app.models.scrape import ScrapeJob

            async with self._session_factory() as session:
                row = await session.execute(
                    select(ScrapeJob.stop_requested).where(ScrapeJob.id == uuid.UUID(self._job_id))
                )
                value = row.scalar_one_or_none()
            self._db_value = value if value and value != "NONE" else None
        except Exception:  # noqa: BLE001 - control-plane errors never kill jobs
            self._db_value = None
        return self._db_value

    async def __call__(self) -> str | None:
        try:
            control = await self._queue.get_control(self._job_id)
        except Exception:  # noqa: BLE001 - control-plane errors never kill jobs
            control = None
        if control:
            return control
        return await self._from_db()


def _event_row(job_id, event_type, message, metadata=None):
    from app.models.scrape import ScrapeJobEvent

    return ScrapeJobEvent(
        job_id=job_id,
        event_type=event_type,
        message=(message or "")[:1000] or None,
        metadata_json=metadata or {},
    )


async def _emit_automation(session, *, job_id: str, payload: dict) -> None:
    """Best-effort automation intake for scrape-job completion (Phase 9 §55)."""
    try:
        from app.automation.services.event_dispatcher import emit_system_event

        await emit_system_event(
            session, event_type="scrape.job.completed", entity_type="scrape_job",
            entity_id=job_id, payload=payload,
            event_id=f"scrape.job.completed:{job_id}",
        )
    except Exception:  # noqa: BLE001 — never break the scrape loop
        pass
