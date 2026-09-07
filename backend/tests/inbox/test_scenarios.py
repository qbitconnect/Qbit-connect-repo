"""Phase 8 spec §55 test scenarios 1–7 + engine invariants.

Each scenario mirrors the spec's expected outcome exactly. The engine rules
they pin down: exact lead matching (never guessed), unresolved contacts stay
lead-less, MATCH_REVIEW_REQUIRED on ambiguous identity, duplicate webhooks
create one message, out-of-order delivery events never downgrade state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.messaging import Conversation, Message
from app.services.inbox.engine import ConversationEngine
from app.services.inbox.normalizer import normalize_email_inbound, normalize_whatsapp_inbound
from tests.marketing.conftest import make_lead, seed_account, seed_leads
from app.services.scraping.lead_keys import normalize_email, normalize_phone

pytestmark = pytest.mark.asyncio


async def _ingest_whatsapp(session, engine, account, phone, body, message_id="wamid-1", **kw):
    msg = normalize_whatsapp_inbound(
        sender_phone=phone, provider_message_id=message_id, message_type="text",
        body=body, occurred_at=kw.pop("occurred_at", None),
        external_contact_id=kw.pop("external_contact_id", None),
        metadata=kw.pop("metadata", None),
    )
    return await engine.ingest_inbound(session, msg, account=account,
                                       settings=kw.pop("settings", None))


# ------------------------------------------------------- scenario 1
async def test_scenario_1_existing_lead_whatsapp_inbound(seeded_db):
    """Existing Lead + WhatsApp inbound → same Lead + correct Conversation
    + unread = 1."""
    leads = await seed_leads(seeded_db, 1, phone="+919876543210", email="ravi@acme.test")
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()

    conversation, message, created = await _ingest_whatsapp(
        seeded_db, engine, account, "+919876543210", "Hello, I need pricing information."
    )
    assert created is True
    assert conversation.lead_id == leads[0].id
    assert conversation.match_status == "MATCHED"
    assert conversation.status == "OPEN"
    assert conversation.unread_count == 1
    assert conversation.last_inbound_at is not None
    assert message.provider_message_id == "wamid-1"

    # a second distinct message increments unread further
    await _ingest_whatsapp(seeded_db, engine, account, "+919876543210", "Please send details",
                           message_id="wamid-2")
    await seeded_db.refresh(conversation)
    assert conversation.unread_count == 2


# ------------------------------------------------------- scenario 2
async def test_scenario_2_existing_lead_email_reply_same_thread(seeded_db):
    """Existing Lead + Email reply → same email thread + same Lead + new Message."""
    leads = await seed_leads(seeded_db, 1, email="ravi.patel@acme.test", phone="+919876543210")
    account = await seed_account(seeded_db, channel="EMAIL", provider="mock")
    engine = ConversationEngine()

    first = normalize_email_inbound(
        from_email="ravi.patel@acme.test", to_email="sender@qbit.test",
        subject="Product Inquiry", body_text="Can you share the catalog?",
        provider_message_id="<mail-1@acme.test>",
    )
    conversation1, message1, _ = await engine.ingest_inbound(seeded_db, first, account=account)

    second = normalize_email_inbound(
        from_email="ravi.patel@acme.test", to_email="sender@qbit.test",
        subject="Re: Product Inquiry", body_text="Thanks — got it.",
        provider_message_id="<mail-2@acme.test>",
        in_reply_to="<mail-1@acme.test>", references="<mail-1@acme.test>",
    )
    conversation2, message2, _ = await engine.ingest_inbound(seeded_db, second, account=account)

    assert conversation2.id == conversation1.id          # same thread — no duplicate
    assert conversation2.lead_id == leads[0].id
    assert message2.message_metadata["in_reply_to"] == "<mail-1@acme.test>"
    assert conversation2.unread_count == 2
    assert conversation2.subject == "Product Inquiry"


# ------------------------------------------------------- scenario 3
async def test_scenario_3_unknown_whatsapp_number_unresolved_contact(seeded_db):
    """Unknown WhatsApp number → lead-less (unresolved) Conversation.
    No invented lead data."""
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()

    conversation, message, created = await _ingest_whatsapp(
        seeded_db, engine, account, "+15550001111",
        "Hi there", external_contact_id="Ethan Brooks",
    )
    assert created is True
    assert conversation.lead_id is None
    assert conversation.match_status == "UNMATCHED"
    assert conversation.status == "PENDING"
    # only provider-received data is stored
    assert conversation.contact_phone == "+15550001111"
    assert conversation.external_contact_id == "Ethan Brooks"
    count = (await seeded_db.execute(
        select(Message).where(Message.conversation_id == conversation.id)
    )).scalars().all()
    assert len(count) == 1


# ------------------------------------------------------- scenario 4
async def test_scenario_4_two_leads_same_email_match_review(seeded_db):
    """Two leads share one email → MATCH_REVIEW_REQUIRED, never silently attach."""
    from app.services.scraping.lead_keys import normalize_email

    for name in ("Alpha Pvt Ltd", "Beta Traders"):
        lead = make_lead(business_name=name, email="shared@acme.test",
                         email_norm=normalize_email("shared@acme.test"),
                         phone=None)
        seeded_db.add(lead)
    await seeded_db.commit()

    account = await seed_account(seeded_db, channel="EMAIL", provider="mock")
    engine = ConversationEngine()
    msg = normalize_email_inbound(
        from_email="shared@acme.test", to_email="sender@qbit.test",
        subject="Hi", body_text="Hello",
    )
    conversation, _message, _created = await engine.ingest_inbound(seeded_db, msg, account=account)
    assert conversation.lead_id is None
    assert conversation.match_status == "MATCH_REVIEW_REQUIRED"


# ------------------------------------------------------- scenario 5
async def test_scenario_5_duplicate_webhook_creates_one_message(seeded_db):
    """Duplicate webhook → ONE message only."""
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()

    conversation1, message1, created1 = await _ingest_whatsapp(
        seeded_db, engine, account, "+919876543210", "Hello", message_id="wamid-dup",
    )
    conversation2, message2, created2 = await _ingest_whatsapp(
        seeded_db, engine, account, "+919876543210", "Hello", message_id="wamid-dup",
    )
    assert created1 is True
    assert created2 is False
    assert message2.id == message1.id
    await seeded_db.refresh(conversation2)
    assert conversation2.unread_count == 1  # not double-counted


# ------------------------------------------------------- scenario 6
async def test_scenario_6_read_before_delivered_never_downgrades(seeded_db):
    """READ arrives before DELIVERED → final state does not downgrade."""
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()

    conversation = Conversation(
        channel="WHATSAPP", sending_account_id=account.id,
        contact_phone="+919876543210", status="OPEN", match_status="MATCHED",
    )
    seeded_db.add(conversation)
    await seeded_db.flush()
    message = Message(
        conversation_id=conversation.id, direction="OUTBOUND", status="SENT",
        provider_message_id="wamid-out-1", message_type="TEXT", body="Hi!",
        sent_at=datetime.now(timezone.utc),
    )
    seeded_db.add(message)
    await seeded_db.commit()

    delivered_first = await engine.apply_delivery_to_message(
        seeded_db, provider_message_id="wamid-out-1",
        event_type="MESSAGE_READ", occurred_at=datetime.now(timezone.utc),
    )
    assert delivered_first is True
    await seeded_db.refresh(message)
    assert message.status == "READ"
    assert message.read_at is not None

    # out-of-order DELIVERED arrives late → must NOT downgrade to DELIVERED
    await engine.apply_delivery_to_message(
        seeded_db, provider_message_id="wamid-out-1",
        event_type="MESSAGE_DELIVERED", occurred_at=datetime.now(timezone.utc),
    )
    await seeded_db.refresh(message)
    assert message.status == "READ"
    assert message.delivered_at is not None  # timestamp still first-write recorded


# ------------------------------------------------------- scenario 7 (API level)
async def test_scenario_7_user_without_reply_permission_cannot_send(app, client, viewer_headers, seeded_db):
    """User lacks inbox.reply → cannot send a reply (403)."""
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()
    conversation, _m, _c = await _ingest_whatsapp(
        seeded_db, engine, account, "+919876543210", "Hello there",
    )
    resp = await client.post(
        f"/api/v1/inbox/conversations/{conversation.id}/messages",
        headers=viewer_headers,
        json={"body": "Hi!", "client_message_id": "v1"},
    )
    assert resp.status_code == 403, resp.text


# ------------------------------------------------------- reopen rule (§32)
async def test_inbound_reply_reopens_resolved_conversation(seeded_db, app):
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()
    conversation, _m, _c = await _ingest_whatsapp(
        seeded_db, engine, account, "+919876543210", "Hello", message_id="wamid-a",
    )
    await engine.change_status(seeded_db, conversation, "RESOLVED")
    assert conversation.status == "RESOLVED"

    await _ingest_whatsapp(seeded_db, engine, account, "+919876543210", "Any update?",
                           message_id="wamid-b")
    await seeded_db.refresh(conversation)
    assert conversation.status == "OPEN"
    assert conversation.closed_at is None


async def test_inbound_reply_stays_closed_when_reopen_disabled(seeded_db, app):
    from app.core.config import Settings

    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()
    settings = Settings(QBIT_ENV="test", QBIT_INBOX_REOPEN_ON_REPLY=False,
                        QBIT_SECRET_KEY="x" * 50, _env_file=None)
    conversation, _m, _c = await _ingest_whatsapp(
        seeded_db, engine, account, "+919876543210", "Hello", message_id="wamid-a",
    )
    await engine.change_status(seeded_db, conversation, "CLOSED")
    msg = normalize_whatsapp_inbound(
        sender_phone="+919876543210", provider_message_id="wamid-b",
        message_type="text", body="Anyone there?",
    )
    await engine.ingest_inbound(seeded_db, msg, account=account, settings=settings)
    await seeded_db.refresh(conversation)
    assert conversation.status == "CLOSED"


# ------------------------------------------------------- window rule (§22)
async def test_whatsapp_window_state(seeded_db):
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()
    conversation, _m, _c = await _ingest_whatsapp(
        seeded_db, engine, account, "+919876543210", "Hello", message_id="wamid-w",
        occurred_at=datetime.now(timezone.utc),
    )
    assert engine.within_whatsapp_window(conversation, hours=24) is True
    assert engine.within_whatsapp_window(conversation, hours=0) is False

    old = normalize_whatsapp_inbound(
        sender_phone="+919876543210", provider_message_id="wamid-old",
        message_type="text", body="old msg",
        occurred_at=datetime.now(timezone.utc) - timedelta(hours=30),
    )
    # out-of-order OLD message must NOT regress last_inbound_at (§40/§41)
    await engine.ingest_inbound(seeded_db, old, account=account)
    await seeded_db.refresh(conversation)
    assert engine.within_whatsapp_window(conversation, hours=24) is True

    # an actually-old last inbound message → window closed (§22)
    conversation.last_inbound_at = datetime.now(timezone.utc) - timedelta(hours=30)
    await seeded_db.commit()
    assert engine.within_whatsapp_window(conversation, hours=24) is False


# ------------------------------------------------------- link/unlink/create
async def test_link_and_unlink_lead_preserves_history(seeded_db):
    leads = await seed_leads(seeded_db, 1, email="ravi@acme.test")
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()
    conversation, _m, _c = await _ingest_whatsapp(
        seeded_db, engine, account, "+15557770000", "Hello from nowhere",
    )
    assert conversation.lead_id is None

    await engine.link_lead(seeded_db, conversation, leads[0].id)
    assert conversation.lead_id == leads[0].id
    assert conversation.match_status == "MATCHED"
    assert conversation.status == "OPEN"
    # existing messages backfilled with the lead id — history preserved
    messages = (await seeded_db.execute(
        select(Message).where(Message.conversation_id == conversation.id)
    )).scalars().all()
    assert all(m.lead_id == leads[0].id for m in messages)

    await engine.unlink_lead(seeded_db, conversation)
    assert conversation.lead_id is None
    assert conversation.match_status == "UNMATCHED"
    assert conversation.status == "PENDING"
    # messages keep their history (immutability §4)
    messages = (await seeded_db.execute(
        select(Message).where(Message.conversation_id == conversation.id)
    )).scalars().all()
    assert len(messages) == 1


async def test_create_lead_uses_only_received_data(seeded_db):
    account = await seed_account(seeded_db, channel="WHATSAPP")
    engine = ConversationEngine()
    conversation, _m, _c = await _ingest_whatsapp(
        seeded_db, engine, account, "+15550002222", "Hello",
        external_contact_id="Ethan Brooks",
    )
    lead = await engine.create_lead_from_conversation(seeded_db, conversation)
    assert lead.phone == "+15550002222"
    assert lead.contact_name == "Ethan Brooks"  # provider-supplied display name
    assert lead.business_name is None           # never invented
    assert conversation.lead_id == lead.id
    assert conversation.status == "OPEN"
