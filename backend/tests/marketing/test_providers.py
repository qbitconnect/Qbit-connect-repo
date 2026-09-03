"""Provider adapter + error normalization tests (Phase 7 §1, §22, §23, §55)."""

import pytest

from app.services.marketing.providers import get_provider, reset_registry
from app.services.marketing.providers.base import OutboundMessage
from app.services.marketing.providers.errors import (
    AUTHENTICATION_ERROR,
    CONNECTION_ERROR,
    INVALID_RECIPIENT,
    RATE_LIMITED,
    TRANSIENT,
    PERMANENT,
    backoff_seconds,
    retry_class,
)
from app.services.marketing.providers.mock import MockEmailProvider
from app.services.marketing.providers.registry import available_providers
from app.services.marketing.providers.smtp import SMTPProvider


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


def _msg(**overrides) -> OutboundMessage:
    base = dict(
        channel="EMAIL",
        recipient="to@example.com",
        sender="from@example.com",
        sender_name="Sender",
        subject="Hi",
        text="Hello",
        html=None,
    )
    base.update(overrides)
    return OutboundMessage(**base)


class TestRegistry:
    def test_mock_refused_in_production(self):
        with pytest.raises(Exception):
            get_provider("EMAIL", "mock_email", is_production=True)

    def test_mock_allowed_outside_production(self):
        provider = get_provider("EMAIL", "mock_email", is_production=False)
        assert provider.is_mock is True

    def test_real_providers_listed(self):
        ids = {p["provider_id"] for p in available_providers("EMAIL")}
        assert {"smtp", "email_api"} <= ids

    def test_unknown_provider(self):
        with pytest.raises(Exception):
            get_provider("EMAIL", "carrier_pigeon")


class TestMockProvider:
    async def test_success_records_send(self):
        mock = MockEmailProvider()
        result = await mock.send({}, {"mode": "success"}, _msg())
        assert result.success and result.provider_message_id
        assert len(mock.sent) == 1

    async def test_transient_failure_retryable(self):
        mock = MockEmailProvider()
        result = await mock.send({}, {"mode": "transient_failure"}, _msg())
        assert not result.success
        assert result.retryable and result.error_code == CONNECTION_ERROR

    async def test_permanent_failure_not_retryable(self):
        mock = MockEmailProvider()
        result = await mock.send({}, {"mode": "permanent_failure"}, _msg())
        assert result.error_code == INVALID_RECIPIENT and not result.retryable

    async def test_auth_failure(self):
        mock = MockEmailProvider()
        result = await mock.send({}, {"mode": "auth_failure"}, _msg())
        assert result.error_code == AUTHENTICATION_ERROR

    async def test_unavailable_retryable(self):
        mock = MockEmailProvider()
        result = await mock.send({}, {"mode": "unavailable"}, _msg())
        assert result.retryable

    async def test_event_normalization_and_types(self):
        mock = MockEmailProvider()
        payload = {
            "events": [
                {"id": "e1", "type": "delivered", "message_id": "m1"},
                {"id": "e2", "type": "bounce", "message_id": "m1", "hard": True},
                {"id": "e3", "type": "complaint", "message_id": "m2"},
                {"id": "e4", "type": "open", "message_id": "m1"},
                {"id": "e5", "type": "click", "message_id": "m1"},
                {"id": "e6", "type": "unknown-thing", "message_id": "m1"},
            ]
        }
        events = await mock.handle_event(payload, {}, {})
        types = [e.event_type for e in events]
        assert types == ["DELIVERED", "BOUNCED", "COMPLAINED", "OPENED", "CLICKED"]
        assert events[1].hard_bounce is True

    async def test_duplicate_webhook_events_keep_ids(self):
        mock = MockEmailProvider()
        payload = {"events": [{"id": "same-id", "type": "delivered", "message_id": "m1"}]}
        first = await mock.handle_event(payload, {}, {})
        second = await mock.handle_event(payload, {}, {})
        assert first[0].provider_event_id == second[0].provider_event_id == "same-id"


class TestErrorClassification:
    def test_transient_classes(self):
        assert retry_class(CONNECTION_ERROR) == TRANSIENT
        assert retry_class(RATE_LIMITED) == TRANSIENT

    def test_permanent_classes(self):
        assert retry_class(INVALID_RECIPIENT) == PERMANENT
        assert retry_class(AUTHENTICATION_ERROR) == PERMANENT

    def test_backoff_exponential_capped(self):
        assert backoff_seconds(1, base_seconds=10, max_seconds=100) == 10
        assert backoff_seconds(2, base_seconds=10, max_seconds=100) == 20
        assert backoff_seconds(4, base_seconds=10, max_seconds=100) == 80
        assert backoff_seconds(9, base_seconds=10, max_seconds=100) == 100  # capped


class TestSMTPValidation:
    async def test_config_requires_host_and_creds(self):
        provider = SMTPProvider()
        ok = await provider.validate_configuration(
            {"host": "smtp.example.com", "port": 587, "security": "STARTTLS"},
            {"username": "u", "password": "p"},
        )
        assert ok.ok

        missing = await provider.validate_configuration(
            {"host": "", "port": 587, "security": "STARTTLS"},
            {"username": "u", "password": "p"},
        )
        assert not missing.ok

        bad_security = await provider.validate_configuration(
            {"host": "h", "port": 587, "security": "ANCIENT"},
            {"username": "u", "password": "p"},
        )
        assert not bad_security.ok

    async def test_crlf_subject_rejected(self):
        provider = SMTPProvider()
        outcome = await provider.validate_message(_msg(subject="Hi\r\nBcc: victim@x.com"))
        assert not outcome.ok

    async def test_crlf_recipient_header_injection(self):
        provider = SMTPProvider()
        outcome = await provider.validate_sender(
            {}, {}, {"sender_email": "from@example.com\r\nBcc: x@y.com"}
        )
        assert not outcome.ok

    async def test_empty_body_rejected(self):
        provider = SMTPProvider()
        outcome = await provider.validate_message(_msg(text=None, html=None))
        assert not outcome.ok

    async def test_invalid_recipient(self):
        provider = SMTPProvider()
        assert not (await provider.validate_recipient("not-an-email")).ok
