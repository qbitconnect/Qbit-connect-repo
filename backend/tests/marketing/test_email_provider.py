"""Phase 7 email provider unit tests (§53: configuration, SMTP connection,
Email API adapter, sender validation, normalization, error classification)."""

from __future__ import annotations

import httpx
import pytest

from app.services.marketing.email_normalization import EmailNormalizationService
from app.services.marketing.providers.base import ErrorClass
from app.services.marketing.providers.email import (
    EmailErrorNormalizer,
    EmailEventNormalizer,
    EmailMockProvider,
    GenericEmailAPIProvider,
    SMTPProvider,
)
from app.services.marketing.providers.email.errors import (
    AUTHENTICATION_ERROR,
    CONNECTION_ERROR,
    INVALID_RECIPIENT,
    PROVIDER_UNAVAILABLE,
    RATE_LIMITED,
    UNKNOWN_STATE,
)


# ------------------------------------------------------- email normalization
class TestEmailNormalization:
    def test_trims_and_lowercases_domain(self):
        ok, normalized, reason = EmailNormalizationService.normalize("  Ravi.Patel@ACME.COM  ")
        assert ok and normalized == "Ravi.Patel@acme.com" and reason == "OK"

    def test_preserves_local_part_case(self):
        ok, normalized, _ = EmailNormalizationService.normalize("Mixed.Case@Example.COM")
        assert ok and normalized == "Mixed.Case@example.com"

    def test_rejects_obviously_malformed(self):
        for bad in ("", "nope", "a@b", "a b@c.com", "a@@b.com", "a@-bad.com",
                    ".dot@x.com", "x.@y.com", "do..uble@x.com", None, "a@b..com"):
            ok, normalized, reason = EmailNormalizationService.normalize(bad)
            assert not ok and normalized is None
            assert reason in ("INVALID", "EMPTY")

    def test_no_unsafe_transformation_of_valid_address(self):
        original = "quoted+tag@sub.example.co.uk"
        ok, normalized, _ = EmailNormalizationService.normalize(original)
        assert ok and normalized == "quoted+tag@sub.example.co.uk"


# ------------------------------------------------------- error normalization
class TestEmailErrorNormalizer:
    def setup_method(self):
        self.normalizer = EmailErrorNormalizer()

    def test_smtp_5xx_user_unknown_is_permanent(self):
        out = self.normalizer.normalize_smtp(smtp_code=550, message="User unknown in virtual mailbox table")
        assert out.code == INVALID_RECIPIENT and out.error_class == ErrorClass.PERMANENT

    def test_smtp_5xx_auth_is_permanent_auth_error(self):
        out = self.normalizer.normalize_smtp(smtp_code=535, message="5.7.8 Authentication credentials invalid")
        assert out.code == AUTHENTICATION_ERROR and out.error_class == ErrorClass.PERMANENT

    def test_smtp_4xx_is_transient(self):
        out = self.normalizer.normalize_smtp(smtp_code=452, message="4.2.2 The email account that you tried to reach is over quota")
        assert out.error_class == ErrorClass.TRANSIENT
        assert out.code in (RATE_LIMITED, "MAILBOX_UNAVAILABLE")

    def test_smtp_421_rate_limited(self):
        out = self.normalizer.normalize_smtp(smtp_code=421, message="421 Too many messages, rate limited")
        assert out.code == RATE_LIMITED and out.error_class == ErrorClass.TRANSIENT

    def test_connection_refused_is_transient_connection_error(self):
        out = self.normalizer.normalize_smtp(exception=ConnectionRefusedError("connection refused"))
        assert out.code == CONNECTION_ERROR and out.error_class == ErrorClass.TRANSIENT

    def test_timeout_is_uncertain_and_never_retried(self):
        out = self.normalizer.normalize_smtp(exception=TimeoutError("timed out"))
        assert out.code == UNKNOWN_STATE
        assert out.error_class == ErrorClass.PERMANENT  # queue never auto-retries

    def test_dns_failure_is_transient_unavailable(self):
        out = self.normalizer.normalize_smtp(exception=OSError("Name or service not known (getaddrinfo failed)"))
        assert out.code == PROVIDER_UNAVAILABLE and out.error_class == ErrorClass.TRANSIENT

    def test_api_401_is_auth_error(self):
        out = self.normalizer.normalize_api(status_code=401, payload={"error": "unauthorized"})
        assert out.code == AUTHENTICATION_ERROR and out.error_class == ErrorClass.PERMANENT

    def test_api_429_carries_retry_after(self):
        out = self.normalizer.normalize_api(status_code=429, payload={"error": "slow down", "retry_after": "30"})
        assert out.code == RATE_LIMITED and out.retry_after_seconds == 30.0

    def test_api_5xx_is_transient_unavailable(self):
        out = self.normalizer.normalize_api(status_code=503, payload={"error": "upstream unavailable"})
        assert out.code == PROVIDER_UNAVAILABLE and out.error_class == ErrorClass.TRANSIENT

    def test_messages_never_contain_secrets(self):
        out = self.normalizer.normalize_smtp(smtp_code=535, message="535 auth failed for super-secret-password")
        assert "super-secret-password" not in out.message


