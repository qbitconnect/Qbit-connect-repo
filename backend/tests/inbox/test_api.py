"""Phase 8 Inbox REST API tests (§45, §46, §49–§52) — RBAC, visibility,
filters, search, pagination, read-state, workflow actions, bulk actions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.messaging import Conversation, Message
from app.services.inbox.engine import ConversationEngine
from app.services.inbox.normalizer import normalize_whatsapp_inbound
from tests.marketing.conftest import seed_account, seed_leads

pytestmark = pytest.mark.asyncio


async def _seed_two_conversations(session):
    engine = ConversationEngine()
    accounts = {}
    conversations = []
    for channel in ("WHATSAPP", "EMAIL"):
        account = await seed_account(session, channel=channel, provider="mock")
        accounts[channel] = account
        if channel == "WHATSAPP":
            msg = normalize_whatsapp_inbound(
                sender_phone="+15551110000", provider_message_id=f"wa-{channel}-1",
                message_type="text", body="Hello from WhatsApp",
            )
        else:
            from app.services.inbox.normalizer import normalize_email_inbound

            msg = normalize_email_inbound(
                from_email="contact1@acme.test", to_email="sender@qbit.test",
                subject="Email thread", body_text="Hello from Email",
                provider_message_id="<api-1@acme.test>",
            )
        conversation, _m, _c = await engine.ingest_inbound(
            session, msg, account=account, settings=None,
        )
        conversations.append(conversation)
    return conversations, accounts


# ----------------------------------------------------------------- list/API
async def test_conversations_list_shape(app, client, admin_headers, seeded_db):
    await _seed_two_conversations(seeded_db)
    resp = await client.get("/api/v1/inbox/conversations", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 2
    item = data["items"][0]
    for key in ("id", "channel", "status", "unread_count", "last_message", "lead"):
        assert key in item


async def test_channel_and_status_filters(app, client, admin_headers, seeded_db):
    await _seed_two_conversations(seeded_db)
    resp = await client.get(
        "/api/v1/inbox/conversations?channel=WHATSAPP", headers=admin_headers,
    )
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["channel"] == "WHATSAPP"

    resp = await client.get(
        "/api/v1/inbox/conversations?status=PENDING", headers=admin_headers,
    )
    assert resp.json()["data"]["total"] == 2  # unresolved contacts are PENDING

    resp = await client.get(
        "/api/v1/inbox/conversations?channel=SMS", headers=admin_headers,
    )
    assert resp.status_code == 422  # invalid channel vocabulary rejected


async def test_unread_filter_and_read_state(app, client, admin_headers, seeded_db):
    conversations, _ = await _seed_two_conversations(seeded_db)
    conversation = conversations[0]
    resp = await client.get(
        "/api/v1/inbox/conversations?unread=true", headers=admin_headers,
    )
    assert resp.json()["data"]["total"] == 2

    resp = await client.post(
        f"/api/v1/inbox/conversations/{conversation.id}/read", headers=admin_headers,
    )
    assert resp.status_code == 200
    await seeded_db.refresh(conversation)
    assert conversation.unread_count == 0

    resp = await client.post(
        f"/api/v1/inbox/conversations/{conversation.id}/unread", headers=admin_headers,
    )
    await seeded_db.refresh(conversation)
    assert conversation.unread_count == 1

    resp = await client.get(
        "/api/v1/inbox/conversations?unread=true", headers=admin_headers,
    )
    assert resp.json()["data"]["total"] == 2  # the marked-unread one came back


async def test_unread_counters_endpoint(app, client, admin_headers, seeded_db):
    await _seed_two_conversations(seeded_db)
    resp = await client.get("/api/v1/inbox/unread-count", headers=admin_headers)
    counters = resp.json()["data"]
    assert counters["total"] == 2
    assert counters["whatsapp"] == 1
    assert counters["email"] == 1


async def test_messages_cursor_pagination(app, client, admin_headers, seeded_db):
    engine = ConversationEngine()
    account = await seed_account(seeded_db, channel="WHATSAPP", provider="mock")
    conversation = Conversation(
        channel="WHATSAPP", sending_account_id=account.id,
        contact_phone="+15552000000", status="OPEN", match_status="UNMATCHED",
    )
    seeded_db.add(conversation)
    await seeded_db.flush()
    for i in range(75):
        seeded_db.add(Message(
            conversation_id=conversation.id, direction="INBOUND", status="RECEIVED",
            message_type="TEXT", body=f"msg {i}", provider_message_id=f"pm-{i}",
        ))
    await seeded_db.commit()

    resp = await client.get(
        f"/api/v1/inbox/conversations/{conversation.id}/messages?limit=50",
        headers=admin_headers,
    )
    data = resp.json()["data"]
    assert len(data["items"]) == 50
    assert data["next_before"] is not None
    assert data["items"][0]["body"] == "msg 25"  # newest 50, chronological

    resp = await client.get(
        f"/api/v1/inbox/conversations/{conversation.id}/messages?limit=50&before={data['next_before']}",
        headers=admin_headers,
    )
    data2 = resp.json()["data"]
    assert len(data2["items"]) == 25
    assert data2["next_before"] is None


async def test_search_server_side(app, client, admin_headers, seeded_db):
    await _seed_two_conversations(seeded_db)
    resp = await client.get(
        "/api/v1/inbox/search?q=pricing", headers=admin_headers,
    )
    assert resp.status_code == 200
    # no match for pricing — empty but well-formed
    assert resp.json()["data"]["total"] == 0

    resp = await client.get(
        "/api/v1/inbox/search?q=Hello from WhatsApp", headers=admin_headers,
    )
    data = resp.json()["data"]
    assert data["total"] >= 1
    assert data["items"][0]["message"]["body"].startswith("Hello from")


# ---------------------------------------------------------------- workflow
async def test_status_priority_assign_notes_activity(app, client, admin_headers, seeded_db):
    conversations, _ = await _seed_two_conversations(seeded_db)
    conversation = conversations[0]
    base = f"/api/v1/inbox/conversations/{conversation.id}"

    resp = await client.patch(f"{base}/status", headers=admin_headers, json={"status": "WAITING"})
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "WAITING"

    resp = await client.patch(f"{base}/priority", headers=admin_headers, json={"priority": "URGENT"})
    assert resp.json()["data"]["priority"] == "URGENT"

    resp = await client.post(f"{base}/notes", headers=admin_headers,
                             json={"content": "Customer is interested in the enterprise plan."})
    assert resp.status_code == 200
    note = resp.json()["data"]

    resp = await client.get(f"{base}/activity", headers=admin_headers)
    entries = resp.json()["data"]["items"]
    kinds = [e["kind"] for e in entries]
    types = [e["event"]["event_type"] for e in entries if e["kind"] == "event"]
    assert "note" in kinds
    assert "STATUS_CHANGED" in types
    assert "PRIORITY_CHANGED" in types
    assert "MESSAGE_RECEIVED" in types

    resp = await client.get(f"/api/v1/inbox/conversations/{conversation.id}", headers=admin_headers)
    detail = resp.json()["data"]
    assert detail["assignee"] is None

    # assign to the admin (self)
    me = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()["data"]
    resp = await client.post(f"{base}/assign", headers=admin_headers,
                             json={"assigned_user_id": me["id"]})
    assert resp.status_code == 200
    assert resp.json()["data"]["assigned_user_id"] == me["id"]
    # unassign
    resp = await client.post(f"{base}/assign", headers=admin_headers,
                             json={"assigned_user_id": None})
    assert resp.json()["data"]["assigned_user_id"] is None


async def test_status_vocabulary_enforced(app, client, admin_headers, seeded_db):
    conversations, _ = await _seed_two_conversations(seeded_db)
    resp = await client.patch(
        f"/api/v1/inbox/conversations/{conversations[0].id}/status",
        headers=admin_headers, json={"status": "MAYBE"},
    )
    assert resp.status_code == 422


async def test_link_create_lead_endpoints(app, client, admin_headers, seeded_db):
    conversations, _ = await _seed_two_conversations(seeded_db)
    conversation = conversations[0]
    assert conversation.lead_id is None

    leads = await seed_leads(seeded_db, 1, email="other@acme.test", phone="+15559990000")
    resp = await client.post(
        f"/api/v1/inbox/conversations/{conversation.id}/link-lead",
        headers=admin_headers, json={"lead_id": str(leads[0].id)},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["lead_id"] == str(leads[0].id)

    resp = await client.post(
        f"/api/v1/inbox/conversations/{conversation.id}/unlink-lead",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["lead_id"] is None

    resp = await client.post(
        f"/api/v1/inbox/conversations/{conversation.id}/create-lead",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["conversation"]["lead_id"] == data["lead_id"]


async def test_bulk_actions(app, client, admin_headers, seeded_db):
    conversations, _ = await _seed_two_conversations(seeded_db)
    ids = [str(c.id) for c in conversations]
    resp = await client.post("/api/v1/inbox/bulk", headers=admin_headers,
                             json={"conversation_ids": ids, "action": "read"})
    assert resp.json()["data"]["changed"] == 2
    resp = await client.post("/api/v1/inbox/bulk", headers=admin_headers,
                             json={"conversation_ids": ids, "action": "unread"})
    assert resp.json()["data"]["changed"] == 2
    resp = await client.post("/api/v1/inbox/bulk", headers=admin_headers,
                             json={"conversation_ids": ids, "action": "priority",
                                   "value": "HIGH"})
    assert resp.json()["data"]["changed"] == 2
    # no destructive bulk delete exists (§46)
    resp = await client.post("/api/v1/inbox/bulk", headers=admin_headers,
                             json={"conversation_ids": ids, "action": "delete"})
    assert resp.status_code == 422


# -------------------------------------------------------------------- RBAC
async def test_viewer_can_view_but_not_mutate(app, client, viewer_headers, seeded_db):
    conversations, _ = await _seed_two_conversations(seeded_db)
    conversation = conversations[0]
    resp = await client.get("/api/v1/inbox/conversations", headers=viewer_headers)
    assert resp.status_code == 200
    resp = await client.get(
        f"/api/v1/inbox/conversations/{conversation.id}", headers=viewer_headers,
    )
    assert resp.status_code == 200
    # mutations blocked server-side (§49)
    resp = await client.patch(f"/api/v1/inbox/conversations/{conversation.id}/status",
                              headers=viewer_headers, json={"status": "OPEN"})
    assert resp.status_code == 403
    resp = await client.post(f"/api/v1/inbox/conversations/{conversation.id}/assign",
                             headers=viewer_headers, json={"assigned_user_id": None})
    assert resp.status_code == 403
    resp = await client.post(f"/api/v1/inbox/conversations/{conversation.id}/notes",
                             headers=viewer_headers, json={"content": "x"})
    assert resp.status_code == 403


async def test_unauthenticated_rejected(app, client, seeded_db):
    resp = await client.get("/api/v1/inbox/conversations")
    assert resp.status_code == 401


# -------------------------------------------------------------- visibility
async def test_assigned_only_visibility_enforced_server_side(app, seeded_db, client, admin_headers, viewer_headers):
    """ASSIGNED_ONLY scope hides other agents' conversations (§50).

    The viewer role has inbox.view but NOT inbox.manage — so the scope
    applies. Assigned conversations stay visible; others 404 (never 403)."""
    conversations, _ = await _seed_two_conversations(seeded_db)
    me = (await client.get("/api/v1/auth/me", headers=viewer_headers)).json()["data"]
    admin = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()["data"]

    # viewer is assigned conversation[0]; conversation[1] is assigned to the
    # ADMIN — under ASSIGNED_ONLY the viewer must never see it
    engine = ConversationEngine()
    await engine.assign_user(seeded_db, conversations[0], uuid.UUID(me["id"]))
    await engine.assign_user(seeded_db, conversations[1], uuid.UUID(admin["id"]))

    app.state.settings.QBIT_INBOX_VISIBILITY = "ASSIGNED_ONLY"
    try:
        resp = await client.get("/api/v1/inbox/conversations", headers=viewer_headers)
        items = resp.json()["data"]["items"]
        assert [i["id"] for i in items] == [str(conversations[0].id)]
        # the admin's conversation 404s for a scoped user (never a 403 leak)
        resp = await client.get(
            f"/api/v1/inbox/conversations/{conversations[1].id}", headers=viewer_headers,
        )
        assert resp.status_code == 404
        # but stays visible to its assignee (admin has inbox.manage anyway)
        resp = await client.get(
            f"/api/v1/inbox/conversations/{conversations[1].id}", headers=admin_headers,
        )
        assert resp.status_code == 200
    finally:
        app.state.settings.QBIT_INBOX_VISIBILITY = "ALL"


async def test_audit_actions_recorded(app, client, admin_headers, seeded_db):
    from app.models.audit import AuditLog

    conversations, _ = await _seed_two_conversations(seeded_db)
    conversation = conversations[0]
    await client.patch(f"/api/v1/inbox/conversations/{conversation.id}/status",
                       headers=admin_headers, json={"status": "RESOLVED"})
    rows = (await seeded_db.execute(
        select(AuditLog).where(AuditLog.action == "inbox.status_changed")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].resource_id == str(conversation.id)
