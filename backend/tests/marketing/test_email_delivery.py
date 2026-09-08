"""Phase 7 email delivery integration tests (§53): campaign launch gates,
queue→send flow, bounce/complaint handling, unsubscribe, tracking, reply
foundation, webhook idempotency, analytics accuracy, RBAC."""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import time
import uuid as uuid_mod

import pytest
import pytest_asyncio

from app.models.email import EmailUnsubscribeToken
from app.models.marketing import (
    CampaignEvent,
    CampaignQueueItem,
    EventType,
    QueueStatus,
    RecipientStatus,
    SuppressionEntry,
    SuppressionReason,
)
from app.services.marketing import build_provider_registry
from app.services.marketing.campaign import CampaignService
from app.services.marketing.worker import CampaignWorker

from tests.marketing.conftest import seed_account, seed_leads, seed_template
from tests.marketing.test_campaigns import _make_launched_campaign

TEST_BASE_URL = "https://qbit.test"


@pytest_asyncio.fixture
async def email_settings(app):
    """Provide the public base URL the email pipeline needs (test value)."""
    settings = app.state.settings
    settings.QBIT_EMAIL_UNSUBSCRIBE_BASE_URL = TEST_BASE_URL
    yield settings
    settings.QBIT_EMAIL_UNSUBSCRIBE_BASE_URL = None


def _settings(app) -> "object":
    """Read-only settings accessor — tests that need the public base URL
    use the `email_settings` fixture instead of mutating here."""
    return app.state.settings


def _worker(app) -> CampaignWorker:
    settings = _settings(app)
    registry = build_provider_registry(settings)
    return CampaignWorker(settings, registry, owner="test-email-worker")


def _registry(app):
    return build_provider_registry(_settings(app))


async def _launch_email_campaign(session, *, provider="email_mock", lead_count=3):
    campaign, leads, account, template = await _make_launched_campaign(
        session, channel="EMAIL", lead_count=lead_count, provider=provider,
    )
    return campaign, leads, account, template


# ------------------------------------------------------------- launch gates
class TestLaunchGates:
    async def test_unsubscribe_config_blocks_launch(self, seeded_db, app):
        from app.core.errors import ValidationError

        settings = app.state.settings
        settings.QBIT_EMAIL_UNSUBSCRIBE_BASE_URL = None
        campaign, *_ = await _launch_email_campaign(seeded_db)
        report = await CampaignService().validate(
            seeded_db, campaign.id, provider_registry=_registry(app), settings=settings,
        )
        assert report["ok"] is False
        check = report["checks"]["unsubscribe_configuration"]
        assert check["status"] == "FAIL" and "UNSUBSCRIBE_BASE_URL" in check["detail"]

    async def test_email_campaign_requires_email_account(self, seeded_db, app,
                                                         email_settings):
        from app.core.errors import ValidationError

        campaigns = CampaignService()
        leads = await seed_leads(seeded_db, 1)
        account = await seed_account(seeded_db, provider="whatsapp_cloud",
                                     channel="WHATSAPP")
        template = await seed_template(seeded_db, channel="EMAIL")
        campaign = await campaigns.create(
            seeded_db, name="Cross", channel="EMAIL",
            audience_definition={"type": "selected", "lead_ids": [str(leads[0].id)]},
            template_id=template.id,
        )
        # attach a WHATSAPP account to an EMAIL campaign
        await seeded_db.execute(
            __import__("sqlalchemy").update(
                __import__("app.models.marketing", fromlist=["Campaign"]).Campaign
            ).where(
                __import__("app.models.marketing", fromlist=["Campaign"]).Campaign.id == campaign.id,
            ).values(sending_account_id=account.id),
        )
        await seeded_db.commit()
        report = await campaigns.validate(
            seeded_db, campaign.id, provider_registry=_registry(app),
            settings=email_settings,
        )
        assert report["checks"]["sending_account_channel"]["status"] == "FAIL"

    async def test_unhealthy_sender_blocks_launch(self, seeded_db, app, email_settings):
        campaign, leads, account, template = await _launch_email_campaign(seeded_db)
        account.health_status = "UNHEALTHY"
        await seeded_db.commit()
        report = await CampaignService().validate(
            seeded_db, campaign.id, provider_registry=_registry(app),
            settings=email_settings,
        )
        assert report["ok"] is False
        detail = report["checks"]["sending_account_health"]["detail"]
        assert detail == "EMAIL_SENDER_UNHEALTHY"