# --------------------------------------------------------- provider contract
class TestProviderValidation:
    async def test_smtp_configuration_problems(self):
        provider = SMTPProvider()
        problems = await provider.validate_configuration({"configured": True})
        assert any("smtp_host" in p for p in problems)
        assert any("sender_email" in p for p in problems)
        assert any("credential" in p.lower() for p in problems)

    async def test_smtp_configuration_ok(self):
        provider = SMTPProvider()
        problems = await provider.validate_configuration({
            "configured": True, "smtp_host": "smtp.example.com",
            "smtp_port": "587", "smtp_security": "STARTTLS",
            "sender_email": "sales@company.com", "credential_ref": "email:abc",
        })
        assert problems == []

    async def test_smtp_rejects_bad_security_mode(self):
        provider = SMTPProvider()
        problems = await provider.validate_configuration({
            "configured": True, "smtp_host": "smtp.example.com",
            "sender_email": "s@x.com", "credential_ref": "c",
            "smtp_security": "telnet",
        })
        assert any("smtp_security" in p for p in problems)

    async def test_smtp_recipient_validation(self):
        provider = SMTPProvider()
        assert await provider.validate_recipient("ravi@acme.test") is True
        assert await provider.validate_recipient("not-an-email") is False

    async def test_smtp_message_requires_subject(self):
        provider = SMTPProvider()
        problems = await provider.validate_message(subject="", body="hello")
        assert any("subject" in p.lower() for p in problems)
        assert await provider.validate_message(subject="Hi", body="hello") == []

    async def test_email_api_configuration_problems(self):
        provider = GenericEmailAPIProvider()
        problems = await provider.validate_configuration({"configured": True})
        assert any("api_base_url" in p for p in problems)
        problems = await provider.validate_configuration({
            "configured": True, "api_base_url": "ftp://bad", "credential_ref": "x",
        })
        assert any("http(s)" in p for p in problems)

    async def test_email_mock_scenarios(self):
        provider = EmailMockProvider()
        config = {"configured": True, "sender_email": "m@qbit.test"}
        ok = await provider.send(
            account_config=config, recipient_address="a@b.test",
            subject="s", body="b", idempotency_key="k1",
        )
        assert ok.ok and ok.provider_message_id
        again = await provider.send(
            account_config=config, recipient_address="a@b.test",
            subject="s", body="b", idempotency_key="k1",
        )
        assert again.provider_message_id == ok.provider_message_id  # idempotent id
        fail = await provider.send(
            account_config=config, recipient_address="a@b.test",
            subject="s", body="b", idempotency_key="k2", metadata={"scenario": "permanent_failure"},
        )
        assert not fail.ok and fail.error_class == ErrorClass.PERMANENT
        slow = await provider.send(
            account_config=config, recipient_address="a@b.test",
            subject="s", body="b", idempotency_key="k3", metadata={"scenario": "rate_limited"},
        )
        assert not slow.ok and slow.error_code == RATE_LIMITED

    async def test_email_mock_health(self):
        provider = EmailMockProvider()
        healthy = await provider.health_check({"configured": True, "sender_email": "m@qbit.test"})
        assert healthy["health"] == "HEALTHY"
        bad = await provider.health_check({"scenario": "unavailable"})
        assert bad["health"] == "UNHEALTHY"


