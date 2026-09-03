"""Pre-queue eligibility chain (Phase 7 §15, §41).

EMAIL chain (evaluated per recipient before queueing):
    1. valid email                → MISSING_EMAIL / INVALID_EMAIL
    2. normalization              → INVALID_EMAIL
    3. opt-in status              → NO_OPT_IN      (only when the campaign
                                    requires opt-in; absence of consent data
                                    is UNKNOWN, never fabricated opt-in)
    4. unsubscribe / suppression  → UNSUBSCRIBED / SUPPRESSED
    5. campaign eligibility       → campaign-level filter
    6. template validity          → INVALID_TEMPLATE
    7. sender account health      → ACCOUNT_UNAVAILABLE
    8. provider capability        → PROVIDER_NOT_READY

A publicly scraped email address is NOT marketing consent (spec §15):
eligibility returns NO_OPT_IN whenever the campaign requires consent and no
OPTED_IN evidence exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import (
    Campaign,
    CampaignRecipient,
    HealthStatus,
    MarketingConsent,
    SendingAccount,
    Suppression,
)
from app.services.marketing.normalization import normalize_email
from app.services.marketing.suppression import SuppressionService

# reason codes (spec §15)
MISSING_EMAIL = "MISSING_EMAIL"
INVALID_EMAIL = "INVALID_EMAIL"
NO_OPT_IN = "NO_OPT_IN"
UNSUBSCRIBED = "UNSUBSCRIBED"
SUPPRESSED = "SUPPRESSED"
INVALID_TEMPLATE = "INVALID_TEMPLATE"
ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"
PROVIDER_NOT_READY = "PROVIDER_NOT_READY"
CAMPAIGN_CANCELLED = "CAMPAIGN_CANCELLED"


@dataclass(frozen=True)
class EligibilityOutcome:
    eligible: bool
    reason: str | None = None


class EmailEligibilityService:
    """Stateless evaluator operating on a single (campaign, lead, email)."""

    async def check(
        self,
        session: AsyncSession,
        *,
        campaign: Campaign,
        email: str | None,
        require_opt_in: bool | None = None,
    ) -> EligibilityOutcome:
        require = require_opt_in if require_opt_in is not None else bool(
            (campaign.audience or {}).get("require_opt_in", True)
        )
        result = normalize_email(email)
        if not result.valid:
            return EligibilityOutcome(False, result.reason)  # MISSING/INVALID_EMAIL

        suppression = SuppressionService()
        row = await suppression.is_suppressed(
            session, channel="EMAIL", address_norm=result.normalized or ""
        )
        if row is not None:
            if row.reason == "UNSUBSCRIBED":
                return EligibilityOutcome(False, UNSUBSCRIBED)
            return EligibilityOutcome(False, SUPPRESSED)

        if require:
            consent = await session.scalar(
                select(MarketingConsent).where(
                    MarketingConsent.channel == "EMAIL",
                    MarketingConsent.address_norm == result.normalized or "",
                )
            )
            if consent is None or consent.opt_in_status != "OPTED_IN":
                return EligibilityOutcome(False, NO_OPT_IN)

        return EligibilityOutcome(True)

    async def check_template(self, *, campaign: Campaign, template) -> EligibilityOutcome:
        if template is None:
            return EligibilityOutcome(False, INVALID_TEMPLATE)
        return EligibilityOutcome(True)

    async def check_account(self, *, campaign: Campaign, account: SendingAccount | None) -> EligibilityOutcome:
        if account is None:
            return EligibilityOutcome(False, ACCOUNT_UNAVAILABLE)
        if account.status != "ACTIVE":
            return EligibilityOutcome(False, ACCOUNT_UNAVAILABLE)
        if account.health_status in (HealthStatus.UNHEALTHY, HealthStatus.UNKNOWN):
            return EligibilityOutcome(False, ACCOUNT_UNAVAILABLE)
        return EligibilityOutcome(True)


async def suppressions_for(session: AsyncSession, channel: str) -> dict[str, str]:
    """Bulk map {address_norm: reason} for a channel (used by batch passes)."""
    rows = (
        await session.scalars(select(Suppression).where(Suppression.channel == channel.upper()))
    ).all()
    return {r.address_norm: r.reason for r in rows}


async def consent_map(session: AsyncSession, channel: str) -> dict[str, str]:
    rows = (
        await session.scalars(
            select(MarketingConsent).where(MarketingConsent.channel == channel.upper())
        )
    ).all()
    return {r.address_norm: r.opt_in_status for r in rows}