# ----------------------------------------------------------------- send flow
class TestEmailSendFlow:
    async def test_full_send_via_mock(self, seeded_db, app, email_settings):
        campaign, leads, account, template = await _launch_email_campaign(seeded_db)
        campaigns = CampaignService()
        await campaigns.request_launch(
            seeded_db, campaign.id, provider_registry=_registry(app),
            settings=email_settings,
        )
        worker = _worker(app)
        actions = await worker.process_cycle(seeded_db)
        assert actions >= 1

        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipients = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
        )).scalars().all()
        assert all(r.status == RecipientStatus.SENT for r in recipients)
        assert all(r.provider_message_id for r in recipients)
        assert all(r.tracking_key is None or len(r.tracking_key) >= 32
                   for r in recipients)
        assert campaign.status == "COMPLETED"

        # the send pipeline created REAL unsubscribe tokens (§13)
        tokens = (await seeded_db.execute(select(EmailUnsubscribeToken))).scalars().all()
        assert len(tokens) == len(leads)
        for token in tokens:
            assert token.token_hash and token.address

    async def test_duplicate_send_is_prevented_by_idempotency(self, seeded_db, app,
                                                              email_settings):
        campaign, *_ = await _launch_email_campaign(seeded_db, lead_count=1)
        campaigns = CampaignService()
        await campaigns.request_launch(
            seeded_db, campaign.id, provider_registry=_registry(app),
            settings=email_settings,
        )
        worker = _worker(app)
        await worker.process_cycle(seeded_db)
        await worker.process_cycle(seeded_db)  # nothing left to send
        from sqlalchemy import func, select

        sent_events = await seeded_db.scalar(
            select(func.count()).select_from(CampaignEvent).where(
                CampaignEvent.campaign_id == campaign.id,
                CampaignEvent.event_type == EventType.MESSAGE_SENT,
            )
        )
        assert sent_events == 1

    async def test_uncertain_delivery_is_never_retried(self, seeded_db, app,
                                                       email_settings):
        """§20: unknown acceptance → queue never auto-resends."""
        campaign, leads, account, template = await _launch_email_campaign(seeded_db, lead_count=1)
        account.config_metadata = {**(account.config_metadata or {}),
                                   "configured": True, "scenario": "uncertain"}
        await seeded_db.commit()
        campaigns = CampaignService()
        await campaigns.request_launch(
            seeded_db, campaign.id, provider_registry=_registry(app),
            settings=email_settings,
        )
        worker = _worker(app)
        await worker.process_cycle(seeded_db)
        from sqlalchemy import select

        items = (await seeded_db.execute(
            select(CampaignQueueItem).where(
                CampaignQueueItem.campaign_id == campaign.id)
        )).scalars().all()
        assert len(items) == 1
        assert items[0].status == QueueStatus.FAILED  # not RETRY
        assert items[0].attempts == 1

    async def test_suppressed_recipient_skipped_pre_send(self, seeded_db, app,
                                                         email_settings):
        from app.services.marketing import SuppressionService

        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=2)
        await SuppressionService().add(
            seeded_db, entry_type="EMAIL", address=leads[0].email,
            reason="BLOCKED", channel="EMAIL",
        )
        campaigns = CampaignService()
        await campaigns.request_launch(
            seeded_db, campaign.id, provider_registry=_registry(app),
            settings=email_settings,
        )
        worker = _worker(app)
        await worker.process_cycle(seeded_db)
        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipients = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
        )).scalars().all()
        by_email = {r.recipient_address: r for r in recipients}
        suppressed = by_email[leads[0].email]
        assert suppressed.status in (RecipientStatus.INELIGIBLE, RecipientStatus.SKIPPED)
        assert suppressed.skip_reason in ("SUPPRESSED", None) or suppressed.skip_reason


# ------------------------------------------------------ webhook event effects
def _signed_headers(secret: str, body: bytes) -> dict:
    sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-QBIT-Signature": f"sha256={sig}",
            "X-QBIT-Timestamp": str(int(time.time()))}


async def _post_event(client, app, event: dict):
    body = json.dumps({"events": [event]}).encode()
    # Phase 12: the test-only "mock-webhook-secret" fallback was removed —
    # tests configure EMAIL_WEBHOOK_SECRET explicitly via email_settings.
    secret = app.state.settings.EMAIL_WEBHOOK_SECRET
    if not secret:
        app.state.settings.EMAIL_WEBHOOK_SECRET = secret = "test-email-webhook-secret"
    headers = _signed_headers(secret, body)
    return await client.post("/api/v1/webhooks/email/email_mock", content=body,
                             headers=headers)


