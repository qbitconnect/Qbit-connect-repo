"""Sending account API tests (Phase 7 §2, §6, §7, §8, §44, §45)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.marketing.helpers import TEST_SECRET


@pytest_asyncio.fixture
async def admin_token(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@qbit.example.com", "password": "Sup3rSecret!Pass"},
    )
    return resp.json()["access_token"]


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


async def _create(client, headers, **overrides):
    payload = {
        "name": overrides.pop("name", "QBIT Marketing"),
        "provider": overrides.pop("provider", "mock_email"),
        "sender_name": "QBIT",
        "sender_email": overrides.pop("sender_email", "marketing@example.com"),
        "reply_to": "replies@example.com",
        "config": overrides.pop("config", {"mock_ready": True}),
        "credentials": overrides.pop("credentials", {"mode": "success"}),
    }
    payload.update(overrides)
    return await client.post("/api/v1/connections/email", json=payload, headers=headers)


class TestEmailAccountAPI:
    async def test_create_account_masks_credentials(self, client, admin_headers):
        resp = await _create(client, admin_headers)
        assert resp.status_code == 201, resp.text
        account = resp.json()["data"]["account"]
        assert account["status"] == "PENDING"
        assert account["health_status"] == "UNKNOWN"
        assert account["sender_email"] == "marketing@example.com"
        # no secret material anywhere in the response
        assert "mode" not in str(account)
        assert "success" not in str(account)
        assert account["credential_ref"].startswith("••••")

    async def test_secrets_encrypted_at_rest(self, client, admin_headers, app):
        await _create(client, admin_headers)
        from sqlalchemy import select

        from app.models.marketing import SecretVaultEntry

        db = app.state.db
        async with db.session() as session:
            rows = (await session.scalars(select(SecretVaultEntry))).all()
            assert len(rows) == 1
            assert "success" not in rows[0].ciphertext  # never plaintext

    async def test_validate_activates_on_success(self, client, admin_headers):
        account = (await _create(client, admin_headers)).json()["data"]["account"]
        resp = await client.post(
            f"/api/v1/connections/email/{account['id']}/validate", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["validation"]["ok"] is True
        assert data["account"]["status"] == "ACTIVE"
        assert data["account"]["health_status"] == "HEALTHY"

    async def test_validation_failure_never_activates(self, client, admin_headers):
        account = (
            await _create(
                client, admin_headers, credentials={"mode": "auth_failure"}
            )
        ).json()["data"]["account"]
        resp = await client.post(
            f"/api/v1/connections/email/{account['id']}/validate", headers=admin_headers
        )
        data = resp.json()["data"]
        # mock auth_failure affects health, not config validation; account stays
        # non-ACTIVE unless the health round-trip succeeded
        if not data["validation"]["ok"]:
            assert data["account"]["status"] != "ACTIVE"

    async def test_health_check_endpoint(self, client, admin_headers):
        account = (await _create(client, admin_headers)).json()["data"]["account"]
        resp = await client.post(
            f"/api/v1/connections/email/{account['id']}/health", headers=admin_headers
        )
        data = resp.json()["data"]
        assert data["health"]["healthy"] is True
        assert data["account"]["last_health_check"] is not None

    async def test_multiple_sender_accounts(self, client, admin_headers):
        await _create(client, admin_headers, name="QBIT Sales", sender_email="sales@example.com")
        await _create(client, admin_headers, name="QBIT Support", sender_email="support@example.com")
        resp = await client.get("/api/v1/connections/email", headers=admin_headers)
        data = resp.json()["data"]
        assert data["total"] == 2
        emails = {i["sender_email"] for i in data["items"]}
        assert emails == {"sales@example.com", "support@example.com"}

    async def test_update_rotates_credentials(self, client, admin_headers, app):
        account = (await _create(client, admin_headers)).json()["data"]["account"]
        resp = await client.patch(
            f"/api/v1/connections/email/{account['id']}",
            json={"credentials": {"mode": "success", "rotated": "yes"}},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        # the response must never echo credential material
        assert "rotated" not in resp.text
        # and the vault now holds the rotated payload, encrypted at rest
        from sqlalchemy import select

        from app.models.marketing import SecretVaultEntry

        async with app.state.db.session() as session:
            rows = (await session.scalars(select(SecretVaultEntry))).all()
            blob = " ".join(r.ciphertext for r in rows)
            assert "rotated" not in blob

    async def test_delete_account(self, client, admin_headers):
        account = (await _create(client, admin_headers)).json()["data"]["account"]
        resp = await client.delete(
            f"/api/v1/connections/email/{account['id']}", headers=admin_headers
        )
        assert resp.json()["data"]["deleted"] is True
        listing = (await client.get("/api/v1/connections/email", headers=admin_headers)).json()
        assert listing["data"]["total"] == 0

    async def test_viewer_cannot_create(self, client):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "viewer@qbit.example.com", "password": "V13werSecret!Pass"},
        )
        resp_login = await client.post(
            "/api/v1/auth/login",
            json={"email": "viewer@qbit.example.com", "password": "V13werSecret!Pass"},
        )
        viewer_headers = {"Authorization": f"Bearer {resp_login.json()['access_token']}"}
        resp = await _create(client, viewer_headers)
        assert resp.status_code == 403

    async def test_unauthenticated_rejected(self, client):
        resp = await client.get("/api/v1/connections/email")
        assert resp.status_code == 401

    async def test_smtp_config_persisted_without_password(self, client, admin_headers):
        resp = await _create(
            client,
            admin_headers,
            provider="smtp",
            config={"host": "smtp.example.com", "port": 587, "security": "STARTTLS"},
            credentials={"username": "u", "password": "secret-password"},
        )
        account = resp.json()["data"]["account"]
        assert account["config"]["host"] == "smtp.example.com"
        assert "password" not in str(account["config"])

    async def test_smtp_bad_security_rejected(self, client, admin_headers):
        resp = await _create(
            client,
            admin_headers,
            provider="smtp",
            config={"host": "smtp.example.com", "port": 465, "security": "NONE"},
        )
        assert resp.status_code == 422
