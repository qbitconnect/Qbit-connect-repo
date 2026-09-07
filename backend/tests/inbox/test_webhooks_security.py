"""Phase 8 webhook → inbox pipeline + security tests (§38–§44, §65).

- WhatsApp delivery events mirror onto Message rows (forward-only)
- email inbound webhook: signature + replay + idempotency + threading
- email HTML never executes (nh3 sanitization before display)
- no secrets ever appear in API responses
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.messaging import Conversation, Message, ProviderEvent
from app.services.marketing.email_compose import sanitize_html
from tests.marketing.conftest import seed_account

pytestmark = pytest.mark.asyncio

SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = SECRET, timestamp: int | None = None) -> dict:
    ts = timestamp if timestamp is not None else int(time.time())
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-QBIT-Signature": f"sha256={signature}",
        "X-QBIT-Timestamp": str(ts),
        "Content-Type": "application/json",
    }


@pytest.fixture
def webhook_env(app, monkeypatch):
    app.state.settings.EMAIL_WEBHOOK_SECRET = SECRET
    return app


# ------------------------------------------- WhatsApp delivery → Message row
async def test_whatsapp_delivery_updates_inbox_message(seeded_db, app):
    account = await seed_account(seeded_db, channel="WHATSAPP")
    from app.models.messaging import Conversation

    conversation = Conversation(
        channel="WHATSAPP", sending_account_id=account.id,
        contact_phone="+15554440000", status="OPEN",
    )
    seeded_db.add(conversation)
    await seeded_db.flush()
    message = Message(
        conversation_id=conversation.id, direction="OUTBOUND", status="SENDING",
        message_type="TEXT", body="Out", provider_message_id="wamid-hook-1",
    )
    seeded_db.add(message)
    await seeded_db.commit()

    payload = {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": account.phone_number_id},
                    "statuses": [{"id": "wamid-hook-1", "status": "delivered",
                                  "timestamp": str(int(datetime.now(timezone.utc).timestamp()))}],
                },
            }],
        }],
    }
    app.state.settings.WHATSAPP_APP_SECRET = "wa-secret"
    body = json.dumps(payload).encode()
    signature = hmac.new(b"wa-secret", body, hashlib.sha256).hexdigest()
    resp = await app.state.client_post if False else None  # placeholder
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/api/v1/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": f"sha256={signature}",
                     "Content-Type": "application/json"},
        )
    assert resp.status_code == 200, resp.text
    await seeded_db.refresh(message)
    assert message.status == "DELIVERED"
    assert message.delivered_at is not None


# ------------------------------------------------- email inbound webhook
async def _post_inbound(app, payload: dict, *, secret: str = SECRET, stale=False):
    from httpx import ASGITransport, AsyncClient

    body = json.dumps(payload).encode()
    ts = int(time.time()) - (10_000 if stale else 0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(
            "/api/v1/webhooks/email/inbound/email_api",
            content=body, headers=_sign(body, secret, ts),
        )


async def test_email_inbound_webhook_full_pipeline(webhook_env, seeded_db):
    app = webhook_env
    account = await seed_account(seeded_db, channel="EMAIL", provider="email_api")
    payload = {
        "message_id": "<in-42@customer.test>",
        "from": "Customer <ravi@acme.test>",
        "to": account.identifier,
        "subject": "Re: Quote",
        "text": "Looks good, proceed.",
        "in_reply_to": "<quote-1@qbit.test>",
        "references": "<quote-1@qbit.test>",
    }
    resp = await _post_inbound(app, payload)
    assert resp.status_code == 200, resp.text
    summary = resp.json()["data"]
    assert summary["stored"] == 1

    conversation = (await seeded_db.execute(
        select(Conversation).where(Conversation.channel == "EMAIL")
    )).scalars().first()
    assert conversation is not None
    assert conversation.contact_email == "ravi@acme.test"
    assert conversation.unread_count == 1
    message = (await seeded_db.execute(
        select(Message).where(Message.conversation_id == conversation.id)
    )).scalars().first()
    assert message.message_metadata["in_reply_to"] == "<quote-1@qbit.test>"
    # ProviderEvent stored idempotently
    events = (await seeded_db.execute(
        select(ProviderEvent).where(ProviderEvent.provider == "email_inbound:email_api")
    )).scalars().all()
    assert len(events) == 1


async def test_email_inbound_webhook_idempotent(webhook_env, seeded_db):
    app = webhook_env
    account = await seed_account(seeded_db, channel="EMAIL", provider="email_api")
    payload = {
        "message_id": "<dup-1@customer.test>", "from": "ravi@acme.test",
        "to": account.identifier, "subject": "Hi", "text": "Hello",
    }
    resp1 = await _post_inbound(app, payload)
    resp2 = await _post_inbound(app, payload)
    assert resp1.json()["data"]["stored"] == 1
    assert resp2.json()["data"]["duplicates"] == 1
    messages = (await seeded_db.execute(select(Message))).scalars().all()
    assert len(messages) == 1


async def test_email_inbound_webhook_rejects_bad_signature(webhook_env):
    payload = {"message_id": "<x@y.test>", "from": "a@b.test", "text": "hi"}
    from httpx import ASGITransport, AsyncClient

    body = json.dumps(payload).encode()
    async with AsyncClient(transport=ASGITransport(app=webhook_env), base_url="http://t") as client:
        resp = await client.post(
            "/api/v1/webhooks/email/inbound/email_api",
            content=body, headers=_sign(body, "wrong-secret"),
        )
    assert resp.status_code == 401


async def test_email_inbound_webhook_rejects_stale_timestamp(webhook_env):
    payload = {"message_id": "<x2@y.test>", "from": "a@b.test", "text": "hi"}
    resp = await _post_inbound(webhook_env, payload, stale=True)
    assert resp.status_code == 401


# ------------------------------------------------------------- XSS (§44)
def test_sanitize_html_strips_scripts():
    raw = (
        '<p onclick="steal()">Hello <script>alert(1)</script></p>'
        '<a href="javascript:evil()">link</a>'
        '<img src=x onerror="alert(2)">'
    )
    cleaned = sanitize_html(raw)
    assert "<script" not in cleaned
    assert "onclick" not in cleaned
    assert "onerror" not in cleaned
    assert "javascript:" not in cleaned
    assert "Hello" in cleaned  # legitimate content survives


async def test_message_html_endpoint_sanitizes(webhook_env, seeded_db, client):
    """UI cookie session required — log in through the UI form first."""
    account = await seed_account(seeded_db, channel="EMAIL", provider="email_api")
    from app.models.messaging import Conversation

    conversation = Conversation(
        channel="EMAIL", sending_account_id=account.id,
        contact_email="ravi@acme.test", status="OPEN",
    )
    seeded_db.add(conversation)
    await seeded_db.flush()
    message = Message(
        conversation_id=conversation.id, direction="INBOUND", status="RECEIVED",
        message_type="EMAIL", body="plain fallback",
        message_metadata={"html": '<b>Hi</b><script>alert(document.cookie)</script>'
                                  '<img src=x onerror="pwn()">'},
    )
    seeded_db.add(message)
    await seeded_db.commit()

    # UI cookie login (UI routes are cookie-authenticated, not Bearer)
    resp = await client.post("/login", data={
        "email": "admin@qbit.example.com", "password": "Sup3rSecret!Pass",
        "next": "/inbox",
    })
    assert resp.status_code in (200, 303)

    resp = await client.get(f"/ui/inbox/messages/{message.id}/html")
    assert resp.status_code == 200
    text = resp.text
    assert "<script" not in text
    assert "onerror" not in text
    assert "<b>Hi</b>" in text


# --------------------------------------------------- secret exposure (§52)
async def test_conversation_api_never_exposes_credentials(app, client, admin_headers, seeded_db):
    account = await seed_account(seeded_db, channel="WHATSAPP", provider="mock")
    account.config_metadata = {"access_token_hint": "SHOULD-NOT-LEAK"}
    await seeded_db.commit()
    from app.models.messaging import Conversation

    conversation = Conversation(
        channel="WHATSAPP", sending_account_id=account.id,
        contact_phone="+15553330000", status="OPEN",
    )
    seeded_db.add(conversation)
    await seeded_db.commit()

    resp = await client.get(
        f"/api/v1/inbox/conversations/{conversation.id}", headers=admin_headers,
    )
    assert resp.status_code == 200
    raw = resp.text
    assert "SHOULD-NOT-LEAK" not in raw
    data = resp.json()["data"]
    if data.get("account"):
        assert "access_token" not in data["account"]
        assert "config_metadata" not in data["account"]
