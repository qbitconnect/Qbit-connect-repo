"""Phase 5 marketing test fixtures — isolated per-test DB (no production data)."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, VIEWER_EMAIL, VIEWER_PASSWORD


@pytest_asyncio.fixture
async def seeded_db(app):
    """Session bound to the SAME database the API client's app uses — service
    and API tests see identical data (no second DB)."""
    db = app.state.db
    async with db.session() as session:
        yield session


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
    token = await _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def viewer_headers(client):
    token = await _login(client, VIEWER_EMAIL, VIEWER_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


def make_lead(**overrides):
    """Build a Lead row with opt-in + contact defaults."""
    from app.models.scrape import Lead

    fields = {
        "business_name": "Acme Pvt Ltd",
        "contact_name": "Ravi Patel",
        "email": "ravi@acme.test",
        "phone": "+919876543210",
        "city": "Surat",
        "state": "Gujarat",
        "source": "test",
        "source_type": "manual",
        "status": "NEW",
    }
    fields.update(overrides)
    lead = Lead(**fields)
    meta = dict(lead.metadata_json or {})
    meta.setdefault("marketing_opt_in", True)
    lead.metadata_json = meta
    return lead


async def seed_leads(session, count: int, **common) -> list:
    from app.services.scraping.lead_keys import normalize_email, normalize_phone

    leads = []
    for i in range(count):
        email = common.get("email", f"lead{i}@acme.test")
        phone = common.get("phone", f"+9198765432{i:02d}")
        lead = make_lead(
            business_name=common.get("business_name", f"Biz {i} Pvt Ltd"),
            email=email,
            email_norm=normalize_email(email),
            phone=phone,
            phone_norm=normalize_phone(phone),
            city=common.get("city", "Surat"),
        )
        session.add(lead)
        leads.append(lead)
    await session.commit()
    return leads


async def seed_account(session, *, provider: str = "mock", channel: str = "WHATSAPP",
                       configured: bool = True, status: str = "ACTIVE") -> "object":
    from app.models.marketing import AccountStatus, SendingAccount

    account = SendingAccount(
        name=f"Test account {provider}",
        channel=channel,
        provider=provider,
        identifier="+910000000000" if channel != "EMAIL" else "sender@qbit.test",
        display_identifier="+91 00000 00000" if channel != "EMAIL" else "sender@qbit.test",
        status=status or AccountStatus.ACTIVE,
        config_metadata={"configured": configured} if configured else {},
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def seed_template(session, *, channel: str = "WHATSAPP",
                        body: str = "Hi {{first_name}} from {{business_name}}!",
                        status: str = "ACTIVE") -> "object":
    from app.models.marketing import CampaignTemplate

    template = CampaignTemplate(
        name=f"Template {channel}", channel=channel, body=body,
        status=status, variables=["first_name", "business_name"],
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template
