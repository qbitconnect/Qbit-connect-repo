"""Campaign lifecycle, queue, retry, idempotency, analytics (Phase 5 §16-§24)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.core.errors import ConflictError, ValidationError
from app.services.marketing import CampaignService, build_provider_registry
from app.services.marketing.queue import QueueService
from app.services.marketing.worker import CampaignWorker
from tests.marketing.conftest import seed_account, seed_leads, seed_template


def _settings() -> Settings:
    # Phase 7: the EMAIL send path requires a REAL unsubscribe base URL
    # (compliance guard) — the test environment provides one so legacy
    # Phase 5 send-loop scenarios exercise the full pipeline unchanged.
    return Settings(
        QBIT_ENV="test",
        QBIT_EMAIL_UNSUBSCRIBE_BASE_URL="https://qbit.test",
        _env_file=None,
    )


def _registry() -> object:
    return build_provider_registry(_settings())


def _worker(session=None) -> CampaignWorker:
    return CampaignWorker(_settings(), _registry(), owner="test-worker")


@pytest.fixture
def campaigns() -> CampaignService:
    return CampaignService()


async def _make_launched_campaign(
    session, *, channel: str = "WHATSAPP", lead_count: int = 5,
    provider: str = "mock", configured: bool = True,
) -> "object":
    leads = await seed_leads(session, lead_count)
    account = await seed_account(session, provider=provider, channel=channel,
                                 configured=configured)
    template = await seed_template(session, channel=channel)
    campaigns = CampaignService()
    if lead_count > 0:
        definition = {"type": "selected", "lead_ids": [str(l.id) for l in leads]}
    else:
        definition = {"type": "filters",
                      "filters": {"field": "business_name", "op": "eq", "value": "__none__"}}
    campaign = await campaigns.create(
        session,
        name=f"Launch {uuid.uuid4().hex[:6]}", channel=channel,
        audience_definition=definition,
        template_id=template.id, sending_account_id=account.id,
        created_by=None,
    )
    return campaign, leads, account, template


class TestCampaignCrud:
    async def test_create_requires_known_channel(self, seeded_db, campaigns):
        with pytest.raises(ValidationError):
            await campaigns.create(seeded_db, name="X", channel="PIGEON")

    async def test_create_and_get(self, seeded_db, campaigns):
        campaign = await campaigns.create(seeded_db, name="Hello", channel="EMAIL")
        assert campaign.status == "DRAFT"
        fetched = await campaigns.get(seeded_db, campaign.id)
        assert fetched.id == campaign.id

    async def test_editable_only_in_draft_or_scheduled(self, seeded_db, campaigns):
        campaign = await campaigns.create(seeded_db, name="Edit me", channel="SMS")
        campaign.status = "RUNNING"
        await seeded_db.commit()
        with pytest.raises(ConflictError):
            await campaigns.update(seeded_db, campaign.id, name="New name")

    async def test_template_channel_must_match(self, seeded_db, campaigns):
        template = await seed_template(seeded_db, channel="EMAIL")
        with pytest.raises(ValidationError):
            await campaigns.create(
                seeded_db, name="X", channel="WHATSAPP", template_id=template.id,
            )

    async def test_scheduled_requires_time(self, seeded_db, campaigns):
        with pytest.raises(ValidationError):
            await campaigns.create(seeded_db, name="X", channel="EMAIL",
                                   schedule_type="SCHEDULED")


class TestValidation:
    async def test_full_validation_report(self, seeded_db, campaigns):
        campaign, leads, account, template = await _make_launched_campaign(seeded_db)
        report = await campaigns.validate(seeded_db, campaign.id, provider_registry=_registry())
        assert report["ok"] is True
        assert report["eligibility"]["eligible"] == 5
        assert report["eligibility"]["skipped"] == 0
        checks = report["checks"]
        assert all(c["status"] == "PASS" for c in checks.values())

    async def test_validation_fails_without_provider(self, seeded_db, campaigns):
        campaign, *_ = await _make_launched_campaign(
            seeded_db, provider="whatsapp_cloud", configured=False,
        )
        report = await campaigns.validate(seeded_db, campaign.id, provider_registry=_registry())
        assert report["ok"] is False
        assert report["checks"]["provider"]["status"] == "FAIL"
        assert "not configured" in report["checks"]["provider"]["detail"].lower()

        # launch must also be blocked
        with pytest.raises(ValidationError):
            await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())

    async def test_validation_counts_missing_and_opt_in(self, seeded_db, campaigns):
        from tests.marketing.conftest import make_lead
        from app.services.scraping.lead_keys import normalize_phone

        ok = make_lead()
        missing = make_lead(phone=None)
        no_optin = make_lead()
        no_optin.metadata_json = {}
        bad_phone = make_lead(phone="12")
        bad_phone.phone_norm = normalize_phone("12")
        seeded_db.add_all([ok, missing, no_optin, bad_phone])
        await seeded_db.commit()
        ids = [str(r.id) for r in (ok, missing, no_optin, bad_phone)]
        campaign = await campaigns.create(
            seeded_db, name="Mixed", channel="WHATSAPP",
            audience_definition={"type": "selected", "lead_ids": ids},
            created_by=None,
        )
        report = await campaigns.validate(seeded_db, campaign.id, provider_registry=_registry())
        e = report["eligibility"]
        assert e["eligible"] == 1
        assert e["missing_address"] == 2  # missing phone + invalid phone
        assert e["no_opt_in"] == 1


class TestLaunchPipeline:
    async def test_launch_blocks_without_provider(self, seeded_db, campaigns):
        campaign, *_ = await _make_launched_campaign(
            seeded_db, provider="whatsapp_cloud", configured=False,
        )
        with pytest.raises(ValidationError):
            await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())
        assert campaign.status == "DRAFT"

    async def test_launch_blocks_with_zero_eligible(self, seeded_db, campaigns):
        leads = await seed_leads(seeded_db, 2)
        for l in leads:
            l.metadata_json = {}  # nobody opted in
        await seeded_db.commit()
        campaign = await campaigns.create(
            seeded_db, name="Nobody", channel="WHATSAPP",
            audience_definition={"type": "selected", "lead_ids": [str(l.id) for l in leads]},
        )
        with pytest.raises(ValidationError):
            await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())

    async def test_launch_snapshot_eligibility_queue(self, seeded_db, campaigns):
        from sqlalchemy import func, select

        from app.models.marketing import CampaignQueueItem, CampaignRecipient, RecipientStatus

        campaign, leads, account, template = await _make_launched_campaign(seeded_db, lead_count=4)
        # one lead loses consent between validate and launch
        leads[0].metadata_json = {}
        await seeded_db.commit()

        armed = await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())
        assert armed.status == "QUEUED"
        result = await campaigns.process_launch(
            seeded_db, armed, provider_registry=_registry(),
            batch_size=100, max_audience=1000,
        )
        assert armed.status == "RUNNING"
        assert result["created"] == 4
        assert result["queued"] == 3  # one skipped for NO_OPT_IN

        statuses = (await seeded_db.execute(
            select(CampaignRecipient.status, func.count())
            .where(CampaignRecipient.campaign_id == campaign.id)
            .group_by(CampaignRecipient.status)
        )).all()
        by_status = dict(statuses)
        assert by_status.get(RecipientStatus.ELIGIBLE) == 3
        assert by_status.get(RecipientStatus.INELIGIBLE) == 1
        queue_count = await seeded_db.scalar(select(func.count()).select_from(CampaignQueueItem))
        assert queue_count == 3

    async def test_scheduled_campaign_flips_when_due(self, seeded_db, campaigns):
        campaign, *_ = await _make_launched_campaign(seeded_db, lead_count=2)
        campaign.schedule_type = "SCHEDULED"
        campaign.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        campaign.status = "SCHEDULED"
        await seeded_db.commit()
        worker = _worker()
        flipped = await worker._process_schedules(seeded_db)
        assert flipped == 1
        assert campaign.status == "QUEUED"


class TestSendLoop:
    async def test_worker_sends_via_mock_provider(self, seeded_db, campaigns):
        from sqlalchemy import select

        from app.models.marketing import (
            CampaignEvent,
            CampaignQueueItem,
            EventType,
            QueueStatus,
            RecipientStatus,
        )

        campaign, leads, account, template = await _make_launched_campaign(seeded_db, lead_count=3)
        await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())
        worker = _worker()
        actions = await worker.process_cycle(seeded_db)
        assert actions >= 1

        assert campaign.status == "COMPLETED"
        items = (await seeded_db.execute(
            select(CampaignQueueItem).where(CampaignQueueItem.campaign_id == campaign.id)
        )).scalars().all()
        assert all(i.status == QueueStatus.COMPLETED for i in items)
        for lead in leads:
            await seeded_db.refresh(lead)
        recipients = (await seeded_db.execute(
            select(__import__("app.models.marketing", fromlist=["CampaignRecipient"]).CampaignRecipient)
        )).scalars().all()
        assert all(r.status == RecipientStatus.SENT for r in recipients)
        assert all(r.provider_message_id and r.provider_message_id.startswith("mock-") for r in recipients)
        events = (await seeded_db.execute(select(CampaignEvent))).scalars().all()
        types = {e.event_type for e in events}
        assert EventType.MESSAGE_SENT in types
        assert EventType.CAMPAIGN_COMPLETED in types

    async def test_mock_provider_never_in_production_registry(self):
        prod_settings = Settings(QBIT_ENV="production", QBIT_MARKETING_ALLOW_MOCK_PROVIDER=True, _env_file=None)
        registry = build_provider_registry(prod_settings)
        assert "mock" not in registry.ids()

    async def test_permanent_failure_fails_immediately(self, seeded_db, campaigns):
        from sqlalchemy import select

        from app.models.marketing import CampaignQueueItem, QueueStatus, RecipientStatus

        leads = await seed_leads(seeded_db, 2)
        for lead in leads:
            lead.email = "fail@acme.test"  # mock fails PERMANENT on "fail" in address
        await seeded_db.commit()
        campaign, *_ = await _make_launched_campaign(
            seeded_db, lead_count=0, channel="EMAIL",
        )
        campaign.audience_definition = {
            "type": "selected", "lead_ids": [str(l.id) for l in leads],
        }
        await seeded_db.commit()
        await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())
        worker = _worker()
        await worker.process_cycle(seeded_db)

        items = (await seeded_db.execute(
            select(CampaignQueueItem).where(CampaignQueueItem.campaign_id == campaign.id)
        )).scalars().all()
        assert len(items) == 2
        assert all(i.status == QueueStatus.FAILED for i in items)
        assert all(i.attempts == 1 for i in items)  # never retried

    async def test_transient_failure_retries_with_backoff(self, seeded_db, campaigns):
        from sqlalchemy import select

        from app.models.marketing import CampaignQueueItem, QueueStatus

        leads = await seed_leads(seeded_db, 1)
        leads[0].email = "flaky@acme.test"  # mock fails TRANSIENT on "flaky"
        await seeded_db.commit()
        campaign, *_ = await _make_launched_campaign(
            seeded_db, lead_count=0, channel="EMAIL",
        )
        campaign.audience_definition = {"type": "selected", "lead_ids": [str(leads[0].id)]}
        await seeded_db.commit()
        await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())
        worker = _worker()
        await worker.process_cycle(seeded_db)

        items = (await seeded_db.execute(
            select(CampaignQueueItem).where(CampaignQueueItem.campaign_id == campaign.id)
        )).scalars().all()
        assert len(items) == 1
        item = items[0]
        assert item.status == QueueStatus.RETRY
        assert item.attempts == 1
        available = item.available_at
        if available.tzinfo is None:
            available = available.replace(tzinfo=timezone.utc)
        assert available > datetime.now(timezone.utc)  # scheduled for later

    async def test_idempotency_duplicate_enqueue_is_noop(self, seeded_db, campaigns):
        from sqlalchemy import func, select

        from app.models.marketing import CampaignQueueItem

        campaign, leads, account, _t = await _make_launched_campaign(seeded_db, lead_count=3)
        await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())
        queue = QueueService(_settings())
        created1 = await queue.enqueue(
            seeded_db, campaign=campaign, recipient_ids=[l.id for l in leads],
            sending_account_id=account.id,
        )
        created2 = await queue.enqueue(
            seeded_db, campaign=campaign, recipient_ids=[l.id for l in leads],
            sending_account_id=account.id,
        )
        total = await seeded_db.scalar(select(func.count()).select_from(CampaignQueueItem))
        assert total == 3  # duplicates never insert
        assert created2 == 0


class TestPauseResumeCancel:
    async def _running_campaign(self, seeded_db, campaigns, lead_count=3):
        campaign, leads, account, template = await _make_launched_campaign(
            seeded_db, lead_count=lead_count
        )
        await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())
        await campaigns.process_launch(seeded_db, campaign, provider_registry=_registry())
        return campaign, leads

    async def test_pause_holds_and_resume_releases(self, seeded_db, campaigns):
        from sqlalchemy import func, select

        from app.models.marketing import CampaignQueueItem, QueueStatus

        campaign, _leads = await self._running_campaign(seeded_db, campaigns)
        await campaigns.pause(seeded_db, campaign.id)
        worker = _worker()
        await worker.process_cycle(seeded_db)
        waiting = await seeded_db.scalar(
            select(func.count()).select_from(CampaignQueueItem).where(
                CampaignQueueItem.campaign_id == campaign.id,
                CampaignQueueItem.status == QueueStatus.WAITING,
            )
        )
        assert waiting == 3  # nothing sent while paused

        await campaigns.resume(seeded_db, campaign.id)
        await worker.process_cycle(seeded_db)
        waiting_after = await seeded_db.scalar(
            select(func.count()).select_from(CampaignQueueItem).where(
                CampaignQueueItem.campaign_id == campaign.id,
                CampaignQueueItem.status == QueueStatus.WAITING,
            )
        )
        assert waiting_after == 0
        assert campaign.status == "COMPLETED"

    async def test_cancel_leaves_completed_events(self, seeded_db, campaigns):
        from sqlalchemy import select

        from app.models.marketing import (
            CampaignEvent,
            EventType,
            QueueStatus,
            RecipientStatus,
        )

        campaign, _leads = await self._running_campaign(seeded_db, campaigns, lead_count=2)
        worker = _worker()
        # send one, then cancel before the rest
        items = (await seeded_db.execute(
            select(__import__("app.models.marketing", fromlist=["CampaignQueueItem"]).CampaignQueueItem)
            .where(__import__("app.models.marketing", fromlist=["CampaignQueueItem"]).CampaignQueueItem.campaign_id == campaign.id)
        )).scalars().all()
        await worker._send_item(seeded_db, items[0])
        await campaigns.cancel(seeded_db, campaign.id)
        assert campaign.status == "CANCELLED"
        # the already-sent item keeps COMPLETED state and its event
        await seeded_db.refresh(items[0])
        assert items[0].status == QueueStatus.COMPLETED
        sent_events = (await seeded_db.execute(
            select(CampaignEvent).where(CampaignEvent.event_type == EventType.MESSAGE_SENT)
        )).scalars().all()
        assert len(sent_events) == 1

    async def test_invalid_transitions_refused(self, seeded_db, campaigns):
        campaign, _ = await self._running_campaign(seeded_db, campaigns, lead_count=1)
        with pytest.raises(ConflictError):
            await campaigns.resume(seeded_db, campaign.id)  # RUNNING, not PAUSED
        with pytest.raises(ConflictError):
            await campaigns.archive(seeded_db, campaign.id)  # RUNNING, not terminal
        draft = await campaigns.create(seeded_db, name="Draft", channel="EMAIL")
        with pytest.raises(ConflictError):
            await campaigns.pause(seeded_db, draft.id)  # DRAFT, not RUNNING


class TestAnalytics:
    async def test_analytics_from_real_events(self, seeded_db, campaigns):
        from sqlalchemy import select

        from app.models.marketing import CampaignRecipient

        campaign, leads, *_ = await _make_launched_campaign(seeded_db, lead_count=4)
        await campaigns.request_launch(seeded_db, campaign.id, provider_registry=_registry())
        worker = _worker()
        await worker.process_cycle(seeded_db)

        from app.services.marketing import AnalyticsService

        analytics = await AnalyticsService().campaign_analytics(seeded_db, campaign.id)
        assert analytics["recipients"]["total"] == 4
        assert analytics["messages"]["sent"] == 4
        assert analytics["rates"]["failure_rate"] == 0.0

        # simulate provider delivery events (event normalizer + forward-only)
        recipients = (await seeded_db.execute(
            select(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign.id)
        )).scalars().all()
        recipient = recipients[0]
        from app.services.marketing.worker import _apply_event_to_recipient

        applied = await _apply_event_to_recipient(seeded_db, recipient, {
            "event_type": "MESSAGE_DELIVERED",
            "provider_message_id": recipient.provider_message_id,
            "metadata": {},
        })
        assert applied == 1
        analytics2 = await AnalyticsService().campaign_analytics(seeded_db, campaign.id)
        assert analytics2["messages"]["delivered"] == 1
        assert analytics2["rates"]["delivery_rate"] == 0.25

    async def test_empty_campaign_reports_zeros(self, seeded_db, campaigns):
        from app.services.marketing import AnalyticsService

        campaign = await campaigns.create(seeded_db, name="Empty", channel="EMAIL")
        analytics = await AnalyticsService().campaign_analytics(seeded_db, campaign.id)
        assert analytics["recipients"]["total"] == 0
        assert analytics["rates"]["delivery_rate"] == 0.0
