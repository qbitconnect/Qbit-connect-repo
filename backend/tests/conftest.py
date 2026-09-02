"""Shared fixtures: fully isolated per-test database + storage (Brief §31).

- SQLite/aiosqlite file DB in a pytest tmp dir (spec-permitted for tests).
- QBIT data dir/log dir/backup dir also in tmp — no production data is touched.
- The app is built through create_app() DI (app.state.settings / app.state.db),
  so no global configuration is mutated.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

TEST_SECRET = "test-secret-key-" + "a" * 48

ADMIN_EMAIL = "admin@qbit.example.com"
ADMIN_PASSWORD = "Sup3rSecret!Pass"
VIEWER_EMAIL = "viewer@qbit.example.com"
VIEWER_PASSWORD = "V13werSecret!Pass"


@pytest_asyncio.fixture
async def app(tmp_path: Path):
    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from app.main import create_app
    from app.services import rbac as rbac_service

    data_dir = tmp_path / "qbit-data"
    settings = Settings(
        QBIT_ENV="test",
        QBIT_SECRET_KEY=TEST_SECRET,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/qbit-test.db",
        QBIT_DATA_DIR=data_dir,
        QBIT_EXPORT_DIR=data_dir / "exports",
        QBIT_LOG_DIR=tmp_path / "logs",
        QBIT_BACKUP_DIR=tmp_path / "backups",
        QBIT_RATE_LIMIT_LOGIN_PER_MIN=50,  # default; rate-limit test overrides
        _env_file=None,  # ignore any local .env — full isolation
    )
    assert not settings.is_production

    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    application = create_app(settings, db=db)

    async with db.session() as session:
        await rbac_service.seed_rbac(session)
        await rbac_service.seed_admin(
            session, email=ADMIN_EMAIL, password=ADMIN_PASSWORD, full_name="Test Admin"
        )
        # A VIEWER user for permission-denial tests
        from app.core.security import hash_password
        from app.models.user import User

        viewer = User(
            email=VIEWER_EMAIL,
            password_hash=hash_password(VIEWER_PASSWORD),
            full_name="Test Viewer",
        )
        session.add(viewer)
        await session.flush()
        await rbac_service.set_user_roles(session, viewer.id, ["VIEWER"])

    yield application
    await db.close()


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def admin_headers(client):
    token = await _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def viewer_headers(client):
    token = await _login(client, VIEWER_EMAIL, VIEWER_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@qbit.example.com"
