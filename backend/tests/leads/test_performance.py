"""Phase 4 §44: performance and scale behaviour on a synthetic dataset.

Isolated test database only — production data is never touched. 100,000
leads are inserted with bulk executemany; then we verify that search,
pagination, filters, bulk updates and streaming export stay practical.
"""

from __future__ import annotations

import time
import uuid as uuid_mod

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text

from app.core.config import Settings
from app.db.base import Base
from app.db.session import DatabaseManager
from app.models.scrape import Lead
from app.services.audit import AuditService
from app.services.files import FileService
from app.services.leads import LeadWorkspaceService
from app.services.leads.exporter import LeadExportService
from app.services.storage import StorageService

N = 100_000
BULK_IDS = 1_000


def _insert_dataset_sync(connection, n: int) -> None:
    """Fast deterministic insert of n leads via raw SQL executemany."""
    connection.execute(text(
        "INSERT INTO leads (id, business_name, contact_name, email, phone, website,"
        " city, state, status, source, quality_score, email_norm, seen_count, tags,"
        " social_links, metadata_json)"
        " VALUES (:id, :name, NULL, :email, :phone, :website, :city, :state, 'NEW',"
        " 'perf-test', :q, :email, 1, '[]', '{}', '{}')"
    ), [
        {
            "id": str(uuid_mod.uuid4()).replace("-", ""),
            "name": f"Bulk Biz {i:06d}",
            "email": f"bulk{i}@biz.in" if i % 2 == 0 else None,
            "phone": f"9{i % 10}00000000" if i % 3 == 0 else None,
            "website": "bulk.biz.in" if i % 5 == 0 else None,
            "city": "Ahmedabad" if i % 2 == 0 else "Surat",
            "state": "Gujarat",
            "q": 50 + (i % 50),
        }
        for i in range(n)
    ])


class _PerfEnv:
    def __init__(self, db: DatabaseManager, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.storage = StorageService(settings)
        self.files = FileService(self.storage, AuditService())


@pytest_asyncio.fixture(scope="module")
async def perf_env(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("perf")
    settings = Settings(
        QBIT_ENV="test", QBIT_SECRET_KEY="test-secret-key-" + "a" * 48,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/perf.db",
        QBIT_DATA_DIR=tmp_path / "data", _env_file=None,
    )
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(lambda sync_conn: _insert_dataset_sync(sync_conn, N))
    yield _PerfEnv(db, settings)
    await db.close()


@pytest.mark.asyncio
async def test_search_pagination_and_filters_at_scale(perf_env):
    svc = LeadWorkspaceService()

    start = time.perf_counter()
    # deep offset pagination (page 500 of 50-row pages)
    async with perf_env.db.session() as session:
        rows, total = await svc.search(session, page=500, page_size=50)
    deep_page = time.perf_counter() - start
    assert total == N and len(rows) == 50
    assert deep_page < 5.0, f"deep pagination took {deep_page:.2f}s"

    async with perf_env.db.session() as session:
        start = time.perf_counter()
        rows, total = await svc.search(session, search="Bulk Biz 042100")
        search_time = time.perf_counter() - start
        assert total == 1
        assert search_time < 5.0, f"search took {search_time:.2f}s"

        start = time.perf_counter()
        rows, total = await svc.search(
            session,
            filters={"and": [{"field": "city", "op": "eq", "value": "Ahmedabad"},
                             {"field": "has_email", "op": "eq", "value": True}]},
            sort="quality_score",
        )
        filter_time = time.perf_counter() - start
    assert total == N // 2
    assert filter_time < 5.0, f"filter took {filter_time:.2f}s"


@pytest.mark.asyncio
async def test_bulk_action_does_not_issue_one_query_per_lead(perf_env):
    svc = LeadWorkspaceService()
    async with perf_env.db.session() as session:
        ids = [
            row[0] if isinstance(row[0], uuid_mod.UUID) else uuid_mod.UUID(row[0])
            for row in (await session.execute(select(Lead.id).limit(BULK_IDS))).all()
        ]
        assert len(ids) == BULK_IDS

        query_count = {"n": 0}

        def _count(conn, cursor, statement, parameters, context, executemany):
            query_count["n"] += 1

        bind = session.sync_session.bind
        event.listen(bind, "before_cursor_execute", _count)
        try:
            counts = await svc.bulk_action(
                session, action="set_status", lead_ids=ids,
                params={"status": "VERIFIED"},
            )
        finally:
            event.remove(bind, "before_cursor_execute", _count)

        assert counts["affected"] == BULK_IDS
        # set-based SQL: a handful of statements, NOT 1000 (one per lead)
        assert query_count["n"] < 25, f"bulk issued {query_count['n']} queries — not set-based"


@pytest.mark.asyncio
async def test_large_export_is_chunked_and_completes(perf_env):
    svc = LeadExportService(perf_env.storage, perf_env.files)
    async with perf_env.db.session() as session:
        record = await svc.create(session, format_name="csv", scope="all")
        start = time.perf_counter()
        record = await svc.run(session, record)
        elapsed = time.perf_counter() - start

        assert record.status == "COMPLETED", record.error
        assert record.row_count == N  # 100,000 rows streamed in 500-row chunks
        assert elapsed < 120, f"export took {elapsed:.1f}s"
        file_record = await perf_env.files.get(session, record.file_id)
        assert file_record.size > N * 40  # real content, not an empty file
