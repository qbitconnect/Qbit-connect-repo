"""Campaign API + pipeline integration tests (Phase 7 §15, §17, §20, §21, §41)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import ValidationError
from app.models.marketing import CampaignRecipient, RecipientStatus
from app.services.marketing.campaigns import CampaignService
from app.services.marketing.delivery import EmailDeliveryService
from app.services.marketing.queue import InProcessMarketingQueue
from tests.marketing.helpers import (
    make_campaign,
    make_consent,
    make_lead,
    make_mock_account,
    make_recipient,
    make_suppression,
    make_template,
)


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


class TestEligibilityChain:
    async def test_missing_email_skipped(self, session, app):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={"require_opt_in": False, "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
        )
        lead = await make_lead(session, email=None)
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        # leads without addresses are not snapshotted at all
        assert campaign.audience_total == 0

    async def test_invalid_email_skipped_with_reason(self, session, app):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={"require_opt_in": False, "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
        )
        await make_lead(session, email="broken@@example")
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        result = await service.run_eligibility(session, campaign)
        assert result["skipped"] == 1
        row = (await session.scalars(select(CampaignRecipient))).first()
        assert row.status == "SKIPPED" and row.reason == "INVALID_EMAIL"

    async def test_unsubscribed_skipped(self, session, app):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={"require_opt_in": False, "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
        )
        await make_lead(session, email="opted-out@example.com")
        await make_suppression(session, email="opted-out@example.com", reason="UNSUBSCRIBED")
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        result = await service.run_eligibility(session, campaign)
        assert result["eligible"] == 0
        row = (await session.scalars(select(CampaignRecipient))).first()
        assert row.reason == "UNSUBSCRIBED"

    async def test_suppressed_skipped(self, session, app):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={"require_opt_in": False, "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
        )
        await make_lead(session, email="suppressed@example.com")
        await make_suppression(session, email="suppressed@example.com", reason="HARD_BOUNCE")
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        await service.run_eligibility(session, campaign)
        row = (await session.scalars(select(CampaignRecipient))).first()
        assert row.reason == "SUPPRESSED"

    async def test_no_opt_in_without_consent(self, session, app):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={"require_opt_in": True, "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
        )
        await make_lead(session, email="no-consent@example.com")
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        await service.run_eligibility(session, campaign)
        row = (await session.scalars(select(CampaignRecipient))).first()
        assert row.reason == "NO_OPT_IN"  # scraped ≠ consent (spec §15)

    async def test_opted_in_eligible(self, session, app):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={"require_opt_in": True, "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
        )
        lead = await make_lead(session, email="opted-in@example.com")
        await make_consent(session, email="opted-in@example.com", lead_id=lead.id)
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        result = await service.run_eligibility(session, campaign)
        assert result["eligible"] == 1


class TestLaunchPipeline:
    async def _full_campaign(self, session, *, email: str = "send@example.com", require_opt_in=False):
        account = await make_mock_account(session)
        account.status = "ACTIVE"       # direct-service path: mark validated
        account.health_status = "HEALTHY"
        template = await make_template(session)
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={
                "require_opt_in": require_opt_in,
                "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]},
            },
        )
        lead = await make_lead(session, email=email)
        return campaign, lead, account, template

    async def test_launch_queues_and_sends(self, session, app):
        campaign, lead, account, template = await self._full_campaign(session)
        queue = InProcessMarketingQueue()
        service = CampaignService()
        result = await service.launch(session, campaign, queue)
        assert result["queued"]["queued"] == 1
        assert campaign.status == "QUEUED"

        recipient_id = (await session.scalars(select(CampaignRecipient.id))).first()
        delivery = EmailDeliveryService(
            Settings(QBIT_ENV="test", QBIT_SECRET_KEY="test-secret-key-" + "a" * 48, _env_file=None)
        )
        status = await delivery.process(session, recipient_id, queue=queue)
        assert status == "SENT"
        row = await session.get(CampaignRecipient, recipient_id)
        assert row.status == "SENT"
        assert row.provider_message_id

    async def test_idempotency_prevents_duplicate_rows(self, session, app):
        campaign, lead, account, template = await self._full_campaign(session)
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        await service.snapshot_audience(session, campaign)  # second pass is a no-op
        total = len((await session.scalars(select(CampaignRecipient))).all())
        assert total == 1

    async def test_launch_refused_without_account(self, session, app):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template)
        campaign.sending_account_id = None
        service = CampaignService()
        with pytest.raises(ValidationError):
            await service.launch(session, campaign, InProcessMarketingQueue())

    async def test_launch_refused_unhealthy_account(self, session, app):
        account = await make_mock_account(session)
        account.health_status = "UNHEALTHY"
        template = await make_template(session)
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={"require_opt_in": False, "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
        )
        service = CampaignService()
        with pytest.raises(ValidationError) as exc:
            await service.launch(session, campaign, InProcessMarketingQueue())
        codes = {i["code"] for i in exc.value.details["issues"]}
        assert "SENDING_ACCOUNT_UNHEALTHY" in codes

    async def test_cancel_skips_remaining(self, session, app):
        campaign, lead, account, template = await self._full_campaign(session)
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        await service.run_eligibility(session, campaign)
        cancelled = await service.cancel(session, campaign)
        assert cancelled == 1
        assert campaign.status == "CANCELLED"

    async def test_requeue_failed_bumps_version(self, session, app):
        campaign, lead, account, template = await self._full_campaign(session)
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        await service.run_eligibility(session, campaign)
        recipient = (await session.scalars(select(CampaignRecipient))).first()
        recipient.status = RecipientStatus.FAILED
        await session.flush()
        result = await service.requeue_failed(session, campaign)
        assert result["created"] == 1
        assert result["message_version"] == 2


class TestCampaignAPI:
    async def test_full_api_launch_flow(self, client, admin_headers, session):
        # seed: account + template + lead
        account = await make_mock_account(session)
        template = await make_template(session)
        lead = await make_lead(session, email="api-flow@example.com")
        await session.commit()

        # 1. validate the sending account first (spec §7: validate → ACTIVE)
        resp = await client.post(
            f"/api/v1/connections/email/{account.id}/validate", headers=admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["account"]["status"] == "ACTIVE"

        resp = await client.post(
            "/api/v1/campaigns",
            json={
                "name": "Q1 Outreach",
                "channel": "EMAIL",
                "template_id": str(template.id),
                "sending_account_id": str(account.id),
                "audience": {"require_opt_in": False,
                             "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        campaign = resp.json()["data"]["campaign"]

        resp = await client.post(f"/api/v1/campaigns/{campaign['id']}/validate", headers=admin_headers)
        assert resp.json()["data"]["validation"]["ok"] is True

        resp = await client.post(f"/api/v1/campaigns/{campaign['id']}/launch", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["launch"]["queued"]["queued"] == 1

        recipients = (
            await client.get(f"/api/v1/campaigns/{campaign['id']}/recipients", headers=admin_headers)
        ).json()["data"]
        assert recipients["total"] == 1

        analytics = (
            await client.get(f"/api/v1/campaigns/{campaign['id']}/email/analytics", headers=admin_headers)
        ).json()["data"]["analytics"]
        assert analytics["verified"]["recipients_total"] == 1

    async def test_launch_requires_launch_permission(self, client, admin_headers, session):
        account = await make_mock_account(session)
        template = await make_template(session)
        await session.commit()
        resp = await client.post(
            "/api/v1/campaigns",
            json={
                "name": "No Perm",
                "channel": "EMAIL",
                "template_id": str(template.id),
                "sending_account_id": str(account.id),
                "audience": {"require_opt_in": False},
            },
            headers=admin_headers,
        )
        campaign = resp.json()["data"]["campaign"]

        # viewer lacks campaigns.email.launch
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "viewer@qbit.example.com", "password": "V13werSecret!Pass"},
        )
        viewer = {"Authorization": f"Bearer {login.json()['access_token']}"}
        resp = await client.post(f"/api/v1/campaigns/{campaign['id']}/launch", headers=viewer)
        assert resp.status_code == 403
