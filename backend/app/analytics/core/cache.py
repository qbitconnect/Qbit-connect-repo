"""Analytics cache (spec §22).

Redis-backed, best-effort:
- keys embed a version, the domain, the metric set and a SHA-256 of the
  canonical filter/period payload (so different filters never collide)
- user/visibility scope is part of the key for scope-dependent analytics
  (team domain, report visibility) — one user can never receive another
  user's restricted numbers
- Redis absent or erroring → direct query (analytics stays fully functional,
  matching the project's local-first rule)
- TTL-based invalidation only; aggregates are rebuilt by the worker and the
  operational tables stay the source of truth
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.analytics.core.time import stable_hash
from app.core.logging import get_logger
from app.redis_client import RedisManager

logger = get_logger("qbit.analytics.cache")

KEY_PREFIX = "qbit:analytics:v1"

#: cache TTLs per domain class (seconds)
TTL_DASHBOARD = 60
TTL_DISTRIBUTION = 120
TTL_CAMPAIGN_DETAIL = 60


class AnalyticsCache:
    def __init__(self, redis: RedisManager, default_ttl: int = TTL_DASHBOARD) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    def build_key(self, domain: str, payload: dict, scope: str = "global") -> str:
        return f"{KEY_PREFIX}:{domain}:{scope}:{stable_hash(payload)}"

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        ttl: int | None = None,
    ) -> Any:
        client = self._redis.get_client() if self._redis.enabled else None
        if client is None:
            return await factory()
        try:
            raw = await client.get(key)
            if raw is not None:
                return json.loads(raw)
        except Exception:  # noqa: BLE001 — cache must never break analytics
            logger.warning("Analytics cache read failed; falling back to query")
        value = await factory()
        try:
            await client.set(key, json.dumps(value, default=str), ex=ttl or self._default_ttl)
        except Exception:  # noqa: BLE001
            logger.warning("Analytics cache write failed (non-fatal)")
        return value

    async def clear_all(self) -> int:
        """Admin action: drop analytics cache entries (never operational data)."""
        client = self._redis.get_client() if self._redis.enabled else None
        if client is None:
            return 0
        removed = 0
        try:
            cursor = 0
            while True:
                cursor, keys = await client.scan(
                    cursor=cursor, match=f"{KEY_PREFIX}:*", count=200
                )
                if keys:
                    removed += await client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:  # noqa: BLE001
            logger.warning("Analytics cache clear failed (non-fatal)")
        return removed
