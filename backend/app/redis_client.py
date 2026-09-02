"""Redis foundation (Brief §15).

Connection + health only. Job queues/caching arrive in later phases.
Redis is OPTIONAL: with no REDIS_URL the app remains fully functional
(local-first rule, Brief §27) and health reports `disabled`.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger("qbit.redis")


class RedisManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.REDIS_URL)

    def get_client(self) -> Any:
        if self._client is None and self.enabled:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self.settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "detail": "REDIS_URL not configured"}
        started = time.perf_counter()
        try:
            client = self.get_client()
            pong = await client.ping()
            latency = round((time.perf_counter() - started) * 1000, 2)
            return {
                "status": "online" if pong else "degraded",
                "latency_ms": latency,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis health check failed", extra={"extra_fields": {"error": type(exc).__name__}})
            return {"status": "unavailable", "error": type(exc).__name__}
