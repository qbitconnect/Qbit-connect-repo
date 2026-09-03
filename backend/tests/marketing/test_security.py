"""Security tests (Phase 7 §54): IDOR, RBAC, leakage, WhatsApp provider."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.services.marketing.providers.whatsapp import WhatsAppProvider


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


@pytest_asyncio.fixture
async def viewer_headers(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "viewer@qbit.example.com", "password": "V13werSecret!Pass"},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestIDORAndAccess:
    async def test_nonexistent_account_404(self, client, admin_headers):
        resp = await client.get(
            f"/api/v1/connections/email/{uuid.uuid4()}", headers=admin_headers
        )
        assert resp.status_code == 404

    async def test_whatsapp_account_not_visible_via_email_route(self, client, admin_headers, session):
        from tests.marketing.helpers import make_mock_account

        account = await make_mock_account(session)
        account.channel = "WHATSAPP"   # same id, different channel → 404, no leak
        await session.commit()
        resp = await client.get(
            f"/api/v1/connections/email/{account.id}", headers=admin_headers
        )
        assert resp.status_code == 404  # channel mismatch = not found (no info leak)

    async def test_viewer_cannot_delete(self, client, viewer_headers):
        resp = await client.delete(
            f"/api/v1/connections/email/{uuid.uuid4()}", headers=viewer_headers
        )
        assert resp.status_code == 403

    async def test_analytics_require_permission(self, client, viewer_headers):
        resp = await client.get(
            f"/api/v1/campaigns/{uuid.uuid4()}/whatsapp/analytics", headers=viewer_headers
        )
        # viewer lacks campaigns.whatsapp.analytics
        assert resp.status_code in (403, 404)

    async def test_suppression_list_requires_permission(self, client, viewer_headers):
        resp = await client.post(
            "/api/v1/suppressions",
            json={"channel": "EMAIL", "address": "x@example.com", "reason": "MANUAL"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestSecretRedaction:
    async def test_account_response_has_no_credential_material(self, client, admin_headers, session):
        from tests.marketing.helpers import make_mock_account

        await make_mock_account(session)
        await session.commit()
        listing = (await client.get("/api/v1/connections/email", headers=admin_headers)).json()
        blob = str(listing)
        assert "mode" not in blob or '"mode": "••••"' in blob
        assert "password" not in blob.lower()
        assert "api_key" not in blob.lower()

    async def test_audit_log_never_contains_credentials(self, client, admin_headers, session, app):
        from sqlalchemy import select

        from app.models.audit import AuditLog

        resp = await client.post(
            "/api/v1/connections/email",
            json={
                "name": "Audit Test",
                "provider": "smtp",
                "sender_email": "audit@example.com",
                "config": {"host": "smtp.example.com", "port": 587, "security": "STARTTLS"},
                "credentials": {"username": "u", "password": "TOPSECRET-PW"},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201
        db = app.state.db
        async with db.session() as s:
            rows = (await s.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(20))).all()
            blob = str([(r.metadata_json, r.action) for r in rows])
            assert "TOPSECRET-PW" not in blob


class TestWhatsAppProvider:
    async def test_signature_verification_valid(self):
        import hashlib
        import hmac

        provider = WhatsAppProvider()
        body = b'{"obj": {}}'
        secret = "app-secret"
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert provider.verify_webhook_signature(
            raw_body=body, signature_header=signature, app_secret=secret
        )

    async def test_signature_verification_invalid(self):
        provider = WhatsAppProvider()
        assert not provider.verify_webhook_signature(
            raw_body=b"{}", signature_header="sha256=bad", app_secret="app-secret"
        )

    async def test_challenge_extraction(self):
        provider = WhatsAppProvider()
        challenge = provider.extract_challenge(
            {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "12345"}
        )
        assert challenge == "12345"

    async def test_recipient_requires_e164(self):
        provider = WhatsAppProvider()
        assert not (await provider.validate_recipient("0987654321")).ok
        assert (await provider.validate_recipient("+919876543210")).ok

    async def test_message_requires_template(self):
        provider = WhatsAppProvider()
        from app.services.marketing.providers.base import OutboundMessage

        outcome = await provider.validate_message(
            OutboundMessage(channel="WHATSAPP", recipient="+919876543210")
        )
        assert not outcome.ok  # template_name missing