# ------------------------------------------------------------ event normalizer
class TestEmailEventNormalizer:
    def setup_method(self):
        self.normalizer = EmailEventNormalizer()

    def test_normalizes_delivered(self):
        out = self.normalizer.normalize({
            "provider_event_id": "evt-1", "message_id": "m-1",
            "event": "delivered", "timestamp": 1756000000,
        })
        assert out and out["event_type"] == "MESSAGE_DELIVERED"
        assert out["provider_message_id"] == "m-1"

    def test_bounce_classification(self):
        hard = self.normalizer.normalize({"message_id": "m-1", "event": "bounced",
                                          "bounce": {"type": "hard"}})
        soft = self.normalizer.normalize({"message_id": "m-2", "event": "bounced",
                                          "bounce": {"type": "soft"}})
        assert hard["metadata"]["bounce_type"] == "HARD_BOUNCE"
        assert soft["metadata"]["bounce_type"] == "SOFT_BOUNCE"

    def test_unclassified_bounce_is_conservatively_soft(self):
        out = self.normalizer.normalize({"message_id": "m-3", "event": "bounced"})
        assert out["metadata"]["bounce_type"] == "SOFT_BOUNCE"

    def test_complaint_and_unsubscribed(self):
        assert self.normalizer.normalize({"message_id": "m", "event": "complaint"})
        assert self.normalizer.normalize({"message_id": "m", "event": "unsubscribed"})

    def test_synthetic_event_id_for_dedupe(self):
        out = self.normalizer.normalize({"message_id": "m-9", "event": "opened"})
        assert out["provider_event_id"] == "m-9:opened"

    def test_rejects_unknown_events(self):
        assert self.normalizer.normalize({"event": "teleported", "message_id": "x"}) is None
        assert self.normalizer.normalize({"event": "delivered"}) is None  # no message id
        assert self.normalizer.normalize("not a dict") is None


# --------------------------------------------------------- SMTP send (hooks)
class TestSMTPSendWithHook:
    class _FakeSession:
        def __init__(self):
            self.sent = []

        def send(self, sender, recipient, message):
            self.sent.append((sender, recipient, message))

    def _provider(self):
        fake = self._FakeSession()

        def factory(cfg):
            return fake

        return SMTPProvider(connect_factory=factory), fake

    async def test_send_success_uses_individual_recipient(self):
        provider, fake = self._provider()
        result = await provider.send(
            account_config={"sender_email": "sales@company.com", "sender_name": "QBIT",
                            "smtp_host": "smtp.company.com", "smtp_security": "STARTTLS",
                            "smtp_port": 587, "configured": True},
            recipient_address="ravi@acme.test", subject="Hello", body="plain",
            idempotency_key="c:r:1",
            template={"html": "<p>Hello</p>", "text": "Hello", "headers": {}},
        )
        assert result.ok and result.provider_message_id.startswith("<")
        assert len(fake.sent) == 1
        sender, recipient, message = fake.sent[0]
        assert recipient == "ravi@acme.test"  # one To:, never CC
        assert message["Subject"] == "Hello"
        assert message["From"] == "QBIT <sales@company.com>"

    async def test_header_injection_rejected(self):
        provider, fake = self._provider()
        result = await provider.send(
            account_config={"sender_email": "sales@company.com", "smtp_host": "x",
                            "configured": True},
            recipient_address="ravi@acme.test", subject="Hello\r\nBcc: victim@x.com",
            body="plain", idempotency_key="k",
        )
        assert not result.ok and result.error_code == "MESSAGE_REJECTED"
        assert fake.sent == []

    async def test_invalid_recipient_permanent(self):
        provider, _ = self._provider()
        result = await provider.send(
            account_config={"sender_email": "sales@company.com", "smtp_host": "x",
                            "configured": True},
            recipient_address="not-an-email", subject="s", body="b", idempotency_key="k",
        )
        assert not result.ok and result.error_code == INVALID_RECIPIENT
        assert result.error_class == ErrorClass.PERMANENT

    async def test_smtp_recipients_refused_is_permanent(self):
        import smtplib

        class _Refusing:
            def send(self, sender, recipient, message):
                raise smtplib.SMTPRecipientsRefused({recipient: (550, b"User unknown")})

        provider = SMTPProvider(connect_factory=lambda cfg: _Refusing())
        result = await provider.send(
            account_config={"sender_email": "sales@company.com", "smtp_host": "x",
                            "configured": True},
            recipient_address="dead@acme.test", subject="s", body="b", idempotency_key="k",
        )
        assert not result.ok and result.error_code == INVALID_RECIPIENT
        assert result.error_class == ErrorClass.PERMANENT


