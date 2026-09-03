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
import signal
import sys
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
        await self._recover_marketing()
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
        marketing_task = asyncio.create_task(self._marketing_loop())
        try:
            while not self._shutdown.is_set():
                if len(self._tasks) >= self.settings.QBIT_WORKER_MAX_CONCURRENT_JOBS:
                    await asyncio.sleep(self.settings.QBIT_WORKER_POLL_SECONDS)
                    continue
                job_id = await self.queue.dequeue(
                    timeout_seconds=self.settings.QBIT_WORKER_POLL_SECONDS
                )
                if job_id is None:
                    continue
                task = asyncio.create_task(self._process(job_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            sweep_task.cancel()
            data_task.cancel()
            marketing_task.cancel()
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

    # -------------------------------------------------------------- marketing
    async def _marketing_loop(self) -> None:
        """Phase 7: campaign email delivery loop.

        - dedicated marketing queue (Redis or in-process fallback)
        - operational rate control: emails/minute + concurrency (§42) —
          throttling ONLY, never a provider-restriction bypass
        - per-send isolation: one recipient failure never kills the loop
        """
        from app.services.marketing.delivery import EmailDeliveryService
        from app.services.marketing.queue import build_marketing_queue

        mqueue = build_marketing_queue(self.settings, self.redis)
        delivery = EmailDeliveryService(self.settings)
        interval = 60.0 / max(self.settings.QBIT_MARKETING_EMAILS_PER_MINUTE, 1)
        log_with(
            logger, 20, "Marketing delivery loop started",
            queue=mqueue.name,
            emails_per_minute=self.settings.QBIT_MARKETING_EMAILS_PER_MINUTE,
        )
        try:
            while not self._shutdown.is_set():
                recipient_id = await mqueue.dequeue(timeout_seconds=2.0)
                if recipient_id is None:
                    continue
                try:
                    async with self.db.session() as session:
                        await delivery.process(session, uuid.UUID(recipient_id), queue=mqueue)
                except Exception:  # noqa: BLE001 — per-send isolation
                    logger.exception(
                        "Marketing send crashed",
                        extra={"extra_fields": {"recipient_id": recipient_id}},
                    )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    async def _recover_marketing(self) -> None:
        """Startup recovery: recipients stuck in SENDING after a crash have
        UNKNOWN provider acceptance — they are marked FAILED (never blindly
        resent, spec §20) with a code telling the operator what happened."""
        import datetime as _dt

        from sqlalchemy import update

        from app.models.marketing import CampaignRecipient, RecipientStatus

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            seconds=self.settings.QBIT_WORKER_LEASE_SECONDS
        )
        async with self.db.session() as session:
            result = await session.execute(
                update(CampaignRecipient)
                .where(
                    CampaignRecipient.status == RecipientStatus.SENDING,
                    CampaignRecipient.last_attempt_at < cutoff,
                )
                .values(
                    status=RecipientStatus.FAILED,
                    last_error_code="SEND_STATE_UNKNOWN",
                    last_error="Worker restarted mid-send; provider acceptance unknown",
                )
            )
            await session.commit()
            if result.rowcount:
                log_with(logger, 20, "Marketing recovery: sends marked unknown", count=result.rowcount)

    async def _drain(self) -> None:
        if not self._tasks:
            return
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _periodic_sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(self.settings.QBIT_WORKER_LEASE_SECONDS, 60))
                await self.recover()
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------- jobs
    async def _process(self, job_id: str) -> None:
        try:
            job_uuid = uuid.UUID(job_id)
        except ValueError:
            log_with(logger, 40, "Invalid job id on queue", job_id=job_id)
            return
        try:
            actor = self.registry.get(job_id)
        except KeyError:
            await self._fail_unknown_actor(job_id)
            return
        try:
            await self.runner.execute(job_uuid, actor)
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
