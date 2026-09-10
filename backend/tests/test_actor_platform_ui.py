"""Actor Platform UI tests (spec §24/§41) — pages render, anonymous users
are redirected, and the catalog is generated from actor metadata."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

UI_PAGES = (
    "/actors",
    "/datasets",
    "/tasks",
    "/run-webhooks",
    "/storage",
    "/api-docs",
)


async def test_actor_platform_pages_render_for_admin(client, admin_headers):
    # The UI uses cookie sessions, but ui_user also accepts Bearer via the
    # API dependency chain — use the UI login flow for a true end-to-end.
    resp = await client.post(
        "/login", data={"email": "admin@qbit.example.com", "password": "Sup3rSecret!Pass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    for page in UI_PAGES:
        r = await client.get(page)
        assert r.status_code == 200, f"{page} -> {r.status_code}"
        assert "QBIT" in r.text


async def test_actor_platform_pages_require_login(client):
    for page in UI_PAGES:
        r = await client.get(page, follow_redirects=False)
        assert r.status_code == 303, f"{page} should redirect anonymous users"
        assert "/login" in r.headers.get("location", "")


async def test_actors_catalog_shows_schema_driven_cards(client, admin_headers):
    await client.post(
        "/login", data={"email": "admin@qbit.example.com", "password": "Sup3rSecret!Pass"},
        follow_redirects=False,
    )
    r = await client.get("/actors")
    for slug in ("instagram", "meta-ads-library", "linkedin-public", "justdial", "indiamart", "universal-web"):
        assert slug in r.text, f"catalog must be generated from registry metadata ({slug} missing)"
    # honest zero-state numbers (spec §42): stats exist per card
    assert "Total runs" in r.text


async def test_api_docs_lists_documented_surface(client, admin_headers):
    await client.post(
        "/login", data={"email": "admin@qbit.example.com", "password": "Sup3rSecret!Pass"},
        follow_redirects=False,
    )
    r = await client.get("/api-docs")
    assert "/api/v1/actors/{slug}/runs" in r.text
    assert "POST /api/actors/{actor}/runs" in r.text.replace("spec: ", "") or "/api/v1/actors/{slug}/runs" in r.text