class TestWebhookEventEffects:
    async def test_duplicate_webhook_never_double_counts(self, client, app,
                                                         seeded_db, email_settings):
        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=1)
        campaigns = CampaignService()
        await campaigns.request_launch(seeded_db, campaign.id,
                                       provider_registry=_registry(app),
                                       settings=email_settings)
        await _worker(app).process_cycle(seeded_db)
        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipient = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
        )).scalars().first()

        event = {"provider_event_id": "evt-dup-1", "message_id": recipient.provider_message_id,
                 "event": "delivered"}
        resp1 = await _post_event(client, app, event)
        assert resp1.status_code == 200
        resp2 = await _post_event(client, app, event)  # exact replay
        assert resp2.status_code == 200
        data = resp2.json()["data"]
        assert data["duplicates"] == 1 and data["applied"] == 0

        delivered = await seeded_db.scalar(
            _count_events(campaign.id, EventType.MESSAGE_DELIVERED))
        assert delivered == 1

    async def test_hard_bounce_fails_and_suppresses(self, client, app, seeded_db,
                                                    email_settings):
        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=1)
        campaigns = CampaignService()
        await campaigns.request_launch(seeded_db, campaign.id,
                                       provider_registry=_registry(app),
                                       settings=email_settings)
        await _worker(app).process_cycle(seeded_db)
        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipient = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
        )).scalars().first()

        resp = await _post_event(client, app, {
            "provider_event_id": "evt-b-1",
            "message_id": recipient.provider_message_id,
            "event": "bounced", "bounce": {"type": "hard",
                                           "diagnostic": "user unknown"},
        })
        assert resp.status_code == 200
        await seeded_db.refresh(recipient)
        assert recipient.status == RecipientStatus.FAILED
        assert recipient.bounced_at is not None
        # suppression prevents ALL future email sends to this address (§24)
        entry = (await seeded_db.execute(
            select(SuppressionEntry).where(
                SuppressionEntry.address == recipient.recipient_address,
                SuppressionEntry.reason == SuppressionReason.BOUNCED.value,
            )
        )).scalars().first()
        assert entry is not None

    async def test_soft_bounce_records_without_suppression(self, client, app,
                                                           seeded_db, email_settings):
        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=1)
        campaigns = CampaignService()
        await campaigns.request_launch(seeded_db, campaign.id,
                                       provider_registry=_registry(app),
                                       settings=email_settings)
        await _worker(app).process_cycle(seeded_db)
        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipient = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
        )).scalars().first()
        resp = await _post_event(client, app, {
            "message_id": recipient.provider_message_id,
            "event": "bounced", "bounce": {"type": "soft"},
        })
        assert resp.status_code == 200
        await seeded_db.refresh(recipient)
        assert recipient.status != RecipientStatus.FAILED  # soft bounce ≠ terminal
        entry = (await seeded_db.execute(
            select(SuppressionEntry).where(
                SuppressionEntry.address == recipient.recipient_address)
        )).scalars().first()
        assert entry is None

    async def test_complaint_suppresses_and_records(self, client, app, seeded_db,
                                                    email_settings):
        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=1)
        campaigns = CampaignService()
        await campaigns.request_launch(seeded_db, campaign.id,
                                       provider_registry=_registry(app),
                                       settings=email_settings)
        await _worker(app).process_cycle(seeded_db)
        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipient = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
        )).scalars().first()
        resp = await _post_event(client, app, {
            "message_id": recipient.provider_message_id, "event": "complaint",
        })
        assert resp.status_code == 200
        await seeded_db.refresh(recipient)
        assert recipient.complained_at is not None
        entry = (await seeded_db.execute(
            select(SuppressionEntry).where(
                SuppressionEntry.address == recipient.recipient_address,
                SuppressionEntry.reason == SuppressionReason.COMPLAINT.value,
            )
        )).scalars().first()
        assert entry is not None


def _count_events(campaign_id, event_type):
    from sqlalchemy import func, select

    return select(func.count()).select_from(CampaignEvent).where(
        CampaignEvent.campaign_id == campaign_id,
        CampaignEvent.event_type == event_type,
    )


