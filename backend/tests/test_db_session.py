"""Database session management tests (Brief §13, §31)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.models.user import User


async def test_session_reuse_and_commit(client):
    """Multiple operations share one engine (pooled); commits persist."""
    db = client._transport.app.state.db  # noqa: SLF001
    async with db.session() as session:
        user = User(email="pool-a@t.local", password_hash="x", full_name=None)
        session.add(user)
        await session.commit()
    async with db.session() as session:
        found = await session.scalar(select(User).where(User.email == "pool-a@t.local"))
    assert found is not None


async def test_rollback_on_failure(client):
    """An exception inside the session context rolls back uncommitted work."""
    db = client._transport.app.state.db  # noqa: SLF001
    with pytest.raises(RuntimeError):
        async with db.session() as session:
            session.add(User(email="rollback-a@t.local", password_hash="x"))
            raise RuntimeError("boom")
    async with db.session() as session:
        found = await session.scalar(select(User).where(User.email == "rollback-a@t.local"))
    assert found is None


async def test_health_select_one(client):
    db = client._transport.app.state.db  # noqa: SLF001
    health = await db.health()
    assert health["status"] == "online"
    assert health["latency_ms"] >= 0


async def test_pool_pre_ping_configured(app):
    db = app.state.db  # noqa: SLF001
    assert db.engine.pool._pre_ping is True


async def test_session_factory_async(client):
    db = client._transport.app.state.db  # noqa: SLF001
    async with db.session() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1
