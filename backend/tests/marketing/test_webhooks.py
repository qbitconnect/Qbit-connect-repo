"""Webhook security + idempotency tests (Phase 7 §24–§28)."""

import json
import time

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.models.marketing import CampaignRecipient, ProviderEvent, Suppression
from app.services.marketing.webhooks import (
    MarketingWebhookService,
    verify_generic_signature,
    verify_timestamp,
    WebhookVerificationError,
)
from tests.marketing.helpers import (
    make_campaign,
    make_mock_account,
    make_recipient,
    make_template,
    webhook_signature,
)

SECRET = "webhook-shared-secret"


@pytest_asyncio.fixture
async def session(app):
    db = app.state.db
    async with db.session() as s:
        yield s


@pytest_asyncio.fixture
async def sent_recipient(session):
    account = await make_mock_account(session)
    template = await make_template(session)
    campaign = await make_campaign(session, account=account, template=template)
    recipient = await make_recipient(session, campaign=campaign, address="hook@example.com")
    recipient.status = "SENT"
    recipient.provider_message_id = "mock-abc-123"
    await session.commit()  # release the write txn so API requests can write
    return campaign, recipient


def _event_body(event_id="evt-1", etype="delivered", message_id="mock-abc-123", **extra):
    return json.dumps(
        {"events": [{"id": event_id, "type": etype, "message_id": message_id, **extra}]}
    ).encode()


def _headers(body: bytes, *, ts: float | None = None, secret: str = SECRET, app) -> dict:
    return {
        "X-QBIT-Signature": webhook_signature(body, secret),
        "X-QBIT-Timestamp": str(int(ts if ts is not None else time.time())),
        "content-type": "application/json",
    }


@pytest.fixture
def settings(app, monkeypatch):
    monkeypatch.setattr(app.state.settings, "QBIT_MARKETING_WEBHOOK_SECRET", SECRET)
    return app.state.settings


class TestSignatureVerification:
    def test_valid_signature_passes(self, app):
        body = b'{"events": []}'
        verify_generic_signature(
            raw_body=body,
            signature=webhook_signature(body, SECRET),
            secret=SECRET,
        )

    def test_forged_signature_rejected(self, app):
        body = b'{"events": []}'
        with pytest.raises(WebhookVerificationError):
            verify_generic_signature(raw_body=body, signature="sha256=deadbeef", secret=SECRET)

    def test_missing_signature_rejected(self, app):
        with pytest.raises(WebhookVerificationError):
            verify_generic_signature(raw_body=b"{}", signature=None, secret=SECRET)

    def test_stale_timestamp_rejected(self, app):
        with pytest.raises(WebhookVerificationError):
            verify_timestamp(timestamp=str(int(time.time()) - 10_000), tolerance_seconds=300)

    def test_fresh_timestamp_passes(self, app):
        verify_timestamp(timestamp=str(int(time.time())), tolerance_seconds=300)

    async def test_api_rejects_forged_webhook(self, client, settings, sent_recipient):
        body = _event_body()
        resp = await client.post(
            "/api/v1/webhooks/email/mock_email",
            content=body,
            headers={"X-QBIT-Signature": "sha256=forged", "X-QBIT-Timestamp": str(int(time.time()))},
        )
        assert resp.status_code == 401

    async def test_api_rejects_replayed_timestamp(self, client, settings, sent_recipient):
        body = _event_body()
        old_ts = time.time() - 10_000
        resp = await client.post(
            "/api/v1/webhooks/email/mock_email",
            content=body,
            headers=_headers(body, ts=old_ts, app=None),
        )
        assert resp.status_code == 401


