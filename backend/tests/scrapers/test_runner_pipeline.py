"""Runner + pipeline + queue integration tests (brief §11, §14–§21, §25, §32, §44, §45).

Uses the REAL JobRunner + ResultPipeline + Deduplicator + LeadService against
an isolated SQLite DB, with fake actors — no external services.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.scrape import JobStatus, ScrapeJob, ScrapeJobCheckpoint, ScrapeJobEvent
from app.scrapers.core.base import ScraperActor
from app.scrapers.core.context import JobLimits, ScraperContext
from app.scrapers.core.exceptions import ScraperNetworkError, ScraperValidationError
from app.services.scraping.engine import JobEngine, sanitize_job_config


def _item(i: int, email: str | None = None) -> dict:
    return {
        "business_name": f"Biz {i}",
        "email": email or f"biz{i}@example.com",
        "phone": f"+9111000000{i:02d}",
    }


class _YieldNSchema:
    """Minimal pydantic-like schema for test actors."""

    @classmethod
    def model_validate(cls, data):
        return data

    @classmethod
    def model_json_schema(cls):
        return {"properties": {}, "required": []}


class _YieldNActor:
    """Yields N items, checkpointing its position so resumes continue."""

    id = "fake"
    version = "1.0.0"
    total = 5

    async def initialize(self, ctx):
        pass

    async def run(self, ctx):
        start = int((ctx.checkpoint.data or {}).get("next_index", 0)) if ctx.checkpoint else 0
        for i in range(start, self.total):
            ctx.checkpoint_cursor({"next_index": i + 1})
            yield _item(i)
            await ctx.save_checkpoint()

    async def cleanup(self, ctx):
        pass


async def _make_job(db, *, actor_id="fake", input=None, config=None, max_attempts=3) -> uuid.UUID:
    async with db.session() as session:
        job = ScrapeJob(
            actor_id=actor_id,
            actor_version="1.0.0",
            status=JobStatus.QUEUED,
            input=input or {},
            config=config or {},
            max_attempts=max_attempts,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id


async def _get_job(db, job_id) -> ScrapeJob:
    async with db.session() as session:
        job = await session.get(ScrapeJob, job_id)
        await session.refresh(job)
        return job


# ------------------------------------------------------------------- happy path
@pytest.mark.asyncio
async def test_runner_completes_and_persists_leads_and_files(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env
    job_id = await _make_job(db)
    status = await runner.execute(job_id, _YieldNActor())
    assert status == JobStatus.COMPLETED

    job = await _get_job(db, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.records_found == 5
    assert job.records_saved == 5
    assert job.records_duplicate == 0
    assert job.progress == 100.0

    raw = (storage_root / "fake" / str(job_id) / "raw.jsonl").read_text().strip().splitlines()
    normalized = (storage_root / "fake" / str(job_id) / "normalized.jsonl").read_text().strip().splitlines()
    assert len(raw) == 5 and len(normalized) == 5

    async with db.session() as session:
        from sqlalchemy import select

        from app.models.scrape import Lead

        leads = (await session.scalars(select(Lead))).all()
        assert len(leads) == 5
        lead = leads[0]
        assert lead.email_norm == "biz0@example.com"
        assert lead.source_actor_id == "fake"
        assert lead.source_job_id == job_id


# ------------------------------------------------------------------- dedup
@pytest.mark.asyncio
async def test_runner_dedups_by_email(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env

    class DupActor(_YieldNActor):
        total = 4

        async def run(self, ctx):
            for i in range(4):
                yield _item(0, email="same@biz.example.com")  # identical every time
                await ctx.check_stopped()

    job_id = await _make_job(db)
    status = await runner.execute(job_id, DupActor())
    assert status == JobStatus.COMPLETED
    job = await _get_job(db, job_id)
    assert job.records_found == 4
    assert job.records_saved == 1
    assert job.records_duplicate == 3


# ------------------------------------------------------------------- invalid
@pytest.mark.asyncio
async def test_runner_counts_invalid_records(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env

    class BadActor(_YieldNActor):
        total = 3

        async def run(self, ctx):
            yield {"garbage": True}      # no name, no contact → invalid
            yield _item(1)
            yield {"nonsense": "x"}
            await ctx.check_stopped()

    job_id = await _make_job(db)
    status = await runner.execute(job_id, BadActor())
    assert status == JobStatus.COMPLETED
    job = await _get_job(db, job_id)
    assert job.records_failed == 2
    assert job.records_saved == 1
    errors = (storage_root / "fake" / str(job_id) / "errors.jsonl").read_text().strip().splitlines()
    assert len(errors) == 2


# ------------------------------------------------------------------- pause/resume
@pytest.mark.asyncio
async def test_runner_pause_resume_from_checkpoint(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env
    job_id = await _make_job(db)

    # Pause after the 2nd item via the control plane (fast path).
    state = {"reads": 0}

    async def control():
        state["reads"] += 1
        return "PAUSE" if state["reads"] >= 2 else None

    class PauseQueue(queue.__class__):
        def __init__(self):
            super().__init__()
            self.flags: dict[str, str] = {}

        async def get_control(self, job_id):
            return self.flags.get(job_id)

        async def set_control(self, job_id, value, ttl_seconds=86400):
            self.flags[job_id] = value

        async def clear_control(self, job_id):
            self.flags.pop(job_id, None)

        async def enqueue(self, job_id, *, delay_seconds=0):
            self._pending = getattr(self, "_pending", [])
            self._pending.append(job_id)

    pq = PauseQueue()
    runner2 = runner.__class__(
        settings=settings, session_factory=runner.session_factory,
        storage_root=storage_root, queue=pq, owner="test-worker",
    )
    # simulate the API: set the control flag BEFORE execution starts
    pq.flags[str(job_id)] = "PAUSE"
    status = await runner2.execute(job_id, _YieldNActor())
    assert status == JobStatus.PAUSED
    job = await _get_job(db, job_id)
    assert job.status == JobStatus.PAUSED
    assert job.records_saved >= 1

    # checkpoint persisted
    async with db.session() as session:
        from sqlalchemy import select

        cps = (await session.scalars(
            select(ScrapeJobCheckpoint).where(ScrapeJobCheckpoint.job_id == job_id)
        )).all()
        assert cps, "checkpoint must exist after pause"
        cursor = max(cps, key=lambda c: c.created_at).cursor
        assert cursor.get("next_index", 0) >= 1

    # RESUME: clear control, run again — continues from checkpoint
    pq.flags.pop(str(job_id), None)
    status = await runner2.execute(job_id, _YieldNActor())
    assert status == JobStatus.COMPLETED
    job = await _get_job(db, job_id)
    assert job.records_saved == 5  # 5 unique leads total, none re-saved


# ------------------------------------------------------------------- cancel
@pytest.mark.asyncio
async def test_runner_cancels_cooperatively(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env
    job_id = await _make_job(db)

    class CancelQueue(queue.__class__):
        def __init__(self):
            super().__init__()
            self.flags: dict[str, str] = {"x": "CANCEL"}

        async def get_control(self, job_id):
            return self.flags.get(str(job_id))

        async def set_control(self, job_id, value, ttl_seconds=86400):
            self.flags[str(job_id)] = value

        async def clear_control(self, job_id):
            self.flags.pop(str(job_id), None)

    cq = CancelQueue()
    cq.flags[str(job_id)] = "CANCEL"
    runner2 = runner.__class__(
        settings=settings, session_factory=runner.session_factory,
        storage_root=storage_root, queue=cq, owner="test-worker",
    )
    status = await runner2.execute(job_id, _YieldNActor())
    assert status == JobStatus.CANCELLED
    job = await _get_job(db, job_id)
    assert job.status == JobStatus.CANCELLED
    assert job.cancelled_at is not None


# ------------------------------------------------------------------- retry
@pytest.mark.asyncio
async def test_runner_retries_transient_failures_then_fails(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env

    class FlakyActor(ScraperActor):
        id, version, name, description = "flaky", "1.0.0", "Flaky", "test"
        input_schema = _YieldNSchema

        async def run(self, ctx):
            raise ScraperNetworkError("connection reset")
            yield  # pragma: no cover

    job_id = await _make_job(db, max_attempts=2)
    status = await runner.execute(job_id, FlakyActor())
    assert status == JobStatus.QUEUED  # scheduled for retry (attempt 1/2)
    job = await _get_job(db, job_id)
    assert "[retry 1/2]" in (job.error or "")

    status = await runner.execute(job_id, FlakyActor())
    assert status == JobStatus.FAILED  # attempts exhausted
    job = await _get_job(db, job_id)
    assert job.error_code == "SCRAPER_NETWORK_ERROR"


@pytest.mark.asyncio
async def test_runner_never_retries_validation_failures(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env

    class InvalidConfigActor(ScraperActor):
        id, version, name, description = "bad", "1.0.0", "Bad", "test"
        input_schema = _YieldNSchema

        async def run(self, ctx):
            raise ScraperValidationError("input is broken")
            yield  # pragma: no cover

    job_id = await _make_job(db)
    status = await runner.execute(job_id, InvalidConfigActor())
    assert status == JobStatus.FAILED
    job = await _get_job(db, job_id)
    assert job.error_code == "SCRAPER_VALIDATION_FAILED"


# ------------------------------------------------------------------- limits
@pytest.mark.asyncio
async def test_runner_stops_cleanly_at_record_limit(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env
    job_id = await _make_job(db, config={"max_records": 3})
    status = await runner.execute(job_id, _YieldNActor())
    assert status == JobStatus.COMPLETED
    job = await _get_job(db, job_id)
    assert job.records_saved == 3
    async with db.session() as session:
        from sqlalchemy import select

        events = (await session.scalars(
            select(ScrapeJobEvent).where(
                ScrapeJobEvent.job_id == job_id,
                ScrapeJobEvent.event_type == "LIMIT_REACHED",
            )
        )).all()
        assert events, "LIMIT_REACHED must be reported"


def test_context_limit_checks():
    ctx = ScraperContext(
        job_id=uuid.uuid4(), actor_id="x", actor_version="1",
        limits=JobLimits(max_records=3, max_pages=2, started_at=time.monotonic()),
    )
    ctx.check_record_limit(2)
    with pytest.raises(Exception, match="max_records"):
        ctx.check_record_limit(3)
    ctx.check_page_limit(1)
    with pytest.raises(Exception, match="max_pages"):
        ctx.check_page_limit(2)
    dead = ScraperContext(
        job_id=uuid.uuid4(), actor_id="x", actor_version="1",
        limits=JobLimits(max_runtime_seconds=10, started_at=time.monotonic() - 100),
    )
    with pytest.raises(Exception, match="max runtime"):
        dead.check_deadline()


# ------------------------------------------------------------------- recovery
@pytest.mark.asyncio
async def test_engine_recovers_stalled_running_jobs(scrape_env):
    settings, db, queue, runner, storage_root = scrape_env
    job_id = await _make_job(db)

    # simulate a worker crash: RUNNING with an expired lease
    async with db.session() as session:
        job = await session.get(ScrapeJob, job_id)
        job.status = JobStatus.RUNNING
        job.leased_at = datetime.now(timezone.utc) - timedelta(seconds=999)
        job.lease_owner = "dead-worker"
        await session.commit()

    async with db.session() as session:
        engine = JobEngine(session, queue)
        requeued = await engine.recover_stalled(lease_ttl_seconds=60)
        assert requeued >= 1

    job = await _get_job(db, job_id)
    assert job.status == JobStatus.QUEUED
    assert job.resumed_count == 1
    # the recovered job is back in the queue
    got = await queue.dequeue(timeout_seconds=0.1)
    assert got == str(job_id)


# ------------------------------------------------------------------- config
def test_sanitize_job_config_whitelist_and_ranges():
    clean = sanitize_job_config(
        {"max_pages": 10, "respect_robots": False, "requests_per_second": 2.5}
    )
    assert clean == {"max_pages": 10, "respect_robots": False, "requests_per_second": 2.5}
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        sanitize_job_config({"totally_unknown": 1})
    with pytest.raises(ValidationError):
        sanitize_job_config({"max_pages": 10**9})
    with pytest.raises(ValidationError):
        sanitize_job_config({"respect_robots": "yes"})
    with pytest.raises(ValidationError):
        sanitize_job_config({"dedup_policy": "chaos"})
    assert sanitize_job_config(None) == {}