# ------------------------------------------------------- Email API adapter
class TestEmailAPIAdapter:
    class _Transport(httpx.AsyncBaseTransport):
        """Scripted httpx transport (no network)."""

        def __init__(self, handler):
            self.handler = handler

        async def handle_async_request(self, request):
            return self.handler(request)

    def _provider(self, handler):
        return GenericEmailAPIProvider(transport_factory=lambda key: self._Transport(handler))

    async def test_send_success(self):

        def handler(request):
            import json as _json
            assert request.url.path.endswith("/messages")
            assert request.headers["authorization"].startswith("Bearer ")
            return httpx.Response(200, json={"id": "prov-123"})

        provider = self._provider(handler)
        result = await provider.send(
            account_config={"api_base_url": "https://api.example.com",
                            "sender_email": "sales@company.com", "configured": True},
            recipient_address="ravi@acme.test", subject="Hello", body="plain",
            idempotency_key="c:r:1", credentials={"api_key": "sk-test"},
            template={"html": "<p>H</p>", "text": "H"},
        )
        assert result.ok and result.provider_message_id == "prov-123"

    async def test_send_invalid_recipient_permanent(self):

        def handler(request):
            return httpx.Response(400, json={"error": "invalid recipient address"})

        provider = self._provider(handler)
        result = await provider.send(
            account_config={"api_base_url": "https://api.example.com",
                            "sender_email": "sales@company.com", "configured": True},
            recipient_address="bad@x.test", subject="s", body="b",
            idempotency_key="k", credentials={"api_key": "sk"},
        )
        assert not result.ok and result.error_code == INVALID_RECIPIENT
        assert result.error_class == ErrorClass.PERMANENT

    async def test_rate_limited_respects_retry_after(self):

        def handler(request):
            return httpx.Response(429, json={"error": "slow down", "retry_after": 42})

        provider = self._provider(handler)
        result = await provider.send(
            account_config={"api_base_url": "https://api.example.com",
                            "sender_email": "sales@company.com", "configured": True},
            recipient_address="a@b.test", subject="s", body="b",
            idempotency_key="k", credentials={"api_key": "sk"},
        )
        assert result.error_code == RATE_LIMITED
        assert result.metadata["retry_after_seconds"] == 42

    async def test_transport_unreachable_is_transient(self):

        def handler(request):
            raise httpx.ConnectError("connection refused")

        provider = self._provider(handler)
        result = await provider.send(
            account_config={"api_base_url": "https://api.example.com",
                            "sender_email": "sales@company.com", "configured": True},
            recipient_address="a@b.test", subject="s", body="b",
            idempotency_key="k", credentials={"api_key": "sk"},
        )
        assert result.error_code == PROVIDER_UNAVAILABLE
        assert result.error_class == ErrorClass.TRANSIENT

    async def test_health_check_states(self):

        def ok_handler(request):
            return httpx.Response(200, json={"status": "ok"})

        def bad_handler(request):
            return httpx.Response(401, json={"error": "nope"})

        provider = self._provider(ok_handler)
        health = await provider.health_check(
            {"api_base_url": "https://api.example.com"}, {"api_key": "sk"})
        assert health["health"] == "HEALTHY"
        provider = self._provider(bad_handler)
        health = await provider.health_check(
            {"api_base_url": "https://api.example.com"}, {"api_key": "sk"})
        assert health["health"] == "UNHEALTHY"
        assert "credentials" in health["detail"]

    async def test_header_injection_blocked(self):
        provider = self._provider(lambda r: None)
        result = await provider.send(
            account_config={"api_base_url": "https://api.example.com",
                            "sender_email": "sales@company.com", "configured": True},
            recipient_address="a@b.test", subject="x\nBcc: v@x.com", body="b",
            idempotency_key="k", credentials={"api_key": "sk"},
        )
        assert not result.ok and result.error_code == "MESSAGE_REJECTED"
