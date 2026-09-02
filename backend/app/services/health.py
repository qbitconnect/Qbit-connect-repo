"""Health aggregation (Brief §16, §17): database, storage, redis — no secrets."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.session import DatabaseManager
from app.redis_client import RedisManager
from app.services.storage import StorageService


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class HealthService:
    def __init__(
        self,
        db: DatabaseManager | None,
        storage: StorageService,
        redis: RedisManager,
    ) -> None:
        self._db = db
        self._storage = storage
        self._redis = redis

    async def database(self) -> dict[str, Any]:
        if self._db is None:
            return {"service": "database", "status": "unavailable", "detail": "not configured", "timestamp": _ts()}
        result = await self._db.health()
        return {"service": "database", **result, "timestamp": _ts()}

    async def storage(self) -> dict[str, Any]:
        result = self._storage.backend.health()
        return {"service": "storage", **result, "timestamp": _ts()}

    async def redis(self) -> dict[str, Any]:
        result = await self._redis.health()
        return {"service": "redis", **result, "timestamp": _ts()}

    async def overall(self) -> tuple[dict[str, Any], int]:
        db = await self.database()
        storage = await self.storage()
        redis = await self.redis()
        services = {"database": db, "storage": storage, "redis": redis}

        critical_down = db["status"] != "online" or storage["status"] != "online"
        degraded = redis["status"] == "unavailable"
        if critical_down:
            status, http = "unhealthy", 503
        elif degraded:
            status, http = "degraded", 200
        else:
            status, http = "healthy", 200

        return (
            {"status": status, "services": services, "timestamp": _ts()},
            http,
        )
