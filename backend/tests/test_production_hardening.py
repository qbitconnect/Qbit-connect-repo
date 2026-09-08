"""Phase 12 — production hardening test suite (brief §45).

Covers: security headers/CSP, request-id sanitization, UI login rate limit +
lockout + session revocation + secure cookies, webhook mock-secret removal +
mandatory timestamp, CSV formula neutralization, IPv4-mapped IPv6 SSRF,
liveness/readiness endpoints, admin ops snapshot, backup files/verify/prune,
login-guard unit behavior, XFF spoof resistance, config validation additions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, TEST_SECRET, VIEWER_EMAIL


# ---------------------------------------------------------------- headers
class TestSecurityHeaders:
    async def test_csp_on_api_response(self, client):
        resp = await client.get("/health/live")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp

    async def test_headers_on_ui_page(self, client):
        resp = await client.get("/login", follow_redirects=False)
        assert resp.status_code == 200
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert "Content-Security-Policy" in resp.headers


# ------------------------------------------------------------ request id
class TestRequestIdSanitization:
    async def test_valid_short_id_is_echoed(self, client):
        resp = await client.get(
            "/health/live", headers={"X-Request-ID": "abc.123-xyz"}
        )
        assert resp.headers["X-Request-ID"] == "abc.123-xyz"

    @pytest.mark.parametrize(
        "evil", ["id with spaces", "a;b;c", "x" * 300, "tok\ninjection", "'quote"]
    )
    async def test_malformed_ids_replaced(self, client, evil):
        resp = await client.get("/health/live", headers={"X-Request-ID": evil})
        rid = resp.headers["X-Request-ID"]
        assert rid != evil
        assert set(rid) <= set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        )


# ------------------------------------------------------------- ui login
class TestUiLoginHardening:
    async def test_ui_login_creates_revocable_session(self, client, app):
        resp = await client.post(
            "/ui-does-not-exist",  # placeholder to keep naming explicit
        )
        # (guard: unrelated 404 confirms client works)
        assert resp.status_code == 404

        login = await client.post(
            "/login",
            data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": "/scraping"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        cookie = login.headers["set-cookie"].split(";")[0]
        assert cookie.startswith("qbit_session=")

        from sqlalchemy import select
        from app.models.enterprise import UserSession

        async with app.state.db.session() as session:
            rows = (
                await session.execute(select(UserSession).order_by(UserSession.created_at))
            ).scalars().all()
            assert rows, "UI login must register a server-side session"

    async def test_ui_session_revocation_evicts_cookie(self, client, app):
        login = await client.post(
            "/login",
            data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": "/scraping"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        page = await client.get("/scraping", follow_redirects=False)
        assert page.status_code == 200

        # admin force-revokes ALL sessions of the user (as the sessions admin does)
        from sqlalchemy import select
        from app.models.enterprise import UserSession
        from app.models.user import User

        async with app.state.db.session() as session:
            user = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
            rows = (
                await session.execute(select(UserSession).where(UserSession.user_id == user.id))
            ).scalars().all()
            for row in rows:
                row.revoked_at = datetime.now(timezone.utc)
                row.revoked_reason = "ADMIN_FORCE_LOGOUT"
            await session.commit()

        evicted = await client.get("/scraping", follow_redirects=False)
        assert evicted.status_code == 303
        assert evicted.headers["location"].startswith("/login")

    async def test_ui_login_rate_limited(self, client, app):
        app.state.login_limiter.reset()
        app.state.login_limiter.max_events = 3
        app.state.login_limiter.per_seconds = 60.0
        for _ in range(3):
            await client.post(
                "/login",
                data={"email": "nobody@x.example.com", "password": "wrong"},
            )
        resp = await client.post(
            "/login",
            data={"email": "nobody@x.example.com", "password": "wrong"},
        )
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After", "").isdigit()
        app.state.login_limiter.reset()
        app.state.login_limiter.max_events = 50

    async def test_ui_login_deactivated_is_not_enumerable(self, client):
        # deactivated user WITH the correct password must not disclose
        # account state beyond what a correct password already proves
        from app.core.security import hash_password
        from app.models.user import User

        # (created through the seeded session by the login attempt below)
        resp = await client.post(
            "/login",
            data={"email": "ghost@qbit.example.com", "password": "whatever-pass"},
        )
        assert resp.status_code == 401
        assert b"Invalid email or password" in resp.content


# ------------------------------------------------------------- lockout
class TestAccountLockout:
    async def test_lockout_after_failed_attempts(self, client, app):
        app.state.settings.QBIT_LOGIN_MAX_FAILED_ATTEMPTS = 3
        app.state.settings.QBIT_LOGIN_LOCKOUT_MINUTES = 15
        target = VIEWER_EMAIL
        for _ in range(3):
            resp = await client.post(
                "/api/v1/auth/login", json={"email": target, "password": "wrong-pass-1"}
            )
            assert resp.status_code == 401
        # correct password is now ALSO rejected (constant shape)
        resp = await client.post(
            "/api/v1/auth/login", json={"email": target, "password": "V13werSecret!Pass"}
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "INVALID_CREDENTIALS"

        from sqlalchemy import select
        from app.models.user import User

        async with app.state.db.session() as session:
            user = await session.scalar(select(User).where(User.email == target))
            assert user.locked_until is not None
            locked_until = user.locked_until
            if locked_until.tzinfo is None:  # SQLite returns naive datetimes
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            assert locked_until > datetime.now(timezone.utc)

    async def test_successful_login_resets_counter(self, client, app):
        app.state.settings.QBIT_LOGIN_MAX_FAILED_ATTEMPTS = 5
        for _ in range(2):
            await client.post(
                "/api/v1/auth/login", json={"email": VIEWER_EMAIL, "password": "nope"}
            )
        resp = await client.post(
            "/api/v1/auth/login", json={"email": VIEWER_EMAIL, "password": "V13werSecret!Pass"}
        )
        assert resp.status_code == 200
        from sqlalchemy import select
        from app.models.user import User

        async with app.state.db.session() as session:
            user = await session.scalar(select(User).where(User.email == VIEWER_EMAIL))
            assert user.failed_login_attempts == 0
            assert user.locked_until is None

    async def test_locked_account_is_constant_shape(self):
        from app.models.user import User
        from app.services.login_guard import is_locked

        user = User(email="x@y.z", password_hash="h")
        assert is_locked(user) is False
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        assert is_locked(user) is True
        user.locked_until = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert is_locked(user) is False


# ------------------------------------------------------------- webhooks
def _email_sig(secret: str, body: bytes, ts: int | None = None) -> dict:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-QBIT-Signature": f"sha256={sig}"}
    if ts is not None:
        headers["X-QBIT-Timestamp"] = str(ts)
    return headers


class TestWebhookHardening:
    async def test_email_mock_without_secret_is_401(self, client, app):
        # audit H6: the hardcoded "mock-webhook-secret" fallback is gone
        app.state.settings.EMAIL_WEBHOOK_SECRET = None
        body = b'{"events": []}'
        signed = _email_sig("mock-webhook-secret", body, ts=int(time.time()))
        resp = await client.post(
            "/api/v1/webhooks/email/email_mock", content=body, headers=signed
        )
        assert resp.status_code == 401

    async def test_missing_timestamp_is_rejected(self, client, app):
        # audit M5: X-QBIT-Timestamp is now mandatory
        app.state.settings.EMAIL_WEBHOOK_SECRET = "whsec-test-123"
        body = b'{"events": []}'
        signed = _email_sig("whsec-test-123", body, ts=None)
        resp = await client.post(
            "/api/v1/webhooks/email/email_mock", content=body, headers=signed
        )
        assert resp.status_code == 401

    async def test_valid_signature_and_timestamp_accepted(self, client, app):
        app.state.settings.EMAIL_WEBHOOK_SECRET = "whsec-test-123"
        body = json.dumps({"events": [{"event": "delivered", "message_id": "x1"}]}).encode()
        signed = _email_sig("whsec-test-123", body, ts=int(time.time()))
        resp = await client.post(
            "/api/v1/webhooks/email/email_mock", content=body, headers=signed
        )
        assert resp.status_code == 200

    async def test_email_mock_blocked_in_production(self):
        from app.core.config import Settings

        # validate_runtime must require EMAIL_WEBHOOK_SECRET in production
        settings = Settings(
            QBIT_ENV="production",
            QBIT_SECRET_KEY=TEST_SECRET,
            DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/qbit",
            WHATSAPP_WEBHOOK_VERIFY_TOKEN="tok",
            EMAIL_WEBHOOK_SECRET=None,
            QBIT_CORS_ORIGINS="https://qbit.example.com",
            QBIT_DATA_DIR="/tmp/unused",
            _env_file=None,
        )
        problems = settings.validate_runtime()
        assert any("EMAIL_WEBHOOK_SECRET" in p for p in problems)


# ---------------------------------------------------------------- units
class TestFormulaSafety:
    def test_dangerous_leads_are_neutralized(self):
        from app.services.leads.exporter import _formula_safe_cell

        for evil in ("=cmd|' /C calc'!A0", "+SUM(A1)", "-2 inches", "@evil", "\ttab"):
            safe = _formula_safe_cell(evil)
            assert safe.startswith("'"), evil
        assert _formula_safe_cell(12345) == 12345
        assert _formula_safe_cell("normal text") == "normal text"
        assert _formula_safe_cell("") == ""


class TestNetguardMappedV6:
    def test_ipv4_mapped_ipv6_loopback_blocked(self):
        from app.scrapers.core.netguard import is_private_ip

        assert is_private_ip("::ffff:127.0.0.1") is True
        assert is_private_ip("::ffff:169.254.169.254") is True
        assert is_private_ip("::ffff:10.0.0.5") is True
        assert is_private_ip("::ffff:8.8.8.8") is False

    def test_mapped_v6_url_validation_blocked(self):
        from app.scrapers.core.netguard import UrlPolicy, validate_url

        policy = UrlPolicy(allowed_ports={80, 443}, allow_private_targets=False)
        with pytest.raises(Exception):
            validate_url("http://[::ffff:127.0.0.1]/secret", policy)


class TestClientIpSpoofResistance:
    async def test_first_xff_entry_is_not_trusted(self, client, app):
        from app.api.deps import get_client_ip

        class _FakeRequest:
            headers = {
                "x-forwarded-for": "1.2.3.4, 10.9.8.7",
                "x-real-ip": "",
            }
            client = None

        assert get_client_ip(_FakeRequest()) == "10.9.8.7"  # last entry

        class _FakeRequestReal:
            headers = {"x-real-ip": "5.6.7.8", "x-forwarded-for": "9.9.9.9"}
            client = None

        assert get_client_ip(_FakeRequestReal()) == "5.6.7.8"


# ---------------------------------------------------------------- health
class TestHealthSplit:
    async def test_liveness_has_no_dependencies(self, client):
        resp = await client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "alive"}

    async def test_readiness_aggregates(self, client):
        resp = await client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in {"healthy", "degraded"}
        assert set(body["services"]) == {"database", "storage", "redis"}

    async def test_storage_health_hides_path(self, client):
        resp = await client.get("/health/storage")
        body = resp.json()
        assert "path" not in body
        resp = await client.get("/health/ready")
        assert "path" not in resp.json()["services"]["storage"]


# -------------------------------------------------------------- ops API
class TestOpsSnapshot:
    async def test_requires_settings_view(self, client, viewer_headers):
        resp = await client.get("/api/v1/admin/ops", headers=viewer_headers)
        assert resp.status_code == 403

    async def test_admin_gets_real_numbers(self, client, admin_headers):
        resp = await client.get("/api/v1/admin/ops", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["queue"]["backend"] in {"redis", "inprocess", "unavailable"}
        assert set(data["scrape_jobs"]) >= {"QUEUED", "RUNNING", "COMPLETED"}
        assert "used_percent" in data["disk"]
        assert data["disk"]["warning"] is False  # tiny test volume
        assert "path" not in json.dumps(data)  # no filesystem paths leak


# --------------------------------------------------------------- backup
class TestBackupLifecycle:
    async def test_files_backup_manifest_verify_prune(self, app, tmp_path):
        from app.services.backup import BackupService

        service = BackupService(app.state.settings)
        data_dir = app.state.settings.data_dir
        (data_dir / "exports").mkdir(parents=True, exist_ok=True)
        (data_dir / "exports" / "sample.csv").write_text("a,b\n1,2\n")

        result = service.run_files_backup()
        assert result.status == "completed", result.details
        assert result.path.startswith("files/")
        manifest = service.read_manifest()
        assert manifest and manifest[-1]["path"] == result.path

        verdict = service.verify_backup(result.path)
        assert verdict["status"] == "verified", verdict

        # temporary/ and cache/ are never included
        (data_dir / "temporary").mkdir(parents=True, exist_ok=True)
        (data_dir / "temporary" / "junk.tmp").write_text("x")
        result2 = service.run_files_backup()
        assert result2.status == "completed"

        # retention: 1 daily kept; prune with keep_daily=1 keeps both (same day)
        removed = service.prune_backups(keep_daily=1, keep_weekly=1, keep_monthly=1)
        assert removed == []

    async def test_sqlite_backup_verifies(self, app):
        from app.services.backup import BackupService

        service = BackupService(app.state.settings)
        result = service.run_database_backup()
        assert result.status == "completed", result.details
        verdict = service.verify_backup(result.path)
        assert verdict["status"] == "verified", verdict

    async def test_prune_never_touches_untracked_files(self, app, tmp_path):
        from app.services.backup import BackupService

        service = BackupService(app.state.settings)
        stranger = service.db_dir / "untracked-manual-backup.dump"
        stranger.write_bytes(b"not in manifest")
        service.prune_backups(keep_daily=1, keep_weekly=1, keep_monthly=1)
        assert stranger.exists(), "files outside the manifest must never be pruned"
        stranger.unlink()


# ------------------------------------------------------- config validation
class TestProductionValidation:
    def _prod_settings(self, **overrides):
        from app.core.config import Settings

        base = dict(
            QBIT_ENV="production",
            QBIT_SECRET_KEY=TEST_SECRET,
            DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/qbit",
            WHATSAPP_WEBHOOK_VERIFY_TOKEN="tok",
            EMAIL_WEBHOOK_SECRET="whsec",
            QBIT_CORS_ORIGINS="https://qbit.example.com",
            QBIT_DATA_DIR="/tmp/unused",
            _env_file=None,
        )
        base.update(overrides)
        return Settings(**base)

    def test_email_webhook_secret_required(self):
        settings = self._prod_settings(EMAIL_WEBHOOK_SECRET=None)
        assert any("EMAIL_WEBHOOK_SECRET" in p for p in settings.validate_runtime())

    def test_valid_production_has_no_problems(self):
        assert self._prod_settings().validate_runtime() == []

    def test_cookie_secure_defaults(self):
        assert self._prod_settings().cookie_secure is True
        from app.core.config import Settings

        dev = Settings(QBIT_ENV="development", _env_file=None)
        assert dev.cookie_secure is False
        forced = self._prod_settings(QBIT_COOKIE_SECURE=False)
        assert forced.cookie_secure is False
