"""QBIT scrape worker — `python -m app.worker` (brief §11, §14, §15).

Isolated from the API process on purpose: a crashing scraper must never take
FastAPI down (§15). Responsibilities:

- startup recovery sweep (crashed leases → requeue from checkpoint; stale
  QUEUED → re-enqueue)
- dequeue loop over the QueueBackend (Redis or in-process fallback)
- bounded concurrency (QBIT_WORKER_MAX_CONCURRENT_JOBS)
- graceful shutdown (SIGTERM/SIGINT): in-flight jobs are checkpointed + PAUSED,
  never failed (§18)
- periodic re-sweep so a stalled sibling worker's jobs are recovered too

One job per asyncio task: JobRunner.execute() owns claim → context → actor →
pipeline → outcome. Actor resolution happens through the registry; an unknown
actor fails the job honestly (FAILED with SCRAPER_CONFIGURATION_ERROR).
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
import uuid

from app.core.config import get_settings
from app.core.logging import get_logger, log_with, setup_logging
from app.db.session import DatabaseManager
from app.redis_client import RedisManager
from app.services.scraping.engine import JobEngine
from app.services.scraping.queue import build_queue_backend
from app.services.scraping.registry import ActorRegistry
from app.services.scraping.runner import JobRunner
from app.services.storage import StorageService

logger = get_logger("qbit.worker")


class ScrapeWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        setup_logging(self.settings)
        self.db = DatabaseManager(self.settings)
        self.redis = RedisManager(self.settings)
        self.queue = build_queue_backend(self.settings, self.redis)
        self.registry = ActorRegistry()
        self.runner = JobRunner(
            settings=self.settings,
            session_factory=self.db.session,
            storage_root=self.settings.data_dir / "scraper-results",
            queue=self.queue,
            owner=f"worker-{uuid.uuid4().hex[:8]}",
        )
        self._tasks: set[asyncio.Task] = set()
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------ setup
    def load_actors(self) -> None:
        from app.scrapers.bootstrap import register_builtin_actors

        register_builtin_actors(self.registry, self.settings)
        summary = self.registry.summary()
        log_with(logger, 20, "Actor registry loaded", **summary)

    async def recover(self) -> None:
        async with self.db.session() as session:
            engine = JobEngine(session, self.queue)
            count = await engine.recover_stalled(
                lease_ttl_seconds=self.settings.QBIT_WORKER_LEASE_SECONDS
            )
        if count:
            log_with(logger, 20, "Recovery sweep re-enqueued jobs", count=count)

    # ------------------------------------------------------------------- loop
    async def run(self) -> None:
        self.load_actors()
        await self.recover()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:  # pragma: no cover — Windows
                pass
        log_with(
            logger, 20, "Scrape worker started",
            queue=self.queue.name,
            max_concurrent=self.settings.QBIT_WORKER_MAX_CONCURRENT_JOBS,
        )

        sweep_task = asyncio.create_task(self._periodic_sweep())
        data_task = asyncio.create_task(self._data_jobs_loop())
        campaign_task = asyncio.create_task(self._campaign_loop())
        outbox_task = asyncio.create_task(self._inbox_outbox_loop())
        automation_task = asyncio.create_task(self._automation_loop())
        analytics_task = asyncio.create_task(self._analytics_loop())
        schedules_task = asyncio.create_task(self._schedules_loop())
        heartbeat_file_task = asyncio.create_task(self._liveness_loop())
        backup_task = asyncio.create_task(self._backup_loop())
        try:
            while not self._shutdown.is_set():
                if len(self._tasks) >= self.settings.QBIT_WORKER_MAX_CONCURRENT_JOBS:
                    await asyncio.sleep(self.settings.QBIT_WORKER_POLL_SECONDS)
                    continue
                try:
                    job_id = await self.queue.dequeue(
                        timeout_seconds=self.settings.QBIT_WORKER_POLL_SECONDS
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — audit M1: a transient broker
                    # error (Redis blip) must kill the dequeue attempt, not the
                    # worker process; back off and keep the loop alive.
                    logger.exception("Queue dequeue failed; backing off")
                    await asyncio.sleep(max(self.settings.QBIT_WORKER_POLL_SECONDS * 5, 5.0))
                    continue
                if job_id is None:
                    continue
                task = asyncio.create_task(self._process(job_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            backup_task.cancel()
            heartbeat_file_task.cancel()
            sweep_task.cancel()
            data_task.cancel()
            campaign_task.cancel()
            outbox_task.cancel()
            automation_task.cancel()
            analytics_task.cancel()
            schedules_task.cancel()
            await self._drain()
            await self.queue.aclose()
            await self.db.close()
            await self.redis.close()
            log_with(logger, 20, "Scrape worker stopped")

    async def _data_jobs_loop(self) -> None:
        """Phase 4: large imports/exports are processed next to scrape jobs.

        Runs on its own cadence; a failure in one data job never touches the
        scrape loop (isolation rule §15).
        """
        from app.services.files import FileService
        from app.services.audit import AuditService
        from app.services.leads.jobs import DataJobWorker

        worker = DataJobWorker(
            StorageService(self.settings),
            FileService(StorageService(self.settings), AuditService()),
            owner=f"data-{uuid.uuid4().hex[:8]}",
        )
        try:
            while not self._shutdown.is_set():
                try:
                    async with self.db.session() as session:
                        ran = await worker.process_pending(session)
                except Exception:  # noqa: BLE001 — keep the loop alive
                    logger.exception("Data jobs loop iteration failed")
                    ran = 0
                await asyncio.sleep(
                    self.settings.QBIT_WORKER_POLL_SECONDS if ran else max(
                        self.settings.QBIT_WORKER_POLL_SECONDS * 5, 5.0
                    )
                )
        except asyncio.CancelledError:
            return

    def _handle_signal(self) -> None:
        log_with(logger, 20, "Shutdown signal received; pausing in-flight jobs")
        self._shutdown.set()

    async def _schedules_loop(self) -> None:
        """Scrape schedule tick (spec §SCHEDULING) — claims due schedules and
        enqueues jobs through the regular JobEngine path. The worker remains
        the ONLY scheduler; a failing schedule never touches other loops."""
        from app.services.scraping.scheduling import ScrapeScheduleService

        poll = max(self.settings.QBIT_WORKER_POLL_SECONDS, 5.0)
        try:
            while not self._shutdown.is_set():
                fired = 0
                try:
                    async with self.db.session() as session:
                        service = ScrapeScheduleService(session)
                        due = await service.due_schedules(limit=10)
                    for schedule in due:
                        fired += await self._fire_schedule(schedule)
                except Exception:  # noqa: BLE001 — keep the loop alive
                    logger.exception("Schedules loop iteration failed")
                await asyncio.sleep(poll if fired else max(poll * 3, 15.0))
        except asyncio.CancelledError:
            return

    async def _fire_schedule(self, schedule) -> int:
        """Claim one due schedule and enqueue its job. Returns 1 on success."""
        from app.services.scraping.engine import JobEngine
        from app.services.scraping.scheduling import ScrapeScheduleService
        owner = f"schedule-{uuid.uuid4().hex[:8]}"
        async with self.db.session() as session:
            service = ScrapeScheduleService(session)
            claimed = await service.claim_due(schedule.id, owner=owner)
            if claimed is None:
                return 0
            actor_id = claimed.actor_id
            schedule_id = claimed.id
            payload_input = dict(claimed.input or {})
            payload_config = dict(claimed.config or {})
        entry = self.registry.entry(actor_id)
        if entry is None or not entry.enabled:
            async with self.db.session() as session:
                await ScrapeScheduleService(session).mark_outcome(
                    schedule_id, ok=False,
                    error=f"Actor {actor_id!r} is not registered or disabled",
                )
            return 0
        try:
            validated = entry.actor.validate_input(payload_input)
            if not validated.valid:
                raise ValueError("input validation failed: " + "; ".join(validated.errors))
            async with self.db.session() as session:
                engine = JobEngine(session, self.queue)
                job = await engine.create_job(
                    entry.actor,
                    validated.normalized_input,
                    payload_config,
                    created_by=claimed.created_by,
                )
                job_id = job.id
                await ScrapeScheduleService(session).mark_outcome(
                    schedule_id, ok=True, job_id=job_id
                )
            log_with(
                logger, 20, "Schedule fired",
                schedule_id=str(schedule_id), actor_id=actor_id, job_id=str(job_id),
            )
            return 1
        except Exception as exc:  # noqa: BLE001 — one bad schedule must not stop others
            async with self.db.session() as session:
                await ScrapeScheduleService(session).mark_outcome(
                    schedule_id, ok=False, error=str(exc)
                )
            return 0

    async def _campaign_loop(self) -> None:
        """Phase 5: marketing engine loop — schedules, launches, sends.

        Runs on its own cadence next to the scrape and data-job loops; a
        failing campaign never touches the other loops (isolation rule).
        """
        from app.services.marketing import build_provider_registry
        from app.services.marketing.worker import CampaignWorker

        worker = CampaignWorker(
            self.settings,
            build_provider_registry(self.settings),
            owner=f"campaign-{uuid.uuid4().hex[:8]}",
        )
        try:
            while not self._shutdown.is_set():
                try:
                    async with self.db.session() as session:
                        actions = await worker.process_cycle(session)
                except Exception:  # noqa: BLE001 — keep the loop alive
                    logger.exception("Campaign loop iteration failed")
                    actions = 0
                await asyncio.sleep(
                    self.settings.QBIT_WORKER_POLL_SECONDS if actions else max(
                        self.settings.QBIT_WORKER_POLL_SECONDS * 3, 3.0
                    )
                )
        except asyncio.CancelledError:
            return

    async def _inbox_outbox_loop(self) -> None:
        """Phase 8: inbox reply delivery loop — drains the outbox through the
        SAME provider abstraction campaigns use (§24). Isolated from the
        scrape/campaign loops; a failing reply never stops the others."""
        from app.services.inbox.outbox import OutboxService
        from app.services.marketing import build_provider_registry

        worker = OutboxService(
            self.settings,
            build_provider_registry(self.settings),
            owner=f"inbox-{uuid.uuid4().hex[:8]}",
        )
        try:
            while not self._shutdown.is_set():
                try:
                    async with self.db.session() as session:
                        processed = await worker.process_cycle(session)
                except Exception:  # noqa: BLE001 — keep the loop alive
                    logger.exception("Inbox outbox loop iteration failed")
                    processed = 0
                await asyncio.sleep(
                    self.settings.QBIT_WORKER_POLL_SECONDS if processed else max(
                        self.settings.QBIT_WORKER_POLL_SECONDS * 2, 2.0
                    )
                )
        except asyncio.CancelledError:
            return

    async def _automation_loop(self) -> None:
        """Phase 9: workflow automation loop — scheduled triggers, execution
        claiming, node execution. Isolated from the other loops: a failing
        workflow never touches scraping/campaigns/inbox (isolation rule)."""
        from app.automation.workers.automation_worker import AutomationWorker

        worker = AutomationWorker(
            self.settings,
            owner=f"automation-{uuid.uuid4().hex[:8]}",
        )
        try:
            while not self._shutdown.is_set():
                try:
                    async with self.db.session() as session:
                        actions = await worker.process_cycle(session)
                except Exception:  # noqa: BLE001 — keep the loop alive
                    logger.exception("Automation loop iteration failed")
                    actions = 0
                await asyncio.sleep(
                    self.settings.QBIT_WORKER_POLL_SECONDS if actions else max(
                        self.settings.QBIT_WORKER_POLL_SECONDS * 3, 3.0
                    )
                )
        except asyncio.CancelledError:
            return

    async def _analytics_loop(self) -> None:
        """Phase 10: analytics loops — (1) daily aggregate incremental refresh
        on its own cadence, (2) report run execution every poll cycle. Both are
        failure-isolated: a failing aggregate never touches report runs or the
        other loops (isolation rule)."""
        from app.analytics.aggregation import AggregationService
        from app.analytics.reports.executor import ReportWorker
        from app.services.files import FileService
        from app.services.audit import AuditService

        storage = StorageService(self.settings)
        report_worker = ReportWorker(
            owner=f"analytics-{uuid.uuid4().hex[:8]}",
            storage_files=FileService(storage, AuditService()),
            max_snapshot_rows=self.settings.QBIT_ANALYTICS_MAX_SNAPSHOT_ROWS,
        )
        dialect = "postgresql" if self.settings.DATABASE_URL.startswith("postgresql") \
            else "sqlite"
        aggregator = AggregationService(dialect=dialect)
        next_aggregation = 0.0  # run on the first cycle
        try:
            while not self._shutdown.is_set():
                # 1) report runs: cheap polling, at most one claimed per cycle
                try:
                    async with self.db.session() as session:
                        await report_worker.process_cycle(session)
                except Exception:  # noqa: BLE001 — keep the loop alive
                    logger.exception("Report run loop iteration failed")

                # 2) aggregate refresh on its configured cadence
                now = asyncio.get_running_loop().time()
                if self.settings.QBIT_ANALYTICS_AGGREGATION_ENABLED \
                        and now >= next_aggregation:
                    try:
                        async with self.db.session() as session:
                            results = await aggregator.refresh_incremental(session)
                            done = [r for r in results if r.get("status") == "COMPLETED"]
                            if done:
                                log_with(logger, 20, "Analytics aggregates refreshed",
                                         tables=len(done))
                    except Exception:  # noqa: BLE001 — keep the loop alive
                        logger.exception("Analytics aggregation iteration failed")
                    next_aggregation = now + max(
                        self.settings.QBIT_ANALYTICS_AGGREGATION_INTERVAL_SECONDS, 300
                    )
                await asyncio.sleep(max(self.settings.QBIT_WORKER_POLL_SECONDS, 2.0))
        except asyncio.CancelledError:
            return

    async def _drain(self) -> None:
        """Bounded drain (Phase 12 audit H3): wait the grace period for
        in-flight jobs, then CANCEL whatever remains so the runner's
        CancelledError path (checkpoint + PAUSED, never FAILED) actually
        executes instead of Docker's SIGKILL landing mid-job."""
        if not self._tasks:
            return
        grace = self.settings.QBIT_WORKER_DRAIN_SECONDS
        done, pending = await asyncio.wait(set(self._tasks), timeout=grace)
        if pending:
            log_with(
                logger, 30, "Drain grace period elapsed; pausing in-flight jobs",
                pending=len(pending), grace_seconds=grace,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if done:
            await asyncio.gather(*done, return_exceptions=True)

    async def _liveness_loop(self) -> None:
        """Phase 12 (audit M12): write a liveness heartbeat file so the
        container healthcheck can distinguish a live worker from a hung one.
        Best-effort by design: observability must never break the worker."""
        try:
            path = self.settings.data_dir / "cache" / "worker-heartbeat.json"
            while True:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({
                        "worker_id": self.runner.owner,
                        "ts": time.time(),
                        "active_jobs": len(self._tasks),
                    }))
                except Exception:  # noqa: BLE001
                    logger.exception("Worker heartbeat write failed")
                await asyncio.sleep(min(self.settings.QBIT_WORKER_POLL_SECONDS * 5, 30.0))
        except asyncio.CancelledError:
            return

    async def _periodic_sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(self.settings.QBIT_WORKER_LEASE_SECONDS, 60))
                await self.recover()
        except asyncio.CancelledError:
            return

    async def _backup_loop(self) -> None:
        """Phase 12 (audit H2, brief §12): scheduled backup beat.

        Every QBIT_BACKUP_SCHEDULE_HOURS (0 = disabled): database backup +
        file-data backup + config manifest, then verify the newest artifacts
        and apply GFS retention pruning. Runs in the single worker process so
        schedules never double-fire; every step is failure-isolated.
        """
        from app.services.backup import BackupService

        hours = self.settings.QBIT_BACKUP_SCHEDULE_HOURS
        if hours <= 0:
            return
        service = BackupService(self.settings)
        try:
            while not self._shutdown.is_set():
                await asyncio.sleep(hours * 3600)
                if self._shutdown.is_set():
                    return
                try:
                    db_result = service.run_database_backup()
                    files_result = service.run_files_backup()
                    service.backup_config_snapshot()
                    log_with(
                        logger, 20, "Scheduled backup cycle completed",
                        db=db_result.status, files=files_result.status,
                    )
                    # verify the newest DB + files artifact (§12 verification)
                    for result in (db_result, files_result):
                        if result.status == "completed" and result.path:
                            verdict = service.verify_backup(result.path)
                            log_with(
                                logger,
                                20 if verdict.get("status") == "verified" else 40,
                                "Backup verification",
                                path=result.path, verdict=verdict.get("status"),
                            )
                    removed = service.prune_backups(
                        keep_daily=self.settings.QBIT_BACKUP_RETENTION_DAILY,
                        keep_weekly=self.settings.QBIT_BACKUP_RETENTION_WEEKLY,
                        keep_monthly=self.settings.QBIT_BACKUP_RETENTION_MONTHLY,
                    )
                    if removed:
                        log_with(logger, 20, "Backup retention pruned", count=len(removed))
                except Exception:  # noqa: BLE001 — one failed cycle never stops the next
                    logger.exception("Scheduled backup cycle failed")
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------- jobs
    async def _process(self, job_id: str) -> None:
        try:
            job_uuid = uuid.UUID(job_id)
        except ValueError:
            log_with(logger, 40, "Invalid job id on queue", job_id=job_id)
            return
        # The queue carries the JOB id; the actor must be resolved from the
        # job row's actor_id (registry keys are actor slugs, not job UUIDs).
        from sqlalchemy import select

        from app.models.scrape import ScrapeJob

        async with self.db.session() as session:
            row = await session.execute(
                select(ScrapeJob.actor_id).where(ScrapeJob.id == job_uuid)
            )
            actor_id = row.scalar_one_or_none()
        if actor_id is None:
            log_with(logger, 40, "Job row not found for queue message", job_id=job_id)
            return
        entry = self.registry.entry(actor_id)
        if entry is None or not entry.enabled:
            await self._fail_unknown_actor(job_id)
            return
        try:
            await self.runner.execute(job_uuid, entry.actor)
        except Exception:  # noqa: BLE001 — worker isolation is the whole point
            logger.exception(
                "Job execution crashed at worker level",
                extra={"extra_fields": {"job_id": job_id}},
            )

    async def _fail_unknown_actor(self, job_id: str) -> None:
        """Honest failure for jobs whose actor is unknown/disabled (§55)."""
        from datetime import datetime, timezone

        from sqlalchemy import select

        from app.models.scrape import JobStatus, ScrapeJob

        try:
            job_uuid = uuid.UUID(job_id)
        except ValueError:
            return
        async with self.db.session() as session:
            row = await session.execute(
                select(ScrapeJob).where(
                    ScrapeJob.id == job_uuid,
                    ScrapeJob.status.in_([JobStatus.QUEUED, JobStatus.PAUSED]),
                )
            )
            job = row.scalar_one_or_none()
            if job is None:
                return
            code = "SCRAPER_CONFIGURATION_ERROR"
            message = f"Actor {job.actor_id!r} is not registered or disabled"
            job.status = JobStatus.FAILED
            job.completed_at = datetime.now(timezone.utc)
            job.error = message
            job.error_code = code
            session.add(
                _event_row(job.id, "JOB_FAILED", message, {"code": code})
            )
            await session.commit()


def _event_row(job_id, event_type, message, metadata):
    from app.models.scrape import ScrapeJobEvent

    return ScrapeJobEvent(
        job_id=job_id,
        event_type=event_type,
        message=(message or "")[:1000] or None,
        metadata_json=metadata or {},
    )


def main() -> int:
    try:
        worker = ScrapeWorker()
        asyncio.run(worker.run())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
