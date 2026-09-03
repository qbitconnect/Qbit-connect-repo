"""Phase 7 reply-tracking foundation tests (§32, §33).

The normalized inbound-email interface: threading via Message-ID /
In-Reply-To (never subject matching), conversation matching by (account,
normalized email), campaign REPLIED linkage. No fake inbox is created."""

from __future__ import annotations

import uuid

import pytest

from app.models.messaging import Conversation, Message
from app.services.marketing.email_tracking import EmailInboundService

from tests.marketing.conftest import seed_account, seed_leads, seed_template
from tests.marketing.test_campaigns import _make_launched_campaign


@pytest.mark.asyncio
class TestReplyTrackingFoundation:
    async def test_inbound_email_creates_conversation_and_message(self, seeded_db):
        account = await seed_account(seeded_db, provider="email_mock",
                                     channel="EMAIL")
        service = EmailInboundService()
        conversation, message = await service.record_inbound_email(
            seeded_db, account=account,
            from_email="  Ravi.Patel@ACME.com ", to_email="sales@company.test",
            subject="Re: partnership", body_text="Interested — tell me more.",
            provider_message_id="inbound-1",
        )
        assert conversation is not None and message is not None
        assert conversation.contact_email == "Ravi.Patel@acme.com"  # domain lowercased
        assert conversation.channel == "EMAIL"
        assert message.direction == "INBOUND"
        assert message.message_metadata["in_reply_to"] is None

    async def test_threading_links_reply_to_campaign(self, seeded_db):
        campaign, leads, account, template = await _make_launched_campaign(
            seeded_db, channel="EMAIL", lead_count=1, provider="email_mock",
        )
        from app.models.marketing import CampaignRecipient, RecipientStatus

        recipient = CampaignRecipient(
            campaign_id=campaign.id, lead_id=leads[0].id,
            recipient_address=leads[0].email,
            provider_message_id="<outbound-abc@company.test>",
            status=RecipientStatus.SENT,
        )
        seeded_db.add(recipient)
        await seeded_db.commit()

        service = EmailInboundService()
        conversation, message = await service.record_inbound_email(
            seeded_db, account=account,
            from_email=leads[0].email, to_email="sales@company.test",
            subject="Re: anything", body_text="Yes please",
            provider_message_id="inbound-2",
            in_reply_to="<outbound-abc@company.test>",
        )
        linked = await service.link_reply_to_campaign(
            seeded_db, from_email=leads[0].email,
            in_reply_to="<outbound-abc@company.test>",
        )
        assert linked is True
        await seeded_db.refresh(recipient)
        assert recipient.status == "REPLIED"
        assert recipient.replied_at is not None

    async def test_reply_without_known_recipient_is_not_fabricated(self, seeded_db):
        service = EmailInboundService()
        linked = await service.link_reply_to_campaign(
            seeded_db, from_email="stranger@unknown.test",
            in_reply_to="<nope@nowhere.test>",
        )
        assert linked is False

    async def test_subject_matching_alone_never_links(self, seeded_db):
        """§33: threading relies on message ids, not subjects."""
        campaign, leads, *_ = await _make_launched_campaign(
            seeded_db, channel="EMAIL", lead_count=1, provider="email_mock",
        )
        service = EmailInboundService()
        linked = await service.link_reply_to_campaign(
            seeded_db, from_email=leads[0].email,
            in_reply_to=None,  # no threading header — no fabricated linkage
        )
        assert linked is False

    async def test_invalid_sender_email_rejected(self, seeded_db):
        account = await seed_account(seeded_db, provider="email_mock",
                                     channel="EMAIL")
        service = EmailInboundService()
        conversation, message = await service.record_inbound_email(
            seeded_db, account=account, from_email="not-an-email",
            to_email=None, subject="x", body_text="y",
        )
        assert conversation is None and message is None
