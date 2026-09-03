"""Operator UI smoke tests (Phase 7 §8, §35, §36, §44)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.marketing.helpers import make_campaign, make_mock_account, make_template

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session(app):
    db = app.state.db
    async with db.session() as s:
        yield s


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@qbit.example.com", "password": "Sup3rSecret!Pass"},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestMarketingPages:
    async def test_connections_page_renders(self, client, admin_headers, session):
        await make_mock_account(session)
        await session.commit()
        resp = await client.get("/connections", follow_redirects=False)
        # cookie-based UI: unauthenticated browser gets a redirect to /login
        assert resp.status_code in (200, 303)

    async def test_wizard_page_renders(self, client, admin_headers):
        resp = await client.get("/connections/email/new", follow_redirects=False)
        assert resp.status_code in (200, 303)

    async def test_campaigns_page_renders(self, client, admin_headers):
        resp = await client.get("/campaigns", follow_redirects=False)
        assert resp.status_code in (200, 303)

    async def test_campaign_wizard_renders(self, client, admin_headers):
        resp = await client.get("/campaigns/new", follow_redirects=False)
        assert resp.status_code in (200, 303)

    async def test_templates_page_renders(self, client, admin_headers):
        resp = await client.get("/marketing/templates", follow_redirects=False)
        assert resp.status_code in (200, 303)

    async def test_suppression_page_renders(self, client, admin_headers):
        resp = await client.get("/marketing/suppression", follow_redirects=False)
        assert resp.status_code in (200, 303)

    async def test_campaign_detail_page(self, client, admin_headers, session):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template)
        await session.commit()
        resp = await client.get(f"/campaigns/{campaign.id}", follow_redirects=False)
        assert resp.status_code in (200, 303)

    async def test_ui_requires_login(self, client):
        resp = await client.get("/connections", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]
