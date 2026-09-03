"""Campaign analytics from REAL events only (Phase 7 §34).

Every metric is derived from CampaignRecipient states / CampaignEvent rows —
never estimated, never faked. Open/click rates are labelled indicative
(client-side blocking/prefetching makes them approximate, spec §31).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import Campaign, CampaignRecipient, RecipientStatus


class AnalyticsService:
    async def campaign_summary(self, session: AsyncSession, campaign: Campaign) -> dict:
        counters = {
            "recipients": campaign.audience_total,
            "eligible": campaign.eligible_count,
            "skipped": campaign.skipped_count,
            "queued": campaign.queued_count,
            "sent": campaign.sent_count,
            "delivered": campaign.delivered_count,
            "bounced": campaign.bounced_count,
            "complained": campaign.complained_count,
            "opened": campaign.opened_count,
            "clicked": campaign.clicked_count,
            "replied": campaign.replied_count,
            "unsubscribed": campaign.unsubscribed_count,
            "failed": campaign.failed_count,
        }

        # Recompute from recipient rows so counters can never drift from truth.
        rows = (
            await session.execute(
                select(CampaignRecipient.status, func.count())
                .where(CampaignRecipient.campaign_id == campaign.id)
                .group_by(CampaignRecipient.status)
            )
        ).all()
        by_status = {status: count for status, count in rows}
        recipients_total = sum(by_status.values())

        def _n(status: str) -> int:
            return int(by_status.get(status, 0))

        delivered = _n(RecipientStatus.DELIVERED) + _n(RecipientStatus.READ) + _n(RecipientStatus.COMPLAINED)
        # every address that reached SENT or beyond WAS accepted by the provider
        sent = _n(RecipientStatus.SENT) + delivered + _n(RecipientStatus.BOUNCED)
        bounced = _n(RecipientStatus.BOUNCED)
        failed = _n(RecipientStatus.FAILED)
        skipped = _n(RecipientStatus.SKIPPED)

        def rate(numerator: int, denominator: int) -> float | None:
            if denominator <= 0:
                return None
            return round(100.0 * numerator / denominator, 2)

        rates = {
            "delivery_rate": rate(delivered, sent),
            "bounce_rate": rate(bounced, sent),
            "complaint_rate": rate(_n(RecipientStatus.COMPLAINED), sent),
            "open_rate": rate(campaign.opened_count, delivered),
            "click_rate": rate(campaign.clicked_count, delivered),
            "reply_rate": rate(campaign.replied_count, delivered),
            "unsubscribe_rate": rate(campaign.unsubscribed_count, delivered),
        }

        return {
            "campaign_id": str(campaign.id),
            "channel": campaign.channel,
            "status": campaign.status,
            "counters": counters,
            "verified": {
                "recipients_total": recipients_total,
                "sent_total": sent,
                "delivered_total": delivered,
                "bounced_total": bounced,
                "failed_total": failed,
                "skipped_total": skipped,
            },
            "rates": rates,
            "notes": [
                "Open/click rates are indicative: some email clients block or "
                "prefetch tracking pixels (spec §31)."
            ]
            if campaign.channel == "EMAIL"
            else [],
        }

    async def account_summary(self, session: AsyncSession, account_id) -> dict:
        """Sender reputation foundation (§43): bounce/complaint/delivery rates
        per sending account from actual sends."""
        rows = (
            await session.execute(
                select(CampaignRecipient.status, func.count())
                .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
                .where(Campaign.sending_account_id == account_id)
                .group_by(CampaignRecipient.status)
            )
        ).all()
        by_status = {status: count for status, count in rows}

        def _n(status: str) -> int:
            return int(by_status.get(status, 0))

        delivered = _n("DELIVERED") + _n("READ") + _n("COMPLAINED")
        sent = _n("SENT") + delivered + _n("BOUNCED")
        bounced = _n("BOUNCED")

        def rate(numerator: int, denominator: int) -> float | None:
            if denominator <= 0:
                return None
            return round(100.0 * numerator / denominator, 2)

        summary = {
            "sent_total": sent,
            "delivered_total": delivered,
            "bounced_total": bounced,
            "complained_total": _n("COMPLAINED"),
            "delivery_rate": rate(delivered, sent),
            "bounce_rate": rate(bounced, sent),
            "complaint_rate": rate(_n("COMPLAINED"), sent),
        }
        summary["warnings"] = self._reputation_warnings(summary)
        return summary

    @staticmethod
    def _reputation_warnings(summary: dict) -> list[str]:
        """Threshold warnings only — no inbox-placement guarantees (§43)."""
        warnings: list[str] = []
        bounce = summary.get("bounce_rate")
        complaint = summary.get("complaint_rate")
        if bounce is not None and bounce > 5.0:
            warnings.append(f"Bounce rate {bounce}% exceeds the 5% warning threshold")
        if complaint is not None and complaint > 0.3:
            warnings.append(f"Complaint rate {complaint}% exceeds the 0.3% warning threshold")
        return warnings
