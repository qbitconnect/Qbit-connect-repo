"""Phase 6 tests — WhatsApp webhooks (§17–§21) + inbound/conversations
(§22–§24) + webhook security (§42).

Covers: verification challenge, signature validation (missing/invalid/forged/
replayed), duplicate delivery protection, sent/delivered/read/failed events,
backward-transition rejection, inbound message → conversation → lead match,
and the strict no-secret-logging rules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.models.marketing import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    CampaignStatus,
    EventType,
    RecipientStatus,
)
from app.models.messaging import Conversation, Message, ProviderEvent
from app.services.marketing.state import can_transition
from tests.marketing.conftest import make_lead, seed_account, seed_leads

APP_SECRET = "unit-test-app-secret"


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _app_settings(app, *, secret: str = APP_SECRET, token: str = "verify-token-123"):
    app.state.settings.WHATSAPP_APP_SECRET = secret
    app.state.settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN = token
    return app


def _delivery_payload(wamid: str, status: str, phone_number_id: str = "111222333") -> dict:
    ts = int(datetime.now(timezone.utc).timestamp())
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "999999999999999",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "4915112345678",
                                 "phone_number_id": phone_number_id},
                    "contacts": [],
                    "messages": [],
                    "statuses": [{
                        "id": wamid, "status": status, "timestamp": ts,
                        "recipient_id": "4915112345678",
                    }],
                },
            }],
        }],
    }


def _inbound_payload(wamid: str, sender: str, body: str, phone_number_id: str = "111222333") -> dict:
    ts = int(datetime.now(timezone.utc).timestamp())
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "999999999999999",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "4915112345678",
                                 "phone_number_id": phone_number_id},
                    "contacts": [{"profile": {"name": "Ravi"}, "wa_id": sender}],
                    "messages": [{
                        "from": sender, "id": wamid, "timestamp": ts,
                        "text": {"body": body}, "type": "text",
                    }],
                    "statuses": [],
                },
            }],
        }],
    }


async def _post_webhook(client: AsyncClient, payload: dict, *, sign: bool = True,
                        secret: str = APP_SECRET, raw_body: bytes | None = None):
    body = raw_body if raw_body is not None else json.dumps(payload).encode()
    headers = {}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return await client.post("/api/v1/webhooks/whatsapp", content=body, headers=headers)


async def _seed_campaign_with_recipient(session, *, wamid: str, phone: str = "+919876543210"):
    """Campaign + ACTIVE account + SENT recipient for delivery-event tests."""
    from app.models.marketing import SendingAccount
    from app.services.scraping.lead_keys import normalize_phone

    account = SendingAccount(
        name="WA Webhook Test", channel="WHATSAPP", provider="whatsapp_cloud",
        identifier=phone, phone_number_id="111222333", status="ACTIVE",
        config_metadata={"configured": True},
    )
    session.add(account)
    lead = make_lead(phone=phone, phone_norm=normalize_phone(phone))
    session.add(lead)
    await session.flush()
    campaign = Campaign(
        name="Webhook campaign", channel="WHATSAPP", status=CampaignStatus.COMPLETED,
        sending_account_id=account.id,
    )
    session.add(campaign)
    await session.flush()
    recipient = CampaignRecipient(
        campaign_id=campaign.id, lead_id=lead.id, recipient_address=phone,
        status=RecipientStatus.SENT, sent_at=datetime.now(timezone.utc),
        provider_message_id=wamid,
    )
    session.add(recipient)
    await session.commit()
    return account, campaign, recipient


# --------------------------------------------------------- §19 GET challenge
async def test_verification_challenge_success(client, app):
    _app_settings(app)
    resp = await client.get(
        "/api/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-token-123",
                "hub.challenge": "CHALLENGE_123"},
    )
    assert resp.status_code == 200
    assert resp.text == "CHALLENGE_123"


async def test_verification_challenge_wrong_token(client, app):
    _app_settings(app)
    resp = await client.get(
        "/api/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "X"},
    )
    assert resp.status_code == 401


async def test_verification_challenge_wrong_mode(client, app):
    _app_settings(app)
    resp = await client.get(
        "/api/v1/webhooks/whatsapp",
        params={"hub.mode": "denied", "hub.verify_token": "verify-token-123", "hub.challenge": "X"},
    )
    assert resp.status_code == 401


async def test_verification_challenge_unconfigured(app, client):
    app.state.settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN = None
    resp = await client.get(
        "/api/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "X"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------- §18/§42 POST auth
async def test_post_without_signature_rejected(client, app, seeded_db):
    _app_settings(app)
    resp = await _post_webhook(client, _delivery_payload("wamid-x", "delivered"), sign=False)
    assert resp.status_code == 401


async def test_post_with_invalid_signature_rejected(client, app, seeded_db):
    _app_settings(app)
    payload = _delivery_payload("wamid-x", "delivered")
    resp = await _post_webhook(client, payload, secret="attacker-secret")
    assert resp.status_code == 401


async def test_post_without_configured_app_secret_rejected(client, app, seeded_db):
    app.state.settings.WHATSAPP_APP_SECRET = None
    resp = await _post_webhook(client, _delivery_payload("wamid-x", "delivered"))
    assert resp.status_code == 401


async def test_post_with_garbage_body_rejected(client, app, seeded_db):
    _app_settings(app)
    resp = await _post_webhook(client, {}, raw_body=b"not-json{{{")
    assert resp.status_code in (400, 422)  # rejected either way


# ------------------------------------------------ §17/§20/§21 delivery events
async def test_delivered_event_updates_recipient(client, app, seeded_db):
    _app_settings(app)
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-d1")
    resp = await _post_webhook(client, _delivery_payload("wamid-d1", "delivered"))
    assert resp.status_code == 200
    assert resp.json()["data"]["applied"] == 1
    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.DELIVERED
    assert recipient.delivered_at is not None
    events = (await seeded_db.execute(
        CampaignEvent.__table__.select().where(
            CampaignEvent.recipient_id == recipient.id,
            CampaignEvent.event_type == EventType.MESSAGE_DELIVERED,
        )
    )).fetchall()
    assert len(events) == 1


async def test_read_event_updates_recipient(client, app, seeded_db):
    _app_settings(app)
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-r1")
    resp = await _post_webhook(client, _delivery_payload("wamid-r1", "read"))
    assert resp.status_code == 200
    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.READ


async def test_sent_event_fills_pending_recipient(client, app, seeded_db):
    _app_settings(app)
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-s1")
    recipient.status = RecipientStatus.SENDING
    recipient.sent_at = None
    await seeded_db.commit()
    resp = await _post_webhook(client, _delivery_payload("wamid-s1", "sent"))
    assert resp.status_code == 200
    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.SENT
    assert recipient.sent_at is not None


async def test_failed_event_terminal(client, app, seeded_db):
    _app_settings(app)
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-f1")
    recipient.status = RecipientStatus.SENT
    await seeded_db.commit()
    resp = await _post_webhook(client, _delivery_payload("wamid-f1", "failed"))
    assert resp.status_code == 200
    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.FAILED


async def test_duplicate_webhook_is_noop(client, app, seeded_db):
    """§20: the same event redelivered never double-counts."""
    _app_settings(app)
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-dup")
    first = await _post_webhook(client, _delivery_payload("wamid-dup", "delivered"))
    assert first.json()["data"]["applied"] == 1
    second = await _post_webhook(client, _delivery_payload("wamid-dup", "delivered"))
    assert second.json()["data"]["duplicates"] == 1
    assert second.json()["data"]["applied"] == 0
    rows = (await seeded_db.execute(
        CampaignEvent.__table__.select().where(
            CampaignEvent.recipient_id == recipient.id,
            CampaignEvent.event_type == EventType.MESSAGE_DELIVERED,
        )
    )).fetchall()
    assert len(rows) == 1
    provider_rows = (await seeded_db.execute(
        ProviderEvent.__table__.select().where(
            ProviderEvent.provider_message_id == "wamid-dup")
    )).fetchall()
    assert len(provider_rows) == 1


async def test_delivered_then_read_both_count(client, app, seeded_db):
    """Different statuses of one message are DIFFERENT events (composite ids)."""
    _app_settings(app)
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-dr")
    await _post_webhook(client, _delivery_payload("wamid-dr", "delivered"))
    await _post_webhook(client, _delivery_payload("wamid-dr", "read"))
    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.READ
    provider_rows = (await seeded_db.execute(
        ProviderEvent.__table__.select().where(
            ProviderEvent.provider_message_id == "wamid-dr")
    )).fetchall()
    assert len(provider_rows) == 2  # delivered + read, never collapsed


async def test_backward_transition_rejected(client, app, seeded_db):
    """§21: READ → QUEUED must never happen."""
    assert can_transition("READ", "QUEUED") is False
    assert can_transition("READ", "SENDING") is False
    assert can_transition("DELIVERED", "SENT") is False
    assert can_transition("FAILED", "SENT") is False
    assert can_transition("SENT", "DELIVERED") is True
    assert can_transition("SENT", "FAILED") is True
    assert can_transition("PENDING", "SENT") is True  # out-of-order absorb
    # state machine over a row
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-b1")
    from app.services.marketing.state import apply_event

    recipient.status = RecipientStatus.READ
    await seeded_db.commit()
    changed = apply_event(recipient, EventType.MESSAGE_SENT, timestamp=datetime.now(timezone.utc))
    assert changed is False
    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.READ


async def test_unknown_message_id_stored_but_unmatched(client, app, seeded_db):
    _app_settings(app)
    await _seed_campaign_with_recipient(seeded_db, wamid="wamid-known")
    resp = await _post_webhook(client, _delivery_payload("wamid-unknown", "delivered"))
    assert resp.status_code == 200
    assert resp.json()["data"]["unmatched"] == 1


async def test_stale_event_stored_but_not_applied(client, app, seeded_db):
    """§18 replay/stale protection: old events are stored, never applied."""
    _app_settings(app)
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-old")
    recipient.status = RecipientStatus.SENT
    await seeded_db.commit()
    old_ts = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
    payload = _delivery_payload("wamid-old-2", "delivered")
    payload["entry"][0]["changes"][0]["value"]["statuses"][0]["timestamp"] = old_ts
    resp = await _post_webhook(client, payload)
    assert resp.status_code == 200
    assert resp.json()["data"]["stale"] == 1
    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.SENT  # unchanged


# --------------------------------------------------- §22–§24 inbound messages
async def test_inbound_creates_conversation_and_message(client, app, seeded_db):
    _app_settings(app)
    account, _c, _r = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-c1")
    resp = await _post_webhook(client, _inbound_payload("wamid-in1", "919876543210", "Hello, tell me more"))
    assert resp.status_code == 200
    assert resp.json()["data"]["inbound"] == 1
    conv = (await seeded_db.execute(Conversation.__table__.select())).first()
    assert conv is not None and conv.sending_account_id == account.id
    # lead matched via normalized phone
    assert conv.lead_id is not None
    assert conv.status == "OPEN"
    msg = (await seeded_db.execute(Message.__table__.select())).first()
    assert msg is not None
    assert msg.direction == "INBOUND" and msg.body == "Hello, tell me more"
    assert msg.provider_message_id == "wamid-in1"


async def test_inbound_no_lead_stays_unresolved(client, app, seeded_db):
    """§24: unknown senders stay unresolved — no invented leads, no duplicates."""
    _app_settings(app)
    await _seed_campaign_with_recipient(seeded_db, wamid="wamid-c2")
    resp = await _post_webhook(client, _inbound_payload("wamid-in2", "491519998877", "Who are you?"))
    assert resp.status_code == 200
    conv = (await seeded_db.execute(Conversation.__table__.select())).first()
    assert conv is not None and conv.lead_id is None
    assert conv.status == "PENDING"


async def test_inbound_duplicate_message_no_double_row(client, app, seeded_db):
    _app_settings(app)
    await _seed_campaign_with_recipient(seeded_db, wamid="wamid-c3")
    await _post_webhook(client, _inbound_payload("wamid-in3", "919876543210", "Hi"))
    second = await _post_webhook(client, _inbound_payload("wamid-in3", "919876543210", "Hi"))
    assert second.json()["data"]["duplicates"] == 1
    msgs = (await seeded_db.execute(Message.__table__.select())).fetchall()
    assert len(msgs) == 1


async def test_inbound_links_campaign_reply(client, app, seeded_db):
    """A reply from a SENT recipient records MESSAGE_REPLIED on the campaign."""
    _app_settings(app)
    account, campaign, recipient = await _seed_campaign_with_recipient(seeded_db, wamid="wamid-c4")
    recipient.status = RecipientStatus.DELIVERED
    await seeded_db.commit()
    resp = await _post_webhook(client, _inbound_payload("wamid-in4", "919876543210", "Interested!"))
    assert resp.status_code == 200
    await seeded_db.refresh(recipient)
    assert recipient.status == RecipientStatus.REPLIED
    assert recipient.replied_at is not None
    events = (await seeded_db.execute(
        CampaignEvent.__table__.select().where(
            CampaignEvent.campaign_id == campaign.id,
            CampaignEvent.event_type == EventType.MESSAGE_REPLIED,
        )
    )).fetchall()
    assert len(events) == 1


async def test_inbound_attachment_type_stored_without_body(client, app, seeded_db):
    _app_settings(app)
    await _seed_campaign_with_recipient(seeded_db, wamid="wamid-c5")
    payload = _inbound_payload("wamid-in5", "919876543210", "")
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": "919876543210", "id": "wamid-in5",
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "type": "image", "image": {"id": "img1", "mime_type": "image/jpeg"},
    }
    resp = await _post_webhook(client, payload)
    assert resp.status_code == 200
    msg = (await seeded_db.execute(Message.__table__.select())).first()
    assert msg.message_type == "IMAGE"


# ------------------------------------------------------------- §44 logging
async def test_webhook_logs_never_contain_signature(app, client, seeded_db, caplog):
    import logging

    _app_settings(app)
    with caplog.at_level(logging.DEBUG, logger="qbit.marketing.webhooks_api"):
        resp = await _post_webhook(client, _delivery_payload("wamid-log", "delivered"))
        assert resp.status_code == 200
        sig = _sign(json.dumps(_delivery_payload("wamid-log", "delivered")).encode())
        for record in caplog.records:
            assert sig not in record.getMessage()
            assert "sha256=" not in record.getMessage()