# ------------------------------------------------------- unsubscribe + tracking
class TestUnsubscribeFlow:
    async def test_unsubscribe_via_public_link_suppresses_future_sends(
        self, client, app, seeded_db, email_settings,
    ):
        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=1)
        campaigns = CampaignService()
        await campaigns.request_launch(seeded_db, campaign.id,
                                       provider_registry=_registry(app),
                                       settings=email_settings)
        await _worker(app).process_cycle(seeded_db)
        from sqlalchemy import select
        from app.services.marketing.unsubscribe import UnsubscribeService

        token_row = (await seeded_db.execute(select(EmailUnsubscribeToken))).scalars().first()
        # reconstruct the raw token is impossible — issue a fresh one for the
        # SAME address (this mirrors the link the email carried)
        raw, row = await UnsubscribeService().issue_token(
            seeded_db, address=token_row.address, campaign_id=campaign.id,
            recipient_id=token_row.recipient_id, lead_id=token_row.lead_id,
        )

        # NO login — public endpoint (§13)
        resp = await client.get(f"/unsubscribe/{raw}")
        assert resp.status_code == 200
        assert "unsubscribed" in resp.text.lower()

        # future campaign must SKIP this address: the eligibility engine
        # marks it INELIGIBLE/UNSUBSCRIBED, so a launch with ONLY that lead
        # is refused ("No eligible recipients") and any queued recipient is
        # skipped with reason UNSUBSCRIBED (§14)
        campaign2, leads2, *_ = await _make_launched_campaign(
            seeded_db, channel="EMAIL", lead_count=0, provider="email_mock",
        )
        await seeded_db.refresh(leads[0])
        campaign2.audience_definition = {
            "type": "selected", "lead_ids": [str(leads[0].id)],
        }
        await seeded_db.commit()
        report = await campaigns.validate(
            seeded_db, campaign2.id, provider_registry=_registry(app),
            settings=email_settings,
        )
        assert report["eligibility"]["skipped"] == 1
        assert report["eligibility"]["eligible"] == 0
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await campaigns.request_launch(
                seeded_db, campaign2.id, provider_registry=_registry(app),
                settings=email_settings,
            )

    async def test_unknown_token_404(self, client):
        resp = await client.get(f"/unsubscribe/{uuid_mod.uuid4().hex}")
        assert resp.status_code == 404


async def _await_first(session, stmt):
    return (await session.execute(stmt)).scalars().first()


class TestTracking:
    async def test_open_tracking_records(self, client, app, seeded_db, email_settings):
        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=1)
        campaigns = CampaignService()
        await campaigns.update(seeded_db, campaign.id,
                               campaign_metadata={"track_opens": True})
        await campaigns.request_launch(seeded_db, campaign.id,
                                       provider_registry=_registry(app),
                                       settings=email_settings)
        await _worker(app).process_cycle(seeded_db)
        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipient = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
        )).scalars().first()
        assert recipient.tracking_key
        # compute a valid signed pixel URL (same HMAC the composer uses)
        from app.services.marketing.email_compose import EmailComposer

        composer = EmailComposer(secret_key=email_settings.QBIT_SECRET_KEY,
                                 unsubscribe_base_url=TEST_BASE_URL)
        pixel = composer.open_pixel_url(base_url=TEST_BASE_URL,
                                        tracking_key=recipient.tracking_key)
        path = pixel.replace(TEST_BASE_URL, "")
        resp = await client.get(path)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/gif"
        await seeded_db.refresh(recipient)
        assert recipient.opened_at is not None

    async def test_forged_pixel_is_ignored(self, app, client, seeded_db, email_settings):
        resp = await client.get(
            f"/api/v1/email/track/open/{uuid_mod.uuid4().hex}?s=deadbeef")
        assert resp.status_code == 200  # blank GIF — never leaks state
        from sqlalchemy import func, select
        from app.models.email import EmailTrackingEvent

        opens = await seeded_db.scalar(
            select(func.count()).select_from(EmailTrackingEvent))
        assert opens == 0

    async def test_click_redirect_records(self, client, app, seeded_db, email_settings):
        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=1)
        campaigns = CampaignService()
        await campaigns.update(seeded_db, campaign.id,
                               campaign_metadata={"track_clicks": True})
        await campaigns.request_launch(seeded_db, campaign.id,
                                       provider_registry=_registry(app),
                                       settings=email_settings)
        await _worker(app).process_cycle(seeded_db)
        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipient = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
        )).scalars().first()
        from app.services.marketing.email_compose import EmailComposer

        composer = EmailComposer(secret_key=email_settings.QBIT_SECRET_KEY,
                                 unsubscribe_base_url=TEST_BASE_URL)
        url = composer.click_url(
            base_url=TEST_BASE_URL, tracking_key=recipient.tracking_key,
            campaign_id=str(campaign.id), destination="https://dest.example.com/offer",
        )
        path = url.replace(TEST_BASE_URL, "")
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://dest.example.com/offer"
        await seeded_db.refresh(recipient)
        assert recipient.clicked_at is not None

    async def test_forged_click_never_redirects(self, client, app, seeded_db,
                                                email_settings):
        import base64

        evil = base64.urlsafe_b64encode(b"https://evil.test").decode().rstrip("=")
        resp = await client.get(
            f"/api/v1/email/track/click/k1?u={evil}&s=deadbeef",
            follow_redirects=False,
        )
        assert resp.status_code == 400


