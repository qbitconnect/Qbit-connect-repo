"""Shared helpers for marketing tests."""

from __future__ import annotations

import uuid

import pytest_asyncio

from app.models.marketing import (
    Campaign,
    CampaignRecipient,
    MarketingConsent,
    MarketingTemplate,
    SendingAccount,
    Suppression,
)
from app.models.scrape import Lead

TEST_SECRET = "test-secret-key-" + "a" * 48


async def make_lead(session, *, email: str | None = None, phone: str | None = None, **fields) -> Lead:
    lead = Lead(
        business_name=fields.pop("business_name", "Acme Co"),
        first_name=fields.pop("first_name", "Sagar"),
        last_name=fields.pop("last_name", "Patel"),
        email=email,
        phone=phone,
        email_norm=(email or "").strip().lower() or None,
        city=fields.pop("city", None),
        state=fields.pop("state", None),
    )
    session.add(lead)
    await session.flush()
    return lead


async def make_consent(session, *, email: str, status: str = "OPTED_IN", lead_id=None) -> MarketingConsent:
    row = MarketingConsent(
        channel="EMAIL",
        address_norm=email.strip().lower(),
        opt_in_status=status,
        source="test",
        lead_id=lead_id,
    )
    session.add(row)
    await session.flush()
    return row


async def make_suppression(session, *, email: str, reason: str = "MANUAL") -> Suppression:
    row = Suppression(
        channel="EMAIL",
        address=email,
        address_norm=email.strip().lower(),
        reason=reason,
        source="test",
    )
    session.add(row)
    await session.flush()
    return row


async def make_mock_account(session, *, name: str = "Mock Mailer") -> SendingAccount:
    from app.services.marketing.accounts import SendingAccountService
    from app.services.marketing.secrets import SecretVault

    service = SendingAccountService(SecretVault(TEST_SECRET))
    return await service.create(
        session,
        channel="EMAIL",
        provider="mock_email",
        name=name,
        config={"mock_ready": True},
        credentials={"mode": "success"},
        sender_name="QBIT",
        sender_email="sales@qbit.example.com",
        reply_to="replies@qbit.example.com",
    )


async def make_template(session, *, channel: str = "EMAIL", **overrides) -> MarketingTemplate:
    row = MarketingTemplate(
        name=overrides.pop("name", "Outreach"),
        channel=channel,
        status=overrides.pop("status", "ACTIVE"),
        subject=overrides.pop("subject", "Hello {{first_name}}"),
        html_body=overrides.pop("html_body", "<p>Hi {{first_name}} at {{business_name}}</p>"),
        text_body=overrides.pop("text_body", "Hi {{first_name}} at {{business_name}}"),
        body=overrides.pop("body", None),
        variables=overrides.pop("variables", []),
    )
    session.add(row)
    await session.flush()
    return row


async def make_campaign(session, *, account: SendingAccount, template: MarketingTemplate, **overrides) -> Campaign:
    row = Campaign(
        name=overrides.pop("name", "Launch"),
        channel=overrides.pop("channel", "EMAIL"),
        template_id=template.id,
        sending_account_id=account.id,
        audience=overrides.pop("audience", {"require_opt_in": False}),
        track_opens=overrides.pop("track_opens", 0),
        track_clicks=overrides.pop("track_clicks", 0),
    )
    session.add(row)
    await session.flush()
    return row


async def make_recipient(session, *, campaign: Campaign, address: str, lead_id=None) -> CampaignRecipient:
    row = CampaignRecipient(
        campaign_id=campaign.id,
        lead_id=lead_id,
        address=address,
        address_norm=address.strip().lower(),
        status="ELIGIBLE",
        idempotency_key=f"{campaign.id}:{lead_id or address}:v1",
    )
    session.add(row)
    await session.flush()
    return row


def webhook_signature(body: bytes, secret: str) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
