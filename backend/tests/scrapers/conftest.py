"""Shared fixtures for the scraping-engine test suite (brief §46, §47).

- Fully isolated SQLite DB per test (tmp path) — mirrors tests/conftest.py.
- InProcessQueueBackend (no Redis needed).
- JobRunner built against the same session factory the API uses.
- Actors get their HTTP via httpx.MockTransport — NO live external sites.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.db.base import Base
from app.db.session import DatabaseManager
from app.services.scraping.queue import InProcessQueueBackend
from app.services.scraping.registry import ActorRegistry
from app.services.scraping.runner import JobRunner

TEST_SECRET = "test-secret-key-" + "a" * 48


def make_settings(tmp_path: Path, **overrides) -> Settings:
    data_dir = tmp_path / "qbit-data"
    base = dict(
        QBIT_ENV="test",
        QBIT_SECRET_KEY=TEST_SECRET,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/qbit-scrape.db",
        QBIT_DATA_DIR=data_dir,
        QBIT_EXPORT_DIR=data_dir / "exports",
        QBIT_LOG_DIR=tmp_path / "logs",
        QBIT_BACKUP_DIR=tmp_path / "backups",
        # fast tests
        QBIT_SCRAPER_REQUEST_TIMEOUT_SECONDS=5,
        QBIT_SCRAPER_RPS_PER_HOST=50.0,  # no throttling in tests
        QBIT_SCRAPER_RETRY_BASE_SECONDS=0.1,
        QBIT_SCRAPER_RETRY_MAX_SECONDS=1.0,
        QBIT_WORKER_LEASE_SECONDS=60,
        QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS=True,  # MockTransport never dials out
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def scrape_env(tmp_path: Path):
    """(settings, db, queue, runner, storage_root) — full worker-side stack."""
    settings = make_settings(tmp_path)
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    queue = InProcessQueueBackend()
    storage_root = settings.data_dir / "scraper-results"
    runner = JobRunner(
        settings=settings,
        session_factory=db.session,
        storage_root=storage_root,
        queue=queue,
        owner="test-worker",
    )
    yield settings, db, queue, runner, storage_root
    await db.close()


@pytest.fixture
def registry() -> ActorRegistry:
    from app.scrapers.bootstrap import register_builtin_actors

    settings = Settings(QBIT_ENV="test", _env_file=None)
    return register_builtin_actors(ActorRegistry(), settings)
