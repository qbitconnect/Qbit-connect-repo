"""Phase 6 tests — WhatsApp campaign launch gates (§8, §10, §12, §13, §27, §29)
and the end-to-end send path through WhatsAppMockProvider (§41).

Covers: provider template approval requirement, unhealthy account gate,
capability checks, opt-in enforcement, suppression pre-send check, worker
send with template payload, idempotency, retry-after respect, analytics
updates from delivery events.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.marketing import (
    Campaign,
    CampaignEvent,
    CampaignQueueItem,
    CampaignRecipient,
    CampaignStatus,
    CampaignTemplate,
    EventType,
    QueueStatus,
    RecipientStatus,
    SendingAccount,
    SuppressionEntry,
)
from app.services.marketing.campaign import CampaignService
from app.services.marketing.credentials import CredentialVault
from app.services.marketing.worker import CampaignWorker
from tests.marketing.conftest import make_lead, seed_leads

pytestmark = pytest.mark.asyncio


async def _wa_account(session, app, *, provider: str = "whatsapp_mock", health: str = "HEALTHY",
                      status: str = "ACTIVE", with_credentials: bool = True,
                      capabilities: dict | None = None) -> SendingAccount:
    account = SendingAccount(
        name=f"WA {uuid.uuid4().hex[:6]}", channel="WHATSAPP", provider=provider,
        identifier="+910000000000", phone_number_id="111222333",
        business_account_id="999999999999", status=status,
        capabilities=capabilities if capabilities is not None else {
            "supports_templates": True, "supports_media": False,
            "supports_inbound": True, "supports_webhooks": True,
        },
        config_metadata={"configured": True, "phone_number_id": "111222333",
                         "business_account_id": "999999999999"},
        health_status=health,
    )
    if with_credentials:
        vault = CredentialVault(app.state.settings.QBIT_SECRET_KEY)
        cred = await vault.store(
            session, name=f"whatsapp-test:{uuid.uuid4().hex[:8]}", provider=provider,
            payload={"access_token": "EAAG-test-token-0000000001"},
        )
        account.credential_ref = cred.name
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def _provider_template(session, account: SendingAccount, *,
                             provider_status: str = "APPROVED",
                             variables: list[str] | None = None,
                             placeholders: dict | None = None) -> CampaignTemplate:
    template = CampaignTemplate(
        name=f"welcome_business_{uuid.uuid4().hex[:6]}", channel="WHATSAPP",
        body="Hello {{1}}, welcome to {{2}}!",
        status="ACTIVE" if provider_status == "APPROVED" else "DRAFT",
        language="en",
        variables=variables if variables is not None else ["contact_name", "business_name"],
        origin="PROVIDER", provider_template_id=f"tpl-{uuid.uuid4().hex[:8]}",
        provider_status=provider_status, category="MARKETING",
        components={"raw": [{"type": "BODY", "text": "Hello {{1}}, welcome to {{2}}!"}],
                    "placeholders": placeholders or {"body": 2, "header": 0}},
        account_id=account.id,
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


async def _campaign(session, *, account: SendingAccount, template: CampaignTemplate,
                    leads: list) -> Campaign:
    campaign = Campaign(
        name=f"Campaign {uuid.uuid4().hex[:6]}", channel="WHATSAPP",
        status=CampaignStatus.DRAFT,
        audience_definition={"type": "selected", "lead_ids": [str(lead.id) for lead in leads]},
        template_id=template.id, sending_account_id=account.id,
    )
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    return campaign


def _worker(app) -> CampaignWorker:
    return CampaignWorker(app.state.settings, app.state.marketing_providers)


# ----------------------------------------------------------- §10 approval gate
async def test_whatsapp_requires_provider_approved_template(app, seeded_db):
    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 2)
    template = await _provider_template(seeded_db, account, provider_status="PENDING")
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)

    report = await CampaignService().validate(seeded_db, campaign.id,
                                              provider_registry=app.state.marketing_providers)
    checks = report["checks"]
    assert report["ok"] is False
    assert checks["template_requirements"]["status"] == "FAIL"
    assert "APPROVED" in checks["template_requirements"]["detail"]


async def test_whatsapp_rejects_local_template(app, seeded_db):
    from tests.marketing.conftest import seed_template

    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 2)
    local_template = await seed_template(seeded_db, channel="WHATSAPP")
    campaign = await _campaign(seeded_db, account=account, template=local_template, leads=leads)

    report = await CampaignService().validate(seeded_db, campaign.id,
                                              provider_registry=app.state.marketing_providers)
    assert report["ok"] is False
    assert "provider-synced" in report["checks"]["template_requirements"]["detail"]


# ------------------------------------------------- §12 opt-in / §13 suppression
async def test_no_opt_in_blocks_launch(app, seeded_db):
    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 2)
    meta = dict(leads[0].metadata_json or {})
    meta["marketing_opt_in"] = False
    meta.pop("opt_in_status", None)
    leads[0].metadata_json = meta
    await seeded_db.commit()

    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)
    report = await CampaignService().validate(seeded_db, campaign.id,
                                              provider_registry=app.state.marketing_providers)
    assert report["eligibility"]["no_opt_in"] == 1
    assert report["eligibility"]["eligible"] == 1  # the opted-in one

    # opt_in_status variant accepted (§12 metadata model)
    meta2 = dict(leads[1].metadata_json or {})
    meta2.pop("marketing_opt_in", None)
    meta2["opt_in_status"] = "OPTED_IN"
    meta2["opt_in_source"] = "web-form"
    leads[1].metadata_json = meta2
    await seeded_db.commit()
    report = await CampaignService().validate(seeded_db, campaign.id,
                                              provider_registry=app.state.marketing_providers)
    assert report["eligibility"]["eligible"] == 1  # still only the original opt-in


async def test_suppressed_recipient_never_queued(app, seeded_db):
    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 2)
    suppressed = SuppressionEntry(
        type="PHONE", address="+919876543200", channel="WHATSAPP", channel_key="WHATSAPP",
        reason="MANUAL",
    )
    seeded_db.add(suppressed)
    await seeded_db.commit()

    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)
    # suppress the FIRST lead's phone
    campaign.audience_definition = {"type": "selected", "lead_ids": [str(leads[0].id)]}
    await seeded_db.commit()
    report = await CampaignService().validate(seeded_db, campaign.id,
                                              provider_registry=app.state.marketing_providers)
    assert report["eligibility"]["eligible"] == 0
    assert report["eligibility"]["suppressed"] == 1
    # and the launch refuses to queue a fully-suppressed audience (§13)
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        await CampaignService().request_launch(
            seeded_db, campaign.id, provider_registry=app.state.marketing_providers)


# ------------------------------------------------------------ §29 health gate
async def test_unhealthy_account_blocks_launch(app, seeded_db):
    account = await _wa_account(seeded_db, app, health="UNHEALTHY")
    leads = await seed_leads(seeded_db, 2)
    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)

    service = CampaignService()
    report = await service.validate(seeded_db, campaign.id,
                                    provider_registry=app.state.marketing_providers)
    assert report["checks"]["sending_account_health"]["detail"] == "SENDING_ACCOUNT_UNHEALTHY"
    assert report["ok"] is False
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        await service.request_launch(seeded_db, campaign.id,
                                     provider_registry=app.state.marketing_providers)
    assert campaign.status == CampaignStatus.DRAFT  # never queued


# ------------------------------------------------------ §27 capability check
async def test_capability_gate_blocks_non_template_account(app, seeded_db):
    account = await _wa_account(seeded_db, app,
                                capabilities={"supports_templates": False})
    leads = await seed_leads(seeded_db, 1)
    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)
    report = await CampaignService().validate(seeded_db, campaign.id,
                                              provider_registry=app.state.marketing_providers)
    assert report["ok"] is False
    assert "template messaging" in report["checks"]["sending_account_capabilities"]["detail"]


# ------------------------------------------- §41 end-to-end send (mock WA API)
async def test_end_to_end_send_delivery_read_flow(app, seeded_db):
    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 3)
    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)

    service = CampaignService()
    worker = _worker(app)

    campaign = await service.request_launch(seeded_db, campaign.id,
                                            provider_registry=app.state.marketing_providers)
    assert campaign.status == CampaignStatus.QUEUED
    await worker.process_cycle(seeded_db)  # launch pipeline: snapshot → eligibility → queue
    await worker.process_cycle(seeded_db)  # send batch

    recipients = (await seeded_db.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    )).scalars().all()
    assert len(recipients) == 3
    sent = [r for r in recipients if r.status == RecipientStatus.SENT]
    assert len(sent) == 3
    assert all(r.provider_message_id and r.provider_message_id.startswith("wamid.mock") for r in sent)
    await seeded_db.refresh(campaign)
    assert campaign.status == CampaignStatus.COMPLETED


from app.models.marketing import CampaignRecipient as RecipientRow  # noqa: E402


async def test_send_failure_permanent_marks_failed(app, seeded_db):
    """§41: recipient marked as not-on-WhatsApp → INVALID_RECIPIENT PERMANENT → FAILED."""
    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 1, phone="+91987600000000")

    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)
    service = CampaignService()
    worker = _worker(app)
    await service.request_launch(seeded_db, campaign.id,
                                 provider_registry=app.state.marketing_providers)
    await worker.process_cycle(seeded_db)
    await worker.process_cycle(seeded_db)

    recipients = (await seeded_db.execute(
        select(RecipientRow).where(RecipientRow.campaign_id == campaign.id)
    )).scalars().all()
    assert len(recipients) == 1
    assert recipients[0].status == RecipientStatus.FAILED
    # the permanent failure event carries the normalized provider code
    events = (await seeded_db.execute(
        select(CampaignEvent).where(
            CampaignEvent.recipient_id == recipients[0].id,
            CampaignEvent.event_type == EventType.MESSAGE_FAILED,
        )
    )).scalars().all()
    assert events and any("INVALID_RECIPIENT" in str(e.payload_metadata) for e in events)


async def test_missing_template_variable_skips_recipient(app, seeded_db):
    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 1)
    leads[0].contact_name = None  # variable cannot render
    await seeded_db.commit()
    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)

    service = CampaignService()
    worker = _worker(app)
    await service.request_launch(seeded_db, campaign.id,
                                 provider_registry=app.state.marketing_providers)
    await worker.process_cycle(seeded_db)
    await worker.process_cycle(seeded_db)

    recipients = (await seeded_db.execute(
        select(RecipientRow).where(RecipientRow.campaign_id == campaign.id)
    )).scalars().all()
    assert recipients[0].status == RecipientStatus.SKIPPED
    assert recipients[0].skip_reason == "MISSING_TEMPLATE_VARIABLE"


async def test_idempotent_send_worker_restart(app, seeded_db):
    """§15/§20: a worker restart re-running the cycle cannot duplicate sends."""
    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 2)
    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)
    service = CampaignService()
    worker = _worker(app)
    await service.request_launch(seeded_db, campaign.id,
                                 provider_registry=app.state.marketing_providers)
    await worker.process_cycle(seeded_db)
    await worker.process_cycle(seeded_db)
    # simulate a worker restart reprocessing everything
    await worker.process_cycle(seeded_db)
    await worker.process_cycle(seeded_db)

    queue_items = (await seeded_db.execute(
        select(CampaignQueueItem).where(CampaignQueueItem.campaign_id == campaign.id)
    )).scalars().all()
    completed = [i for i in queue_items if i.status == QueueStatus.COMPLETED]
    assert len(completed) == 2
    sent_events = (await seeded_db.execute(
        select(CampaignEvent).where(
            CampaignEvent.campaign_id == campaign.id,
            CampaignEvent.event_type == EventType.MESSAGE_SENT,
        )
    )).scalars().all()
    assert len(sent_events) == 2  # exactly one per recipient


# ----------------------------------------------------------------- §30 rates
async def test_retry_after_respected(app, seeded_db):
    """Provider retry-after pushes the next attempt no earlier than allowed."""
    from app.core.config import Settings

    settings = Settings(QBIT_ENV="test", QBIT_MARKETING_RETRY_BASE_SECONDS=1.0)
    from app.services.marketing.queue import QueueService
    from app.models.marketing import CampaignQueueItem

    item = CampaignQueueItem(campaign_id=uuid.uuid4(), recipient_id=uuid.uuid4(),
                             channel="WHATSAPP", attempts=1)
    seeded_db.add(item)
    await seeded_db.commit()
    await QueueService().fail(seeded_db, item, error="rate limited",
                              error_class="TRANSIENT", settings=settings,
                              provider_retry_after=600.0)
    wait = (item.available_at - datetime.now(timezone.utc)).total_seconds()
    assert wait >= 590  # at least the provider's hint (bounded test tolerance)


# --------------------------------------------------------- webhook → analytics
async def test_delivery_events_flow_into_analytics(app, seeded_db):
    from app.services.marketing.webhooks import WhatsAppWebhookService

    account = await _wa_account(seeded_db, app)
    leads = await seed_leads(seeded_db, 1)
    template = await _provider_template(seeded_db, account)
    campaign = await _campaign(seeded_db, account=account, template=template, leads=leads)
    service = CampaignService()
    worker = _worker(app)
    await service.request_launch(seeded_db, campaign.id,
                                 provider_registry=app.state.marketing_providers)
    await worker.process_cycle(seeded_db)
    await worker.process_cycle(seeded_db)

    recipient = (await seeded_db.execute(
        select(RecipientRow).where(RecipientRow.campaign_id == campaign.id)
    )).scalars().one()
    wamid = recipient.provider_message_id
    assert wamid

    # simulate provider delivery webhooks (signature bypassed: direct service)
    webhook_service = WhatsAppWebhookService(app.state.settings)
    ts = int(datetime.now(timezone.utc).timestamp())
    base_value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "+910000000000", "phone_number_id": "111222333"},
        "contacts": [], "messages": [],
    }
    for status in ("delivered", "read"):
        value = dict(base_value)
        value["statuses"] = [{"id": wamid, "status": status, "timestamp": ts,
                              "recipient_id": "919876543200"}]
        payload = {"object": "whatsapp_business_account",
                   "entry": [{"id": "999", "changes": [{"field": "messages", "value": value}]}]}
        summary = await webhook_service.process_payload(seeded_db, payload)
        assert summary["applied"] == 1

    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.READ

    from app.services.marketing.analytics import AnalyticsService

    analytics = await AnalyticsService().campaign_analytics(seeded_db, campaign.id)
    assert analytics["messages"]["read"] == 1
    assert analytics["messages"]["delivered"] == 1
    assert analytics["messages"]["sent"] == 1
    assert analytics["rates"]["read_rate"] == 1.0
