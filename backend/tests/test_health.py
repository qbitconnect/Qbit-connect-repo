"""Health endpoint tests (Brief §16, §17)."""

from __future__ import annotations


async def test_overall_health_healthy(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("healthy", "degraded")
    assert body["timestamp"]
    assert set(body["services"]) == {"database", "storage", "redis"}


async def test_database_health_online(client):
    resp = await client.get("/health/database")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "database"
    assert body["status"] == "online"
    assert body["latency_ms"] >= 0
    # No credentials in health output
    assert "sqlite" not in str(body).lower() or "path" not in body


async def test_storage_health_online(client):
    resp = await client.get("/health/storage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "storage"
    assert body["status"] == "online"
    assert body["writable"] is True
    assert body["free_gb"] is not None


async def test_redis_health_disabled_without_url(client):
    resp = await client.get("/health/redis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "redis"
    assert body["status"] == "disabled"  # local-first: redis optional (Brief §27)


async def test_health_does_not_leak_credentials(client):
    for path in ("/health", "/health/database", "/health/storage", "/health/redis"):
        body = (await client.get(path)).text.lower()
        for forbidden in ("password", "secret_key", "argon2", "bearer"):
            assert forbidden not in body, f"{path} leaked {forbidden}"
