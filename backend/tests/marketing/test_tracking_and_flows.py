"""Open/click tracking + unsubscribe e2e + security tests (Phase 7 §29–§33, §54)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.models.marketing import CampaignRecipient, EmailTrackingEvent, Suppression
from app.services.marketing import tracking as T
from tests.marketing.helpers import (
    make_campaign,
    make_mock_account,
    make_recipient,
    make_suppression,
    make_template,
)

SECRET = "test-secret-key-" + "a" * 48


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


class TestTokenSecurity:
    def test_open_token_roundtrip(self):
        import uuid as _uuid

        token = T.make_open_token(_uuid.uuid4(), _uuid.uuid4(), SECRET)
        data = T.verify_open_token(token, SECRET)
        assert "c" in data and "r" in data

    def test_forged_token_rejected(self):
        import uuid as _uuid

        token = T.make_open_token(_uuid.uuid4(), _uuid.uuid4(), SECRET)[:-4] + "zzzz"
        with pytest.raises(T.ValidationError):
            T.verify_open_token(token, SECRET)

    def test_safe_destination(self):
        assert T.is_safe_destination("https://example.com/page")
        assert T.is_safe_destination("http://example.com")
        assert not T.is_safe_destination("javascript:alert(1)")
        assert not T.is_safe_destination("data:text/html,x")
        assert not T.is_safe_destination("file:///etc/passwd")
        assert not T.is_safe_destination("//evil.com")
        assert not T.is_safe_destination("")

    def test_click_token_carries_url(self):
        import uuid as _uuid

        token = T.make_click_token(_uuid.uuid4(), _uuid.uuid4(), "https://ok.example.com", SECRET)
        assert T.verify_click_token(token, SECRET)["u"] == "https://ok.example.com"


class TestOpenPixel:
    async def test_open_recorded_when_enabled(self, client, session):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template, track_opens=1)
        recipient = await make_recipient(session, campaign=campaign, address="open@example.com")
        recipient.status = "DELIVERED"
        await session.commit()

        token = T.make_open_token(campaign.id, recipient.id, "test-secret-key-" + "a" * 48)
        resp = await client.get(f"/t/open/{token}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")

        await session.refresh(recipient)
        assert recipient.opened_at is not None
        events = (await session.scalars(select(EmailTrackingEvent))).all()
        assert len(events) == 1 and events[0].event_type == "OPEN"

    async def test_open_not_recorded_when_disabled(self, client, session):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template, track_opens=0)
        recipient = await make_recipient(session, campaign=campaign, address="noopen@example.com")
        recipient.status = "DELIVERED"
        await session.commit()

        token = T.make_open_token(campaign.id, recipient.id, "test-secret-key-" + "a" * 48)
        await client.get(f"/t/open/{token}")
        await session.refresh(recipient)
        assert recipient.opened_at is None  # tracking disabled → nothing recorded


class TestClickRedirect:
    async def test_click_redirects_and_records(self, client, session):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template, track_clicks=1)
        recipient = await make_recipient(session, campaign=campaign, address="click@example.com")
        recipient.status = "DELIVERED"
        await session.commit()

        token = T.make_click_token(
            campaign.id, recipient.id, "https://destination.example.com/offer", "test-secret-key-" + "a" * 48
        )
        resp = await client.get(f"/t/click/{token}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://destination.example.com/offer"
        await session.refresh(recipient)
        assert recipient.clicked_at is not None

    async def test_click_unsafe_destination_blocked(self, client, session):
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template, track_clicks=1)
        recipient = await make_recipient(session, campaign=campaign, address="unsafe@example.com")
        await session.commit()
        token = T.make_click_token(
            campaign.id, recipient.id, "javascript:alert(document.cookie)", "test-secret-key-" + "a" * 48
        )
        resp = await client.get(f"/t/click/{token}", follow_redirects=False)
        assert resp.status_code in (401, 404)

    async def test_rewrite_links_wraps_and_blocks(self):
        from app.services.marketing import templates as template_engine

        raw = '<p><a href="https://good.com">ok</a><a href="javascript:bad()">bad</a></p>'
        import uuid as _uuid

        # pipeline order: sanitize FIRST (removes javascript: links), then wrap
        html = template_engine.sanitize_html(raw)
        out = T.rewrite_links(html, _uuid.uuid4(), _uuid.uuid4(), secret=SECRET)
        assert "javascript:" not in out
        assert "/t/click/" in out
        assert "https://good.com" not in out  # wrapped


class TestUnsubscribeFlow:
    async def test_full_unsubscribe_e2e(self, client, session, admin_headers):
        # 1. deliver a send (mock pipeline pieces)
        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template)
        recipient = await make_recipient(session, campaign=campaign, address="bye@example.com")
        recipient.status = "SENT"
        await session.commit()

        # 2. issue a real token through the service the delivery pipeline uses
        from app.services.marketing.suppression import UnsubscribeService

        service = UnsubscribeService(ttl_days=30, base_url="http://testserver")
        raw = await service.issue_token(
            session, channel="EMAIL", address="bye@example.com", address_norm="bye@example.com",
            lead_id=None, campaign_id=campaign.id, recipient_id=recipient.id,
        )
        await session.commit()

        # 3. GET confirmation page (no login required)
        page = await client.get(f"/unsubscribe/{raw}")
        assert page.status_code == 200
        assert "Confirm unsubscribe" in page.text

        # 4. POST confirm → suppression created
        confirmed = await client.post(f"/unsubscribe/{raw}")
        assert confirmed.status_code == 200
        assert "unsubscribed" in confirmed.text.lower()

        suppression = (
            await session.scalars(
                select(Suppression).where(
                    Suppression.address_norm == "bye@example.com",
                    Suppression.reason == "UNSUBSCRIBED",
                )
            )
        ).first()
        assert suppression is not None

        # 5. re-use of the token fails
        again = await client.post(f"/unsubscribe/{raw}")
        assert "already been used" in again.text or "not valid" in again.text

    async def test_guessed_token_page_renders_invalid(self, client):
        page = await client.get("/unsubscribe/guess-" + "0" * 60)
        assert page.status_code == 200
        assert "not valid" in page.text

    async def test_unsubscribed_lead_never_queued_again(self, client, session):
        """The e2e guarantee of §14: suppression blocks future eligibility."""
        from app.services.marketing.campaigns import CampaignService

        account = await make_mock_account(session)
        template = await make_template(session)
        await make_suppression(session, email="blocked-forever@example.com", reason="UNSUBSCRIBED")
        campaign = await make_campaign(
            session, account=account, template=template,
            audience={"require_opt_in": False,
                      "filter": {"and": [{"field": "email", "op": "eq", "value": "blocked-forever@example.com"}]}},
        )
        from tests.marketing.helpers import make_lead

        await make_lead(session, email="blocked-forever@example.com")
        service = CampaignService()
        await service.snapshot_audience(session, campaign)
        await service.run_eligibility(session, campaign)
        recipient = (await session.scalars(select(CampaignRecipient))).first()
        assert recipient.status == "SKIPPED" and recipient.reason == "UNSUBSCRIBED"


class TestThreadingFoundation:
    async def test_inbound_email_creates_conversation_and_matches_lead(self, session):
        from app.services.marketing.conversations import ConversationService
        from tests.marketing.helpers import make_lead

        lead = await make_lead(session, email="thread@example.com")
        account = await make_mock_account(session)
        service = ConversationService()
        conversation, message = await service.ingest_inbound_email(
            session,
            sending_account_id=account.id,
            from_email="Thread@Example.com",
            from_name="Thread Tester",
            subject="Re: outreach",
            text="I am interested",
            html=None,
            headers={"Message-ID": "<inbound-1@x>", "In-Reply-To": "<outbound-1@x>"},
        )
        await session.commit()
        assert conversation.lead_id == lead.id  # matched via normalized address
        assert message.headers["In-Reply-To"] == "<outbound-1@x>"
        assert message.direction == "INBOUND"

        # second email joins the SAME conversation (dedup by address)
        _, message2 = await service.ingest_inbound_email(
            session, sending_account_id=account.id, from_email="thread@example.com",
            from_name=None, subject="Re: re", text="again", html=None, headers={},
        )
        assert message2.conversation_id == conversation.id

    async def test_unknown_sender_no_fabricated_lead(self, session):
        from app.services.marketing.conversations import ConversationService

        service = ConversationService()
        conversation, _ = await service.ingest_inbound_email(
            session, sending_account_id=None, from_email="stranger@nowhere.test",
            from_name="Stranger", subject="hi", text="?", html=None, headers={},
        )
        assert conversation.lead_id is None  # unresolved contact — never fabricated


class TestAnalyticsIntegrity:
    async def test_summary_from_real_events_only(self, session):
        from app.services.marketing.analytics import AnalyticsService
        from tests.marketing.helpers import make_recipient

        account = await make_mock_account(session)
        template = await make_template(session)
        campaign = await make_campaign(session, account=account, template=template)
        r1 = await make_recipient(session, campaign=campaign, address="a@example.com")
        r1.status = "DELIVERED"
        r2 = await make_recipient(session, campaign=campaign, address="b@example.com")
        r2.status = "BOUNCED"
        r3 = await make_recipient(session, campaign=campaign, address="c@example.com")
        r3.status = "SENT"
        await session.flush()

        summary = await AnalyticsService().campaign_summary(session, campaign)
        assert summary["verified"]["recipients_total"] == 3
        assert summary["verified"]["sent_total"] == 3
        assert summary["verified"]["delivered_total"] == 1
        assert summary["verified"]["bounced_total"] == 1
        assert summary["rates"]["bounce_rate"] == 33.33
