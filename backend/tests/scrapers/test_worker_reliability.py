"""Phase 12 — worker reliability regression tests (audit C1, H1, H3).

- claim atomicity: a RUNNING job can never be claimed twice
- DB lease renewal: the heartbeat keeps `leased_at` fresh so the recovery
  sweep never re-queues a job that is still executing (C1)
- recovery sweep still recovers genuinely crashed leases
- cancellation (the drain path) checkpoints + PAUSES, never fails (H3/§18)
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.scrape import JobStatus, ScrapeJob
from app.services.scraping.engine import JobEngine
from app.services.scraping.runner import JobRunner
from tests.scrapers.test_runner_pipeline import _make_job


def _owner_owner(job) -> str:
    return job.lease_owner


@pytest.mark.asyncio
async def test_claim_is_atomic_second_claim_loses(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env
    job_id = await _make_job(db)

    first = await runner.claim(job_id)
    assert first is not None
    assert first.status == JobStatus.RUNNING

    # a second worker (or a duplicate queue delivery) must lose the race
    second_owner = JobRunner(
        settings=settings,
        session_factory=db.session,
        storage_root=storage_root,
        queue=queue,
        owner="worker-second",
    )
    second = await second_owner.claim(job_id)
    assert second is None


@pytest.mark.asyncio
async def test_db_lease_renewal_prevents_false_recovery(scrape_env):
    """C1 regression: a long-running job with a renewed DB lease must NOT be
    re-queued by the recovery sweep while it is still executing."""
    settings, db, queue, runner, storage_root = scrape_env
    job_id = await _make_job(db)

    job = await runner.claim(job_id)
    assert job is not None

    # simulate the job having started long ago (lease would look expired)
    stale = datetime.now(timezone.utc) - timedelta(seconds=settings.QBIT_WORKER_LEASE_SECONDS * 3)
    async with db.session() as session:
        row = await session.get(ScrapeJob, job_id)
        row.leased_at = stale
        await session.commit()

    # without the heartbeat renewal, the sweep WOULD recover it — prove the
    # stale state is otherwise recoverable, then renew and prove it is not.
    # 1) stale lease is recoverable (pre-condition sanity check)
    async with db.session() as session:
        engine = JobEngine(session, queue)
        recovered = await engine.recover_stalled(
            lease_ttl_seconds=settings.QBIT_WORKER_LEASE_SECONDS
        )
        assert recovered == 1
        row = await session.get(ScrapeJob, job_id)
        assert row.status == JobStatus.QUEUED

    # 2) re-claim and renew the lease like the runner heartbeat does
    job = await runner.claim(job_id)
    assert job is not None
    async with db.session() as session:
        row = await session.get(ScrapeJob, job_id)
        row.leased_at = datetime.now(timezone.utc) - timedelta(
            seconds=settings.QBIT_WORKER_LEASE_SECONDS * 3
        )
        await session.commit()
    await runner._renew_db_lease(job_id)

    async with db.session() as session:
        engine = JobEngine(session, queue)
        recovered = await engine.recover_stalled(
            lease_ttl_seconds=settings.QBIT_WORKER_LEASE_SECONDS
        )
        assert recovered == 0, "a renewed RUNNING lease must never be re-queued"
        row = await session.get(ScrapeJob, job_id)
        assert row.status == JobStatus.RUNNING


@pytest.mark.asyncio
async def test_crashed_worker_lease_is_still_recovered(scrape_env):
    """The C1 fix must not break genuine crash recovery."""
    settings, db, queue, runner, storage_root = scrape_env
    job_id = await _make_job(db)
    job = await runner.claim(job_id)
    assert job is not None

    # worker died: no more heartbeat renewals
    async with db.session() as session:
        row = await session.get(ScrapeJob, job_id)
        row.leased_at = datetime.now(timezone.utc) - timedelta(
            seconds=settings.QBIT_WORKER_LEASE_SECONDS * 3
        )
        await session.commit()

    async with db.session() as session:
        engine = JobEngine(session, queue)
        recovered = await engine.recover_stalled(
            lease_ttl_seconds=settings.QBIT_WORKER_LEASE_SECONDS
        )
        assert recovered == 1
        row = await session.get(ScrapeJob, job_id)
        assert row.status == JobStatus.QUEUED
        assert row.resumed_count == 1


@pytest.mark.asyncio
async def test_cancellation_checkpoints_and_pauses(scrape_env):
    """H3: the bounded drain CANCELS in-flight tasks; the runner's
    CancelledError path must checkpoint + PAUSE (never FAIL)."""
    settings, db, queue, runner, storage_root = scrape_env

    class SlowActor:
        id = "slow"
        version = "1.0.0"

        async def initialize(self, ctx):
            pass

        async def run(self, ctx):
            yield {"business_name": "One", "email": "one@example.com"}
            await asyncio.sleep(600)  # simulates a long page fetch
            yield {"business_name": "Two", "email": "two@example.com"}

        async def cleanup(self, ctx):
            pass

    class _Schema:
        @classmethod
        def model_validate(cls, data):
            return data

        @classmethod
        def model_json_schema(cls):
            return {"properties": {}, "required": []}

    SlowActor.input_schema = _Schema

    job_id = await _make_job(db)
    task = asyncio.create_task(runner.execute(job_id, SlowActor()))
    await asyncio.sleep(0.8)  # let it claim + process item one
    task.cancel()
    # the runner DELIBERATELY swallows CancelledError (checkpoint + PAUSE),
    # so the task completes and reports PAUSED instead of raising.
    final_status = await task
    assert final_status == JobStatus.PAUSED

    job = None
    async with db.session() as session:
        job = await session.get(ScrapeJob, job_id)
        await session.refresh(job)
        status = job.status
    assert status == JobStatus.PAUSED
