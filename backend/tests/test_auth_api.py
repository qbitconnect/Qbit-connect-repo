"""Auth API tests: login, me, rate limiting (Brief §9, §25)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


async def test_login_success_and_me(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_at"]

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    data = me.json()["data"]
    assert data["email"] == ADMIN_EMAIL
    assert "SUPER_ADMIN" in data["roles"]
    assert "users.manage" in data["permissions"]
    # password hash must never appear
    assert "password" not in me.text.lower()


async def test_login_wrong_password_401_envelope(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "totally-wrong"}
    )
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "INVALID_CREDENTIALS"
    assert err["request_id"]


async def test_login_unknown_email_401(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@qbit.example.com", "password": "whatever-pass"},
    )
    assert resp.status_code == 401


async def test_me_requires_token(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_me_rejects_garbage_token(client: AsyncClient):
    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert resp.status_code == 401


async def test_logout_audited(client: AsyncClient, admin_headers):
    resp = await client.post("/api/v1/auth/logout", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["success"] is True


async def test_login_rate_limited(app, tmp_path):
    """Nth+1 attempt within the window gets 429 (Brief §25 rate limiting)."""
    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from app.main import create_app
    from app.services import rbac as rbac_service

    settings = Settings(
        QBIT_ENV="test",
        QBIT_SECRET_KEY="rl-secret-" + "r" * 48,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/rl.db",
        QBIT_DATA_DIR=tmp_path / "d",
        QBIT_LOG_DIR=tmp_path / "l",
        QBIT_BACKUP_DIR=tmp_path / "b",
        QBIT_RATE_LIMIT_LOGIN_PER_MIN=3,
        _env_file=None,
    )
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, db=db)
    async with db.session() as session:
        await rbac_service.seed_rbac(session)
        await rbac_service.seed_admin(
            session, email=ADMIN_EMAIL, password=ADMIN_PASSWORD, full_name=None
        )

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://rl") as c:
        statuses = []
        for _ in range(5):
            resp = await c.post(
                "/api/v1/auth/login",
                json={"email": ADMIN_EMAIL, "password": "bad-password"},
            )
            statuses.append(resp.status_code)
        assert statuses[:3] == [401, 401, 401]  # auth failures before limit
        assert statuses[3] == 429 and statuses[4] == 429
        err = (await c.post(
            "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )).json()["error"]
        assert err["code"] == "TOO_MANY_REQUESTS"
    await db.close()


async def test_login_is_audited(client, admin_headers):
    """Login produces an audit trail entry (Brief §12)."""
    from sqlalchemy import select

    from app.models.audit import AuditLog

    db = client._transport.app.state.db  # noqa: SLF001 - test introspection
    async with db.session() as session:
        rows = await session.execute(
            select(AuditLog).where(AuditLog.action == "user.login")
        )
        assert rows.scalars().first() is not None
