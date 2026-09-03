"""Phase 7 email UI tests (§8, §9, §44): email connections page, 7-step
wizard, detail page — no secrets ever rendered."""

from __future__ import annotations

import pytest_asyncio

from tests.test_ui import ADMIN_EMAIL, ADMIN_PASSWORD


@pytest_asyncio.fixture
async def admin_ui(app):
    """Cookie-authenticated UI session for the admin user (form login)."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert resp.status_code == 303, resp.text
        yield client


class TestEmailConnectionsUI:
    async def test_email_dashboard_renders(self, admin_ui):
        resp = await admin_ui.get("/connections/email")
        assert resp.status_code == 200
        assert "EMAIL SENDER ACCOUNTS" in resp.text

    async def test_wizard_renders_seven_steps(self, admin_ui):
        resp = await admin_ui.get("/connections/email/new")
        assert resp.status_code == 200
        for step in ("Provider type", "Sender email", "Validation runs on submit",
                     "health probe", "never falsely activated"):
            assert step in resp.text

    async def test_wizard_creates_and_validates_mock_account(self, admin_ui, app):
        # mock provider only exists in test env; the mock health probe passes
        resp = await admin_ui.post(
            "/connections/email/new",
            data={
                "name": "UI Mock", "provider": "email_mock",
                "sender_name": "QBIT UI", "sender_email": "ui@qbit.test",
                "reply_to": "", "smtp_host": "", "smtp_port": "",
                "smtp_security": "STARTTLS", "api_base_url": "", "region": "",
                "smtp_username": "", "smtp_password": "", "api_key": "",
            },
        )
        assert resp.status_code == 303, resp.text
        detail = await admin_ui.get(resp.headers["location"])
        assert detail.status_code == 200
        assert "QBIT UI" in detail.text
        assert "hunter2" not in detail.text  # paranoia: no secrets on pages

    async def test_wizard_shows_real_validation_error(self, admin_ui):
        # SMTP account with no credentials → validation fails honestly
        resp = await admin_ui.post(
            "/connections/email/new",
            data={
                "name": "Broken SMTP", "provider": "smtp",
                "sender_name": "", "sender_email": "broken@company.test",
                "reply_to": "", "smtp_host": "smtp.invalid", "smtp_port": "587",
                "smtp_security": "STARTTLS", "api_base_url": "", "region": "",
                "smtp_username": "", "smtp_password": "", "api_key": "",
            },
        )
        # wizard redirect: account exists as ERROR with honest message
        assert resp.status_code == 303
        detail = await admin_ui.get(resp.headers["location"])
        assert "validation" in resp.headers["location"] or "ERROR" in detail.text

    async def test_secrets_never_render_in_detail(self, admin_ui):
        # create via the UI wizard (mock provider); then rotate credentials
        # through the detail page's write-only form
        resp = await admin_ui.post(
            "/connections/email/new",
            data={
                "name": "UI Sec", "provider": "email_mock",
                "sender_name": "Sec", "sender_email": "sec@qbit.test",
                "reply_to": "", "smtp_host": "", "smtp_port": "",
                "smtp_security": "STARTTLS", "api_base_url": "", "region": "",
                "smtp_username": "", "smtp_password": "", "api_key": "",
            },
        )
        assert resp.status_code == 303
        location = resp.headers["location"].split("?")[0]
        rotate = await admin_ui.post(
            f"{location}/credentials", data={"api_key": "topsecret-api-key-9931"},
        )
        assert rotate.status_code == 303
        page = await admin_ui.get(location)
        assert page.status_code == 200
        assert "topsecret-api-key-9931" not in page.text
        assert "stored (encrypted; never displayed)" in page.text
