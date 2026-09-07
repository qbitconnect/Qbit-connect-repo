"""Phase 8 inbox test fixtures — reuse the marketing fixtures + inbox helpers."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.marketing.conftest import (  # noqa: F401 — re-export fixtures
    admin_headers,
    make_lead,
    seed_account,
    seed_leads,
    seed_template,
    seeded_db,
    viewer_headers,
)


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def admin_headers(client):
    token = await _login(client, "admin@qbit.example.com", "Sup3rSecret!Pass")
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def viewer_headers(client):
    token = await _login(client, "viewer@qbit.example.com", "V13werSecret!Pass")
    return {"Authorization": f"Bearer {token}"}
