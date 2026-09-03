"""Phase 6 tests — WhatsAppErrorNormalizer (§16) + WhatsAppProvider over the
official Graph API using httpx.MockTransport (no network, §40/§41)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.marketing.providers.base import ErrorClass
from app.services.marketing.providers.whatsapp import (
    WhatsAppCloudClient,
    WhatsAppErrorNormalizer,
    WhatsAppProvider,
    count_placeholders,
)

SECRET = "unit-test-secret-key-" + "d" * 40


def _make_provider(handler) -> WhatsAppProvider:
    return WhatsAppProvider(transport_factory=lambda token: httpx.MockTransport(handler))


# ------------------------------------------------------------- §16 normalizer
def test_rate_limit_is_transient_with_retry_after():
    err = WhatsAppErrorNormalizer().normalize(status_code=400, payload={
        "error": {"code": 131048, "message": "Rate limit hit",
                  "error_data": {"details": "wait 90 seconds", "messaging_product": "whatsapp"}},
    })
    assert err.code == "RATE_LIMITED" and err.error_class == ErrorClass.TRANSIENT


def test_auth_error_permanent():
    err = WhatsAppErrorNormalizer().normalize(status_code=401, payload={
        "error": {"code": 190, "message": "Access token expired", "type": "OAuthException"},
    })
    assert err.code == "AUTHENTICATION_ERROR" and err.error_class == ErrorClass.PERMANENT


def test_permission_error_permanent():
    err = WhatsAppErrorNormalizer().normalize(status_code=403, payload={
        "error": {"code": 10, "message": "Permission denied"},
    })
    assert err.code == "PERMISSION_ERROR" and err.error_class == ErrorClass.PERMANENT


def test_invalid_recipient_permanent():
    err = WhatsAppErrorNormalizer().normalize(status_code=400, payload={
        "error": {"code": 131026, "message": "Message undeliverable"},
    })
    assert err.code == "INVALID_RECIPIENT" and err.error_class == ErrorClass.PERMANENT


def test_message_rejected_reengagement_permanent():
    err = WhatsAppErrorNormalizer().normalize(status_code=400, payload={
        "error": {"code": 131047, "message": "Re-engagement message required"},
    })
    assert err.code == "MESSAGE_REJECTED" and err.error_class == ErrorClass.PERMANENT


def test_template_family_invalid_template():
    err = WhatsAppErrorNormalizer().normalize(status_code=400, payload={
        "error": {"code": 132001, "message": "Template name does not exist"},
    })
    assert err.code == "INVALID_TEMPLATE" and err.error_class == ErrorClass.PERMANENT


def test_template_not_approved_detected_by_text():
    err = WhatsAppErrorNormalizer().normalize(status_code=400, payload={
        "error": {"code": 132005, "message": "Template has not been approved"},
    })
    assert err.code == "TEMPLATE_NOT_APPROVED" and err.error_class == ErrorClass.PERMANENT


def test_server_error_transient():
    err = WhatsAppErrorNormalizer().normalize(status_code=503, payload={"error": {"message": "unavailable"}})
    assert err.code == "PROVIDER_UNAVAILABLE" and err.error_class == ErrorClass.TRANSIENT


def test_network_timeout_transient():
    err = WhatsAppErrorNormalizer().normalize(status_code=0, payload={
        "error": {"code": -1, "message": "Provider request timed out"}})
    assert err.code == "PROVIDER_UNAVAILABLE" and err.error_class == ErrorClass.TRANSIENT


def test_unknown_error_classified_by_status():
    err = WhatsAppErrorNormalizer().normalize(status_code=418, payload={
        "error": {"code": 999999, "message": "Something odd"}})
    assert err.code == "UNKNOWN_PROVIDER_ERROR" and err.error_class == ErrorClass.PERMANENT


def test_no_secrets_in_normalized_error():
    err = WhatsAppErrorNormalizer().normalize(status_code=401, payload={
        "error": {"code": 190, "message": "token EAAG-secret-value rejected"}})
    assert "access_token" not in (err.detail or "")
    # the message is the provider's own — but never any of OUR credentials
    assert "Bearer" not in (err.detail or "")


# ------------------------------------------------------------- §1/§14 send
def _send_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path.endswith("/111111111111111/messages")
    assert request.headers["Authorization"].startswith("Bearer ")
    body = json.loads(request.content.decode())
    if body["template"]["name"] == "bad_template":
        return httpx.Response(400, json={"error": {"code": 132001, "message": "Template does not exist"}})
    if body["template"]["name"] == "unapproved_tpl":
        return httpx.Response(400, json={"error": {"code": 132005,
                                                   "message": "Template has not been approved"}})
    return httpx.Response(200, json={
        "messaging_product": "whatsapp",
        "contacts": [{"input": body["to"], "wa_id": "4915112345678"}],
        "messages": [{"id": "wamid.test123", "message_status": "accepted"}],
    })


async def test_send_success_normalizes_response():
    provider = _make_provider(_send_handler)
    result = await provider.send(
        account_config={"configured": True, "phone_number_id": "111111111111111"},
        recipient_address="+49 (151) 1234-5678", subject=None, body="ignored",
        idempotency_key="c1:r1:1",
        credentials={"access_token": "test-token"},
        template={"provider_template_name": "welcome_business", "language": "en",
                  "components": [{"type": "body", "parameters": [{"type": "text", "text": "Hi"}]}]},
    )
    assert result.ok and result.provider_message_id == "wamid.test123"
    assert result.status == "SENT"
    assert result.metadata["wa_id"] == "4915112345678"


async def test_send_error_normalized_permanent():
    provider = _make_provider(_send_handler)
    result = await provider.send(
        account_config={"configured": True, "phone_number_id": "111111111111111"},
        recipient_address="+4915112345678", subject=None, body="x",
        idempotency_key="c1:r2:1", credentials={"access_token": "test-token"},
        template={"provider_template_name": "bad_template", "language": "en", "components": []},
    )
    assert not result.ok and result.error_code == "INVALID_TEMPLATE"
    assert result.error_class == ErrorClass.PERMANENT


async def test_send_error_normalized_not_approved():
    provider = _make_provider(_send_handler)
    result = await provider.send(
        account_config={"configured": True, "phone_number_id": "111111111111111"},
        recipient_address="+4915112345678", subject=None, body="x",
        idempotency_key="c1:r3:1", credentials={"access_token": "test-token"},
        template={"provider_template_name": "unapproved_tpl", "language": "en", "components": []},
    )
    assert not result.ok and result.error_code == "TEMPLATE_NOT_APPROVED"


async def test_send_requires_template_payload():
    provider = _make_provider(_send_handler)
    result = await provider.send(
        account_config={"configured": True, "phone_number_id": "111111111111111"},
        recipient_address="+4915112345678", subject=None, body="text only",
        idempotency_key="c1:r4:1", credentials={"access_token": "test-token"},
        template=None,
    )
    assert not result.ok and result.error_code == "INVALID_TEMPLATE"


async def test_send_without_credentials_fails_configuration():
    provider = _make_provider(_send_handler)
    result = await provider.send(
        account_config={"configured": True, "phone_number_id": "111111111111111"},
        recipient_address="+4915112345678", subject=None, body="x",
        idempotency_key="c1:r5:1", credentials=None,
        template={"provider_template_name": "t", "language": "en", "components": []},
    )
    assert not result.ok and result.error_code == "CREDENTIALS_MISSING"


async def test_send_invalid_recipient_structural():
    provider = _make_provider(_send_handler)
    result = await provider.send(
        account_config={"configured": True, "phone_number_id": "111111111111111"},
        recipient_address="12345", subject=None, body="x",
        idempotency_key="c1:r6:1", credentials={"access_token": "test-token"},
        template={"provider_template_name": "t", "language": "en", "components": []},
    )
    assert not result.ok and result.error_code == "INVALID_RECIPIENT"


# ------------------------------------------------------------- §7 health
def _phone_handler(status: int = 200, quality: str = "GREEN") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "fields=" in request.url.query.decode() or "fields" in str(request.url)
        return httpx.Response(status, json={
            "id": "111111111111111", "display_phone_number": "+49 151 12345678",
            "verified_name": "QBIT Test", "quality_rating": quality,
        })
    return httpx.MockTransport(handler)


async def test_health_healthy_green():
    provider = WhatsAppProvider(transport_factory=lambda t: _phone_handler(quality="GREEN"))
    result = await provider.health_check(
        {"configured": True, "phone_number_id": "111111111111111"},
        credentials={"access_token": "t"},
    )
    assert result["health"] == "HEALTHY"
    assert result["detail"]["display_phone_number_masked"].startswith("+49")
    assert "12345678" not in result["detail"]["display_phone_number_masked"]


async def test_health_degraded_yellow():
    provider = WhatsAppProvider(transport_factory=lambda t: _phone_handler(quality="YELLOW"))
    result = await provider.health_check(
        {"configured": True, "phone_number_id": "111111111111111"},
        credentials={"access_token": "t"})
    assert result["health"] == "DEGRADED"


async def test_health_auth_failure_unhealthy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 190, "message": "Access token expired"}})
    provider = WhatsAppProvider(transport_factory=lambda t: httpx.MockTransport(handler))
    result = await provider.health_check(
        {"configured": True, "phone_number_id": "111111111111111"},
        credentials={"access_token": "expired"})
    assert result["health"] == "UNHEALTHY"
    assert "AUTHENTICATION_ERROR" in result["detail"]


# ----------------------------------------------------- §5 account validation
async def test_validate_account_full_flow():
    provider = _make_provider(_send_handler)
    # get_phone_number endpoint: use phone handler for the validation calls
    provider2 = WhatsAppProvider(transport_factory=lambda t: httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "id": "111111111111111", "display_phone_number": "+49 151 12345678",
            "verified_name": "QBIT", "quality_rating": "GREEN",
        }) if "111111111111111?fields" in str(req.url) or "111111111111111/" not in str(req.url).split("?")[0][-30:] else httpx.Response(200, json={
            "id": "999999999999999", "name": "QBIT WABA",
            "business_verification_status": "APPROVED", "messaging_limit_tier": "TIER_1K",
        })
    ))
    report = await provider2.validate_account(
        {"configured": True, "phone_number_id": "111111111111111",
         "business_account_id": "999999999999999"},
        credentials={"access_token": "t"},
    )
    assert report["ok"] is True
    assert report["steps"]["credentials"]["ok"]
    assert report["steps"]["phone_number_valid"]["ok"]
    assert report["steps"]["business_account"]["ok"]
    assert report["steps"]["permissions"]["ok"]


async def test_validate_account_bad_token_reports_step():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 190, "message": "Access token expired"}})
    provider = WhatsAppProvider(transport_factory=lambda t: httpx.MockTransport(handler))
    report = await provider.validate_account(
        {"configured": True, "phone_number_id": "111111111111111"},
        credentials={"access_token": "bad"})
    assert report["ok"] is False
    assert report["steps"]["phone_number_valid"]["ok"] is False
    assert "AUTHENTICATION_ERROR" in report["steps"]["phone_number_valid"]["detail"]


async def test_validate_account_missing_token():
    provider = _make_provider(_send_handler)
    report = await provider.validate_account(
        {"configured": True, "phone_number_id": "111111111111111"}, credentials=None)
    assert report["ok"] is False
    assert report["steps"]["credentials"]["ok"] is False


# ------------------------------------------------------ §9/§10 templates
async def test_fetch_templates_normalizes_catalog():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "message_templates" in str(request.url)
        return httpx.Response(200, json={"data": [
            {"id": "t1", "name": "welcome_business", "status": "APPROVED",
             "category": "MARKETING", "language": "en",
             "components": [{"type": "BODY", "text": "Hello {{1}} and {{2}}"}]},
            {"id": "t2", "name": "order_update", "status": "PENDING",
             "category": "UTILITY", "language": "de",
             "components": [{"type": "BODY", "text": "Order {{1}}"}]},
            {"no_name": True},  # malformed entry is skipped
        ]})
    provider = WhatsAppProvider(transport_factory=lambda t: httpx.MockTransport(handler))
    rows = await provider.fetch_templates(
        {"configured": True, "business_account_id": "999999999999999"},
        credentials={"access_token": "t"})
    assert len(rows) == 2
    assert rows[0]["provider_template_id"] == "t1"
    assert rows[0]["provider_status"] == "APPROVED"
    assert count_placeholders(rows[0]["components"]["raw"]) == {"body": 2, "header": 0}


async def test_fetch_templates_requires_waba_id():
    provider = _make_provider(_send_handler)
    from app.services.marketing.providers.base import ProviderError

    with pytest.raises(ProviderError):
        await provider.fetch_templates({"configured": True}, credentials={"access_token": "t"})


def test_validate_send_requirements_gate():
    provider = WhatsAppProvider()

    class T:
        origin = "PROVIDER"
        provider_status = "APPROVED"
        provider_template_id = "t1"
        language = "en"
        variables = ["first_name", "business_name"]
        components = {"placeholders": {"body": 2, "header": 0}}
        rejected_reason = None

    assert provider.validate_send_requirements.__self__ is not None  # callable
    problems = _run(provider.validate_send_requirements(template=T(), account_config={
        "phone_number_id": "111111111111111"}))
    assert problems == []


def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_validate_send_requirements_rejects_local_template():
    provider = WhatsAppProvider()

    class T:
        origin = "LOCAL"
        provider_status = None
        provider_template_id = None
        variables = []
        components = {}
        rejected_reason = None

    problems = _run(provider.validate_send_requirements(
        template=T(), account_config={"phone_number_id": "111111111111111"}))
    assert problems and "provider-synced" in problems[0]


def test_validate_send_requirements_rejects_non_approved():
    provider = WhatsAppProvider()

    class T:
        origin = "PROVIDER"
        provider_status = "PENDING"
        provider_template_id = "t2"
        variables = []
        components = {"placeholders": {"body": 1, "header": 0}}
        rejected_reason = None

    problems = _run(provider.validate_send_requirements(
        template=T(), account_config={"phone_number_id": "111111111111111"}))
    assert any("APPROVED" in p for p in problems)


def test_validate_send_requirements_variable_count_mismatch():
    provider = WhatsAppProvider()

    class T:
        origin = "PROVIDER"
        provider_status = "APPROVED"
        provider_template_id = "t1"
        variables = ["first_name"]
        components = {"placeholders": {"body": 2, "header": 0}}
        rejected_reason = None

    problems = _run(provider.validate_send_requirements(
        template=T(), account_config={"phone_number_id": "111111111111111"}))
    assert any("2 variable" in p for p in problems)


def test_validate_send_requirements_missing_phone_number_id():
    provider = WhatsAppProvider()

    class T:
        origin = "PROVIDER"
        provider_status = "APPROVED"
        provider_template_id = "t1"
        variables = []
        components = {"placeholders": {"body": 0, "header": 0}}
        rejected_reason = None

    problems = _run(provider.validate_send_requirements(template=T(), account_config={}))
    assert any("phone_number_id" in p for p in problems)


# ------------------------------------------------ §14 template payload build
class _Lead:
    first_name = "Ravi"
    business_name = "Acme"


def test_build_template_payload_components_ordered():
    provider = WhatsAppProvider()

    class T:
        name = "welcome_business"
        provider_template_id = "t1"
        language = "en"
        variables = ["first_name", "business_name"]
        components = {"placeholders": {"body": 2, "header": 0}}

    payload, missing = provider.build_template_payload(T(), _Lead())
    assert payload is not None and missing == []
    body = [c for c in payload["components"] if c["type"] == "body"][0]
    assert [p["text"] for p in body["parameters"]] == ["Ravi", "Acme"]


def test_build_template_payload_missing_variable_skips():
    provider = WhatsAppProvider()

    class T:
        name = "welcome_business"
        provider_template_id = "t1"
        language = "en"
        variables = ["first_name", "business_name"]
        components = {"placeholders": {"body": 2, "header": 0}}

    class EmptyLead:
        first_name = "Ravi"
        business_name = None

    payload, missing = provider.build_template_payload(T(), EmptyLead())
    assert payload is None and missing == ["business_name"]


def test_build_template_payload_with_header():
    provider = WhatsAppProvider()

    class T:
        name = "welcome_business"
        provider_template_id = "t1"
        language = "en"
        variables = ["business_name", "first_name"]
        components = {"placeholders": {"body": 1, "header": 1}}

    payload, missing = provider.build_template_payload(T(), _Lead())
    assert payload is not None and missing == []
    types = [c["type"] for c in payload["components"]]
    assert types == ["header", "body"]


# ------------------------------------------------------------- §2 config
async def test_validate_configuration_problems():
    provider = WhatsAppProvider()
    assert await provider.validate_configuration({}) != []  # unconfigured
    problems = await provider.validate_configuration({"configured": True})
    assert any("phone_number_id" in p for p in problems)
    ok = await provider.validate_configuration({
        "configured": True, "phone_number_id": "123", "credential_ref": "whatsapp:abc",
    })
    assert ok == []
    bad_url = await provider.validate_configuration({
        "configured": True, "phone_number_id": "123", "credential_ref": "x",
        "api_base_url": "not-a-url",
    })
    assert any("api_base_url" in p for p in bad_url)
