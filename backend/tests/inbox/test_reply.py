"""Phase 8 reply pipeline tests: idempotency (§25), WhatsApp window rules
(§22), suppression gate, outbox delivery through the provider registry (§24),
retry (§26) and campaign→conversation linkage (§61)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models.marketing import Campaign, CampaignRecipient, SendingAccount
from app.models.messaging import Conversation, InboxOutboxItem, Message
from app.services.inbox.engine import ConversationEngine
from app.services.inbox.normalizer import normalize_whatsapp_inbound
from app.services.inbox.outbox import OutboxService
from app.services.inbox.reply import ReplyService, TemplateRequiredError
from app.services.marketing import build_provider_registry
from tests.marketing.conftest import make_lead, seed_account
from app.services.scraping.lead_keys import normalize_email, normalize_phone

pytestmark = pytest.mark.asyncio


def _settings(**overrides) -> Settings:
    fields = dict(
        QBIT_ENV="test", QBIT_SECRET_KEY="x" * 50, _env_file=None,
    )
    fields.update(overrides)
    return Settings(**fields)


async def _conversation_with_lead(session, *, channel="WHATSAPP", provider="mock"):
    from tests.marketing.conftest import seed_leads

    leads = await seed_leads(session, 1)
    account = await seed_account(session, channel=channel, provider=provider)
    engine = ConversationEngine()
    if channel == "WHATSAPP":
        conversation = Conversation(
            channel="WHATSAPP", sending_account_id=account.id,
            lead_id=leads[0].id, contact_phone=leads[0].phone,
            external_contact_id=leads[0].phone,
            status="OPEN", match_status="MATCHED",
            last_inbound_at=datetime.now(timezone.utc),
        )
    else:
        conversation = Conversation(
            channel="EMAIL", sending_account_id=account.id,
            lead_id=leads[0].id, contact_email=leads[0].email,
            external_contact_id=leads[0].email,
            status="OPEN", match_status="MATCHED",
            last_inbound_at=datetime.now(timezone.utc),
        )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation, account, leads[0]


# ------------------------------------------------------------ idempotency
async def test_reply_double_click_sends_once(seeded_db, app):
    conversation, account, lead = await _conversation_with_lead(seeded_db)
    replies = ReplyService()
    settings = _settings()

    message1, created1 = await replies.queue_reply(
        seeded_db, conversation, user_id=None, body="Hello!",
        client_message_id="client-1", settings=settings,
    )
    message2, created2 = await replies.queue_reply(
        seeded_db, conversation, user_id=None, body="Hello!",
        client_message_id="client-1", settings=settings,
    )
    assert created1 is True
    assert created2 is False
    assert message1.id == message2.id
    outbox = (await seeded_db.execute(select(InboxOutboxItem))).scalars().all()
    assert len(outbox) == 1
    assert outbox[0].idempotency_key == f"inbox:{conversation.id}:client-1"


# ---------------------------------------------------------- window rules
async def test_whatsapp_reply_outside_window_requires_template(seeded_db, app):
    conversation, account, lead = await _conversation_with_lead(seeded_db)
    conversation.last_inbound_at = datetime.now(timezone.utc) - timedelta(hours=30)
    await seeded_db.commit()
    replies = ReplyService()

    with pytest.raises(TemplateRequiredError) as excinfo:
        await replies.queue_reply(
            seeded_db, conversation, user_id=None, body="Hi!",
            client_message_id="client-x", settings=_settings(),
        )
    assert excinfo.value.code == "TEMPLATE_REQUIRED"


async def test_whatsapp_reply_inside_window_queues_text(seeded_db, app):
    conversation, account, lead = await _conversation_with_lead(seeded_db)
    replies = ReplyService()
    message, created = await replies.queue_reply(
        seeded_db, conversation, user_id=None, body="Quick answer!",
        client_message_id="client-2", settings=_settings(),
    )
    assert created is True
    assert message.status == "SENDING"
    assert message.direction == "OUTBOUND"
    assert message.message_type == "TEXT"


# ------------------------------------------------------- suppression gate
async def test_suppressed_recipient_rejected(seeded_db, app):
    from app.models.marketing import SuppressionEntry, SuppressionType

    conversation, account, lead = await _conversation_with_lead(seeded_db)
    seeded_db.add(SuppressionEntry(
        type=SuppressionType.PHONE.value, address=lead.phone,
        channel="WHATSAPP", channel_key="WHATSAPP", reason="MANUAL",
        source="test",
    ))
    await seeded_db.commit()
    replies = ReplyService()
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError) as excinfo:
        await replies.queue_reply(
            seeded_db, conversation, user_id=None, body="Hi!",
            client_message_id="client-s", settings=_settings(),
        )
    assert "RECIPIENT_SUPPRESSED" in str(excinfo.value.message)


# ---------------------------------------------------------- outbox worker
async def test_outbox_delivers_through_provider(seeded_db, app):
    conversation, account, lead = await _conversation_with_lead(seeded_db)
    replies = ReplyService()
    settings = _settings()
    message, _ = await replies.queue_reply(
        seeded_db, conversation, user_id=None, body="Deliver me",
        client_message_id="client-3", settings=settings,
    )
    worker = OutboxService(settings, build_provider_registry(settings), owner="t")
    processed = await worker.process_cycle(seeded_db)
    assert processed == 1
    await seeded_db.refresh(message)
    assert message.status == "SENT"
    assert message.sent_at is not None
    assert (message.provider_message_id or "").startswith("mock-")


async def test_outbox_permanent_failure_fails_message(seeded_db, app):
    conversation, account, lead = await _conversation_with_lead(seeded_db)
    conversation.contact_phone = "+1555fail000"  # mock provider fails on 'fail'
    await seeded_db.commit()
    replies = ReplyService()
    settings = _settings()
    message, _ = await replies.queue_reply(
        seeded_db, conversation, user_id=None, body="Will fail",
        client_message_id="client-4", settings=settings,
    )
    worker = OutboxService(settings, build_provider_registry(settings), owner="t")
    await worker.process_cycle(seeded_db)
    await seeded_db.refresh(message)
    assert message.status == "FAILED"
    assert message.failed_at is not None


async def test_retry_only_confirmed_failed(seeded_db, app):
    conversation, account, lead = await _conversation_with_lead(seeded_db)
    conversation.contact_phone = "+1555fail000"
    await seeded_db.commit()
    replies = ReplyService()
    settings = _settings()
    message, _ = await replies.queue_reply(
        seeded_db, conversation, user_id=None, body="Retry me",
        client_message_id="client-5", settings=settings,
    )
    # a SENDING message cannot be retried (§26 — no duplicate provider request)
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError):
        await replies.retry_failed(
            seeded_db, conversation=conversation, message=message, settings=settings,
        )
    # fail it, then retry re-queues the SAME message row
    worker = OutboxService(settings, build_provider_registry(settings), owner="t")
    await worker.process_cycle(seeded_db)
    await seeded_db.refresh(message)
    assert message.status == "FAILED"
    retried = await replies.retry_failed(
        seeded_db, conversation=conversation, message=message, settings=settings,
    )
    assert retried.id == message.id
    assert retried.status == "SENDING"


# ------------------------------------------------- campaign linkage (§61)
async def test_campaign_send_appears_in_conversation(seeded_db, app):
    conversation, account, lead = await _conversation_with_lead(seeded_db)
    engine = ConversationEngine()

    campaign = Campaign(
        name="Test campaign", channel="WHATSAPP",
        sending_account_id=account.id, status="RUNNING",
        created_by=None,
    )
    seeded_db.add(campaign)
    await seeded_db.flush()
    recipient = CampaignRecipient(
        campaign_id=campaign.id, lead_id=lead.id,
        recipient_address=lead.phone, status="SENT",
        sent_at=datetime.now(timezone.utc),
        provider_message_id="wamid-camp-9",
    )
    seeded_db.add(recipient)
    await seeded_db.commit()

    result = await engine.ingest_campaign_outbound(
        seeded_db, campaign=campaign, recipient=recipient, account=account,
        provider_message_id="wamid-camp-9", subject=None,
        body="Template body text", message_type="TEMPLATE",
    )
    assert result is not None
    linked_conversation, message = result
    assert linked_conversation.id == conversation.id  # same thread
    assert message.direction == "OUTBOUND"
    assert message.provider_message_id == "wamid-camp-9"
    assert (message.message_metadata or {}).get("campaign_id") == str(campaign.id)
    await seeded_db.refresh(conversation)
    assert conversation.unread_count == 0  # outbound never increments unread


# ----------------------------------------------- email reply threading (§23)
async def test_email_reply_preserves_threading_headers(seeded_db, app):
    conversation, account, lead = await _conversation_with_lead(
        seeded_db, channel="EMAIL", provider="mock",
    )
    inbound = Message(
        conversation_id=conversation.id, direction="INBOUND", status="RECEIVED",
        message_type="EMAIL", body="Question about pricing",
        message_metadata={"message_id": "<in-1@acme.test>", "references": "<in-0@acme.test>"},
    )
    seeded_db.add(inbound)
    await seeded_db.commit()

    replies = ReplyService()
    message, created = await replies.queue_reply(
        seeded_db, conversation, user_id=None, body="Here is the pricing",
        client_message_id="client-mail-1", settings=_settings(),
    )
    assert created is True
    metadata = message.message_metadata or {}
    assert metadata["in_reply_to"] == "<in-1@acme.test>"
    assert "<in-0@acme.test>" in (metadata.get("references") or "")
    assert message.subject  # default subject present
