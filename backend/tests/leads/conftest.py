"""Isolated async session fixture for lead service tests."""

from __future__ import annotations

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def seeded_db(tmp_path):
    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import DatabaseManager

    data_dir = tmp_path / "qbit-data"
    settings = Settings(
        QBIT_ENV="test",
        QBIT_SECRET_KEY="test-secret-key-" + "a" * 48,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/leads-test.db",
        QBIT_DATA_DIR=data_dir,
        QBIT_LOG_DIR=tmp_path / "logs",
        QBIT_BACKUP_DIR=tmp_path / "backups",
        _env_file=None,
    )
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.session() as session:
        yield session
    await db.close()
