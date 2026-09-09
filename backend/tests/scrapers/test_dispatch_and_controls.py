"""Product-upgrade P0 regression tests (audit findings 1-9).

- worker dispatch resolves the actor from the JOB ROW (was: registry.get(job_uuid))
- finish-path notification labels (ScrapeJob has no `name` column)
- _NullProgress.add_updated exists (HIGH-confidence duplicate merge path)
- ControlReader falls back to DB stop_requested without Redis
- engine.list_jobs enforces tenant scope in SQL (accurate totals/pages)
- actor input knobs (respect_robots / request_timeout) are no longer dead
- directory next_page resolves relative hrefs against the current page
- maps provider query string is URL-encoded
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

from app.models.scrape import JobStatus, ScrapeJob
from app.scrapers.actors.business_directory.adapters import (
    DirectoryAdapterConfig,
    GenericDirectoryAdapter,
)
from app.scrapers.actors.google_maps.provider import HttpMapsProvider
from app.scrapers.core.context import _NullProgress
from app.services.scraping.engine import JobEngine
from app.services.scraping.queue import InProcessQueueBackend
from app.services.scraping.registry import ActorRegistry
from app.services.scraping.runner import ControlReader, JobRunner, _job_label
from tests.scrapers.test_runner_pipeline import _YieldNActor, _YieldNSchema

# --------------------------------------------------------------------- helpers


class _FakeActor(_YieldNActor):
    id = "fake"
    version = "1.0.0"
    name = "Fake"
    description = "fake actor for dispatch tests"
    input_schema = _YieldNSchema


def _worker_with(db, queue, runner, registry) -> object:
    from app.worker import ScrapeWorker

    worker = ScrapeWorker.__new__(ScrapeWorker)
    worker.db = db
    worker.queue = queue
    worker.runner = runner
    worker.registry = registry
    return worker


async def _make_job_row(
    db,
    *,
    actor_id="fake",
    created_by=None,
    organization_id=None,
    input=None,
    config=None,
):
    async with db.session() as session:
        job = ScrapeJob(
            actor_id=actor_id,
            actor_version="1.0.0",
            status=JobStatus.QUEUED,
            input=input or {},
            config=config or {},
            created_by=created_by,
            organization_id=organization_id,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id


# ------------------------------------------------------------- worker dispatch


@pytest.mark.asyncio
async def test_worker_resolves_actor_from_job_row(scrape_env):
    """Audit #1: registry keys are actor slugs — dispatch must read the job row."""
    settings, db, queue, runner, _root = scrape_env
    registry = ActorRegistry()
    registry.register(_FakeActor())
    worker = _worker_with(db, queue, runner, registry)

    job_id = await _make_job_row(db)
    await worker._process(str(job_id))

    async with db.session() as session:
        job = await session.get(ScrapeJob, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.records_saved == 5


@pytest.mark.asyncio
async def test_worker_honestly_fails_disabled_actor(scrape_env):
    settings, db, queue, runner, _root = scrape_env
    registry = ActorRegistry()
    registry.register(_FakeActor(), enabled=False)
    worker = _worker_with(db, queue, runner, registry)

    job_id = await _make_job_row(db)
    await worker._process(str(job_id))

    async with db.session() as session:
        job = await session.get(ScrapeJob, job_id)
    assert job.status == JobStatus.FAILED
    assert job.error_code == "SCRAPER_CONFIGURATION_ERROR"


@pytest.mark.asyncio
async def test_worker_tolerates_missing_job_row(scrape_env):
    settings, db, queue, runner, _root = scrape_env
    registry = ActorRegistry()
    registry.register(_FakeActor())
    worker = _worker_with(db, queue, runner, registry)
    # must not raise for a queue message without a job row
    await worker._process(str(uuid.uuid4()))


# ------------------------------------------------- notification label / finish


@pytest.mark.asyncio
async def test_finish_success_notifies_with_derived_label(scrape_env):
    """Audit #2: `fresh.name` AttributeError broke every completed-job path
    whenever created_by was set (the API always sets it)."""
    settings, db, queue, runner, _root = scrape_env
    job_id = await _make_job_row(
        db, created_by=uuid.uuid4(), organization_id=uuid.uuid4(),
        input={"query": "coffee roasters"},
    )
    status = await runner.execute(job_id, _FakeActor())
    assert status == JobStatus.COMPLETED
    async with db.session() as session:
        job = await session.get(ScrapeJob, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.error_code is None


def test_job_label_derivation():
    job = ScrapeJob(actor_id="google-maps", input={"query": "cafés in Delhi"})
    assert _job_label(job) == "google-maps · cafés in Delhi"
    bare = ScrapeJob(actor_id="website", input={})
    assert _job_label(bare) == "website"


# ----------------------------------------------------------------- NullProgress


def test_null_progress_supports_add_updated():
    """Audit #3: pipeline calls progress.add_updated() on duplicate merges."""
    sink = _NullProgress()
    sink.add_updated(3)  # must not raise
    sink.add_found()
    sink.add_saved()


# ---------------------------------------------------------------- ControlReader


@pytest.mark.asyncio
async def test_control_reader_falls_back_to_db_stop_requested(scrape_env):
    """Audit #4: without Redis the InProcess backend cannot see the API
    process's control write — the DB column is the only truth."""
    settings, db, queue, _runner, _root = scrape_env
    job_id = await _make_job_row(db)

    reader = ControlReader(queue, job_id, db.session)
    assert await reader() is None  # nothing requested yet

    async with db.session() as session:
        row = await session.get(ScrapeJob, job_id)
        row.stop_requested = "PAUSE"
        await session.commit()

    # expire the TTL window so the reader re-queries the DB
    import time

    reader._db_checked_at = time.monotonic() - (reader._DB_TTL_SECONDS + 0.1)
    assert await reader() == "PAUSE"

    # TTL cache: an immediate clear must still read PAUSE from the cache
    async with db.session() as session:
        row = await session.get(ScrapeJob, job_id)
        row.stop_requested = "NONE"
        await session.commit()
    assert await reader() == "PAUSE"

    # ...and after the TTL window it observes NONE again
    reader._db_checked_at = time.monotonic() - (reader._DB_TTL_SECONDS + 0.1)
    assert await reader() is None


@pytest.mark.asyncio
async def test_control_reader_queue_fast_path_wins(scrape_env):
    settings, db, queue, _runner, _root = scrape_env
    job_id = await _make_job_row(db)
    await queue.set_control(str(job_id), "CANCEL")
    reader = ControlReader(queue, job_id, db.session)
    assert await reader() == "CANCEL"


# --------------------------------------------------- tenant scope in SQL listing


@pytest.mark.asyncio
async def test_list_jobs_org_scope_in_sql(scrape_env):
    """Audit #5: totals must count only the caller's organization."""
    settings, db, queue, _runner, _root = scrape_env
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    for org in (org_a, org_b, None):
        await _make_job_row(db, organization_id=org)

    async with db.session() as session:
        engine = JobEngine(session, queue)
        rows, total = await engine.list_jobs(organization_id=org_a, page_size=50)
    orgs = {row.organization_id for row in rows}
    assert orgs <= {org_a, None}
    assert org_b not in orgs
    assert total == 2  # org_a + legacy NULL-org row


# ----------------------------------------------------- actor input knob wiring


@pytest.mark.asyncio
async def test_http_overrides_fall_back_to_input_knobs(scrape_env):
    """Audit #7: per-input respect_robots / request_timeout were dead."""
    settings, db, _queue, runner, _root = scrape_env

    async def _check():
        job_input = {"request_timeout": 42, "respect_robots": False}
        async with db.session() as session:
            job = ScrapeJob(
                actor_id="fake", actor_version="1.0.0",
                status=JobStatus.QUEUED, input=job_input, config={},
            )
            session.add(job)
            await session.flush()
            overrides = runner._http_overrides(job)
            job.config = {"request_timeout": 10}
            await session.flush()
            overrides_config_wins = runner._http_overrides(job)
            await session.delete(job)
            await session.commit()
        return overrides, overrides_config_wins

    overrides, overrides_config_wins = await _check()
    assert overrides["request_timeout"] == 42
    assert overrides["respect_robots"] is False
    # Advanced-settings panel (job.config) still wins when set
    assert overrides_config_wins["request_timeout"] == 10


# ----------------------------------------------------- directory pagination fix


def test_directory_next_page_resolves_against_current_page():
    """Audit #8: relative next-page hrefs must resolve against the CURRENT
    page URL, not the configured list_url (breaks on page 2+)."""
    adapter = GenericDirectoryAdapter()
    from bs4 import BeautifulSoup

    config = DirectoryAdapterConfig(
        list_url="https://dir.example/list.html",
        item_selector=".item",
        fields={"business_name": "h2"},
        pagination_next_selector="a.next",
    )
    soup = BeautifulSoup('<a class="next" href="page2.html">Next</a>', "html.parser")
    current = "https://dir.example/list/page1.html"
    resolved = adapter.next_page(config, soup, current)
    assert resolved == "https://dir.example/list/page2.html"


# ------------------------------------------------------- maps provider encoding


@pytest.mark.asyncio
async def test_maps_provider_urlencodes_query():
    """Audit #9: raw string concat corrupted spaces/&/# in provider queries."""

    class _CaptureHttp:
        def __init__(self):
            self.urls = []

        async def get_json(self, url, headers=None):
            self.urls.append(url)
            return {"results": [], "next_page_token": None}

    http = _CaptureHttp()
    provider = HttpMapsProvider(base_url="https://provider.example/search", api_key=None)
    await provider.search(
        query="cafés & bars #1",
        city="New Delhi",
        state=None,
        country=None,
        language=None,
        page_token=None,
        max_results=10,
        http=http,
    )
    q = parse_qs(urlsplit(http.urls[0]).query)
    assert q["q"] == ["cafés & bars #1"]
    assert q["city"] == ["New Delhi"]
