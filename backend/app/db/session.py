"""Async engine + session management (Brief §13).

- Connection pooling (size/overflow from configuration; Postgres only — SQLite
  drivers manage their own pool).
- `pool_pre_ping` guards against stale connections.
- Sessions are per-request/per-operation; failure => context manager rolls back.
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.logging import get_logger, log_with

logger = get_logger("qbit.db")


def build_engine(settings: Settings) -> AsyncEngine:
    url = settings.DATABASE_URL
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "future": True}
    if url.startswith("postgresql"):
        kwargs.update(
            pool_size=settings.QBIT_DB_POOL_SIZE,
            max_overflow=settings.QBIT_DB_MAX_OVERFLOW,
            pool_recycle=1800,
        )
    elif url.startswith("sqlite"):
        # File-based SQLite: serialize writes; pool bounds are irrelevant here.
        kwargs.update(connect_args={"timeout": 30})
    return create_async_engine(url, **kwargs)


class DatabaseManager:
    """Owns the engine + session factory for one application instance."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = build_engine(settings)
        self.session_factory = async_sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False
        )

    def session(self) -> AsyncSession:
        return self.session_factory()

    async def close(self) -> None:
        await self.engine.dispose()
        log_with(logger, 20, "Database engine disposed")  # INFO

    async def health(self) -> dict[str, Any]:
        """`SELECT 1` roundtrip. Returns status payload; never raises."""
        started = time.perf_counter()
        try:
            async with self.session() as session:
                await session.execute(text("SELECT 1"))
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            return {"status": "online", "latency_ms": latency_ms}
        except Exception as exc:  # noqa: BLE001 - health must never raise
            log_with(logger, 40, "Database health check failed", error=str(exc))
            return {"status": "unavailable", "error": type(exc).__name__}
