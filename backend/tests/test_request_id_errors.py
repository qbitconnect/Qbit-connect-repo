"""Request-ID propagation + error envelope tests (Brief §19, §20)."""

from __future__ import annotations

import uuid


async def test_request_id_generated_and_returned(client):
    resp = await client.get("/health")
    rid = resp.headers.get("x-request-id")
    assert rid and rid.startswith("req_")


async def test_inbound_request_id_preserved(client):
    rid = f"req_{uuid.uuid4().hex}"
    resp = await client.get("/health", headers={"X-Request-ID": rid})
    assert resp.headers["x-request-id"] == rid


async def test_request_ids_unique_per_request(client):
    r1 = (await client.get("/health")).headers["x-request-id"]
    r2 = (await client.get("/health")).headers["x-request-id"]
    assert r1 != r2


async def test_404_error_envelope(client):
    resp = await client.get("/api/v1/definitely/not/here")
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    err = body["error"]
    assert err["code"] == "RESOURCE_NOT_FOUND"
    assert err["request_id"]
    assert "message" in err


async def test_validation_error_envelope(client, admin_headers):
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "not-an-email", "password": ""}
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]


async def test_unhandled_exception_returns_clean_500(client, app):
    """Internal errors: uniform envelope, no stack trace, no internal paths.

    Uses raise_app_exceptions=False because Starlette intentionally re-raises
    after sending the error response (for server logging) — uvicorn behaves the
    same way in production.
    """
    from httpx import ASGITransport, AsyncClient

    async def boom():
        raise RuntimeError("internal detail /qbit-data/secret-path")

    app.router.add_api_route("/__boom", boom, methods=["GET"], include_in_schema=False)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        resp = await c.get("/__boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    text = str(body)
    assert "RuntimeError" not in text
    assert "internal detail" not in text
    assert "secret-path" not in text


async def test_method_not_allowed_envelope(client, admin_headers):
    resp = await client.delete("/health")
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