class TestEventApplication:
    async def test_delivered_event_applied(self, client, settings, session, sent_recipient):
        campaign, recipient = sent_recipient
        body = _event_body()
        resp = await client.post(
            "/api/v1/webhooks/email/mock_email", content=body, headers=_headers(body, app=None)
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["applied"] == 1

        await session.refresh(recipient)
        assert recipient.status == "DELIVERED"

    async def test_duplicate_webhook_never_applies_twice(self, client, settings, session, sent_recipient):
        campaign, recipient = sent_recipient
        body = _event_body()
        for _ in range(2):
            resp = await client.post(
                "/api/v1/webhooks/email/mock_email", content=body, headers=_headers(body, app=None)
            )
            assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["duplicates"] == 1
        assert data["applied"] == 0

        rows = (await session.scalars(select(ProviderEvent))).all()
        assert len(rows) == 1  # single provider_event_id row

    async def test_hard_bounce_suppresses_future_sends(self, client, settings, session, sent_recipient):
        campaign, recipient = sent_recipient
        body = _event_body(etype="bounce", hard=True, reason="user unknown")
        resp = await client.post(
            "/api/v1/webhooks/email/mock_email", content=body, headers=_headers(body, app=None)
        )
        assert resp.status_code == 200
        await session.refresh(recipient)
        assert recipient.status == "BOUNCED"

        suppression = (
            await session.scalars(
                select(Suppression).where(
                    Suppression.address_norm == "hook@example.com",
                    Suppression.reason == "HARD_BOUNCE",
                )
            )
        ).first()
        assert suppression is not None

    async def test_complaint_suppresses(self, client, settings, session, sent_recipient):
        campaign, recipient = sent_recipient
        body = _event_body(etype="complaint")
        resp = await client.post(
            "/api/v1/webhooks/email/mock_email", content=body, headers=_headers(body, app=None)
        )
        assert resp.status_code == 200
        await session.refresh(recipient)
        assert recipient.status == "COMPLAINED"
        suppression = (
            await session.scalars(
                select(Suppression).where(
                    Suppression.address_norm == "hook@example.com",
                    Suppression.reason == "COMPLAINT",
                )
            )
        ).first()
        assert suppression is not None

    async def test_soft_bounce_no_suppression(self, client, settings, session, sent_recipient):
        campaign, recipient = sent_recipient
        body = _event_body(etype="bounce", hard=False)
        await client.post(
            "/api/v1/webhooks/email/mock_email", content=body, headers=_headers(body, app=None)
        )
        suppression = (
            await session.scalars(
                select(Suppression).where(Suppression.address_norm == "hook@example.com")
            )
        ).first()
        assert suppression is None

    async def test_unknown_message_id_recorded_not_applied(self, client, settings, session):
        body = _event_body(message_id="never-sent-id")
        resp = await client.post(
            "/api/v1/webhooks/email/mock_email", content=body, headers=_headers(body, app=None)
        )
        data = resp.json()["data"]
        assert data["applied"] == 0 and data["skipped"] == 1

    async def test_failed_event(self, client, settings, session, sent_recipient):
        campaign, recipient = sent_recipient
        body = _event_body(etype="fail", reason="policy")
        resp = await client.post(
            "/api/v1/webhooks/email/mock_email", content=body, headers=_headers(body, app=None)
        )
        data = resp.json()["data"]
        assert data["applied"] == 1
        await session.refresh(recipient)
        assert recipient.status == "FAILED"

    async def test_out_of_order_event_ignored(self, session):
        """READ arriving after DELIVERED is fine; DELIVERED after READ must not
        move the state machine backwards (spec §events)."""
        service = MarketingWebhookService(timestamp_tolerance=300)
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template)
        recipient = await make_recipient(session, campaign=campaign, address="x@example.com")
        recipient.status = "SENT"
        recipient.provider_message_id = "m-1"
        await session.flush()

        from app.services.marketing.providers.base import NormalizedEvent

        # delivered first
        applied = await service._apply(
            session, channel="EMAIL", provider="mock_email",
            events=[NormalizedEvent("e1", "DELIVERED", "m-1", None)],
        )
        assert applied["applied"] == 1
        # duplicate delivered — forward-only transition blocks it
        applied2 = await service._apply(
            session, channel="EMAIL", provider="mock_email",
            events=[NormalizedEvent("e2", "DELIVERED", "m-1", None)],
        )
        assert applied2["applied"] == 0