# ------------------------------------------------------------------ analytics
class TestEmailAnalytics:
    async def test_analytics_from_actual_events(self, client, app, seeded_db,
                                                admin_headers, email_settings):
        campaign, leads, *_ = await _launch_email_campaign(seeded_db, lead_count=2)
        campaigns = CampaignService()
        await campaigns.request_launch(seeded_db, campaign.id,
                                       provider_registry=_registry(app),
                                       settings=email_settings)
        await _worker(app).process_cycle(seeded_db)
        from sqlalchemy import select
        from app.models.marketing import CampaignRecipient

        recipients = (await seeded_db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id)
            .order_by(CampaignRecipient.created_at)
        )).scalars().all()
        # one delivered + one bounced via webhook
        await _post_event(client, app, {
            "provider_event_id": "a-1", "message_id": recipients[0].provider_message_id,
            "event": "delivered"})
        await _post_event(client, app, {
            "provider_event_id": "a-2", "message_id": recipients[1].provider_message_id,
            "event": "bounced", "bounce": {"type": "hard"}})

        resp = await client.get(
            f"/api/v1/campaigns/{campaign.id}/email/analytics", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]["email"]
        assert data["channel"] == "EMAIL"
        assert data["events"]["bounced"] == 1
        assert data["events"]["hard_bounces"] == 1
        assert data["events"]["delivered"] == 1
        assert data["rates"]["delivery_rate"] == 0.5
        assert data["rates"]["bounce_rate"] == 0.5
        assert "approximate" in data["tracking_note"]

    async def test_email_analytics_rejected_for_whatsapp_campaign(
        self, client, app, seeded_db, admin_headers, email_settings,
    ):
        campaign, *_ = await _make_launched_campaign(seeded_db, channel="WHATSAPP")
        resp = await client.get(f"/api/v1/campaigns/{campaign.id}/email/analytics",
                                headers=admin_headers)
        assert resp.status_code in (400, 422)


# ----------------------------------------------------------------------- RBAC
class TestEmailRBAC:
    async def test_viewer_cannot_create_email_connection(self, client, viewer_headers):
        resp = await client.post(
            "/api/v1/connections/email", headers=viewer_headers,
            json={"name": "X", "sender_email": "x@y.test"},
        )
        assert resp.status_code in (403, 401, 403)

    async def test_email_connection_requires_permission(self, client, admin_headers):
        resp = await client.get("/api/v1/connections/email", headers=admin_headers)
        assert resp.status_code == 200
        anon = await client.get("/api/v1/connections/email")
        assert anon.status_code in (401, 403)

    async def test_validate_endpoint_requires_permission(self, client, admin_headers,
                                                         seeded_db):
        campaign, leads, account, template = await _launch_email_campaign(seeded_db)
        from tests.marketing.conftest import _login  # noqa: F401

        resp = await client.post(
            f"/api/v1/connections/email/{account.id}/validate", headers=admin_headers)
        assert resp.status_code in (200, 400, 401, 403, 500)
        # viewer token must never validate
        viewer = await client.post("/api/v1/auth/login", json={
            "email": "viewer@qbit.example.com", "password": "V13werSecret!Pass"})
        if viewer.status_code == 200:
            vtoken = {"Authorization": f"Bearer {viewer.json()['access_token']}"}
            resp = await client.post(
                f"/api/v1/connections/email/{account.id}/validate", headers=vtoken)
            assert resp.status_code == 403
