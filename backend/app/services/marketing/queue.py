"""Campaign send queue (Phase 5 §17–§21).

DB-backed queue: `campaign_queue` rows are the source of truth (Redis is a
signal channel, never the record — same rule as scrape checkpoints). The
worker claims WAITING items with a guarded UPDATE (lease), sends them through
the provider, and transitions state honestly.

- Idempotency (§20): UNIQUE (campaign_id, recipient_id, message_version).
- Retry (§19): TRANSIENT failures back off exponentially; PERMANENT failures
  go straight to FAILED. Max attempts bounded (never retry endlessly).
- Rate control (§21): conservative per-account operational throttling —
  a claims gate checks completed/processing counts per account in the last
  minute/hour against the account's rate policy. This throttles OUR send
  pace; it is never used to evade provider restrictions.
- Pause/Cancel (§23): paused campaigns stop releasing work; cancel marks
  pending items CANCELLED without touching completed provider events.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.marketing import (
    Campaign,
    CampaignQueueItem,
    QueueStatus,
    RecipientStatus,
    SendingAccount,
)

logger = get_logger("qbit.marketing.queue")

#: statuses that keep an account's rate budget "occupied"
ACTIVE_SEND_STATUSES = (QueueStatus.PROCESSING, QueueStatus.COMPLETED)


class QueueService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    # ---------------------------------------------------------------- enqueue
    async def enqueue(
        self, session: AsyncSession, *,
        campaign: Campaign, recipient_ids: Iterable[uuid.UUID],
        sending_account_id: uuid.UUID | None,
        batch_size: int = 1000,
    ) -> int:
        """Create WAITING queue items for recipients (§17, §18).

        Bulk insert with ON CONFLICT DO NOTHING against the idempotency key
        (campaign_id, recipient_id, message_version) — duplicate enqueue
        attempts (worker restart, API retry, double launch) are harmless.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(timezone.utc)
        ids = list(recipient_ids)
        created = 0
        for start in range(0, len(ids), batch_size):
            chunk = ids[start:start + batch_size]
            payload = [{
                "campaign_id": campaign.id,
                "recipient_id": rid,
                "channel": campaign.channel,
                "sending_account_id": sending_account_id,
                "status": QueueStatus.WAITING,
                "available_at": now,
            } for rid in chunk]
            if not payload:
                continue
            stmt = pg_insert(CampaignQueueItem)
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                stmt = sqlite_insert(CampaignQueueItem)
            stmt = stmt.on_conflict_do_nothing().returning(CampaignQueueItem.id)
            result = await session.execute(stmt, payload)
            created += len(result.scalars().all())
        await session.commit()
        return created

    async def enqueue_from_snapshot(
        self, session: AsyncSession, *,
        campaign: Campaign, sending_account_id: uuid.UUID | None,
        batch_size: int = 1000,
    ) -> int:
        """Queue every ELIGIBLE recipient of a campaign snapshot."""
        recipient_rows = await session.execute(
            select(CampaignRecipient.id)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status == RecipientStatus.ELIGIBLE,
            )
            .order_by(CampaignRecipient.id)
        )
        recipient_ids = [row[0] for row in recipient_rows.all()]
        return await self.enqueue(
            session, campaign=campaign, recipient_ids=recipient_ids,
            sending_account_id=sending_account_id, batch_size=batch_size,
        )

    # ----------------------------------------------------------------- claim
    async def claim_batch(
        self, session: AsyncSession, *,
        owner: str, batch_size: int = 25,
        lease_minutes: int = 15,
    ) -> list[CampaignQueueItem]:
        """Claim WAITING items whose campaign is RUNNING (or QUEUED→running
        transition handled by the worker) via a guarded UPDATE lease."""
        now = datetime.now(timezone.utc)
        claim_cutoff = now - timedelta(minutes=lease_minutes)

        candidate_ids = (await session.execute(
            select(CampaignQueueItem.id)
            .join(Campaign, Campaign.id == CampaignQueueItem.campaign_id)
            .where(
                CampaignQueueItem.status.in_([QueueStatus.WAITING, QueueStatus.RETRY]),
                CampaignQueueItem.available_at <= now,
                Campaign.status.in_(["RUNNING"]),
            )
            .order_by(CampaignQueueItem.priority, CampaignQueueItem.available_at)
            .limit(batch_size)
        )).scalars().all()
        if not candidate_ids:
            return []

        claimed = (await session.execute(
            update(CampaignQueueItem)
            .where(
                CampaignQueueItem.id.in_(candidate_ids),
                CampaignQueueItem.status.in_([QueueStatus.WAITING, QueueStatus.RETRY]),
                CampaignQueueItem.available_at <= now,
            )
            .values(
                status=QueueStatus.PROCESSING,
                locked_at=now,
                lease_owner=owner,
                attempts=CampaignQueueItem.attempts + 1,
                updated_at=now,
            )
            .returning(CampaignQueueItem.id)
        )).scalars().all()
        await session.commit()
        if not claimed:
            return []
        rows = await session.execute(
            select(CampaignQueueItem).where(CampaignQueueItem.id.in_(claimed))
        )
        items = list(rows.scalars().all())
        # stale lease recovery: items PROCESSING longer than the lease window
        stale = (await session.execute(
            select(CampaignQueueItem).where(
                CampaignQueueItem.status == QueueStatus.PROCESSING,
                CampaignQueueItem.locked_at.isnot(None),
                CampaignQueueItem.locked_at < claim_cutoff,
            ).limit(batch_size)
        )).scalars().all()
        if stale:
            await session.execute(
                update(CampaignQueueItem)
                .where(CampaignQueueItem.id.in_([s.id for s in stale]))
                .values(status=QueueStatus.WAITING, lease_owner=None, locked_at=None)
            )
            await session.commit()
            logger.warning(
                "Recovered stale queue leases",
                extra={"extra_fields": {"count": len(stale)}},
            )
        return items

    # ---------------------------------------------------------- rate control
    async def rate_gate(
        self, session: AsyncSession, *,
        account: SendingAccount | None, settings: Settings,
    ) -> tuple[bool, str | None]:
        """Conservative operational throttle (§21). Returns (allowed, wait_hint).

        - per-minute: PROCESSING+COMPLETED items for the account in the last
          60 seconds must stay under messages_per_minute
        - per-hour: same over the last hour
        Defaults come from settings; accounts may override via
        config_metadata.rate_policy (messages_per_minute / messages_per_hour).
        """
        if account is None:
            return True, None
        policy = (account.config_metadata or {}).get("rate_policy") or {}
        # Phase 7 §42 vocabulary (emails_per_minute/hour) is accepted as an
        # alias of the Phase 5 keys — one operational throttle, two names.
        def _policy_value(primary: str, alias: str, fallback: int) -> int:
            raw = policy.get(primary, policy.get(alias, fallback))
            try:
                return int(raw)
            except (TypeError, ValueError):
                return fallback
        per_minute = _policy_value(
            "messages_per_minute", "emails_per_minute",
            settings.QBIT_MARKETING_RATE_PER_MINUTE,
        )
        per_hour = _policy_value(
            "messages_per_hour", "emails_per_hour",
            settings.QBIT_MARKETING_RATE_PER_HOUR,
        )
        now = datetime.now(timezone.utc)

        minute_count = (await session.scalar(
            select(func.count()).select_from(CampaignQueueItem).where(
                CampaignQueueItem.sending_account_id == account.id,
                CampaignQueueItem.status.in_(ACTIVE_SEND_STATUSES),
                CampaignQueueItem.updated_at >= now - timedelta(seconds=60),
            )
        )) or 0
        if minute_count >= per_minute:
            return False, f"minute limit ({per_minute}/min)"
        hour_count = (await session.scalar(
            select(func.count()).select_from(CampaignQueueItem).where(
                CampaignQueueItem.sending_account_id == account.id,
                CampaignQueueItem.status.in_(ACTIVE_SEND_STATUSES),
                CampaignQueueItem.updated_at >= now - timedelta(hours=1),
            )
        )) or 0
        if hour_count >= per_hour:
            return False, f"hour limit ({per_hour}/hour)"
        return True, None

    # ----------------------------------------------------------- transitions
    async def complete(
        self, session: AsyncSession, item: CampaignQueueItem, *,
        provider_message_id: str | None = None,
    ) -> None:
        item.status = QueueStatus.COMPLETED
        item.completed_at = datetime.now(timezone.utc)
        item.last_error = None
        item.locked_at = None
        item.lease_owner = None
        if provider_message_id:
            from app.models.marketing import CampaignRecipient
            recipient = await session.get(CampaignRecipient, item.recipient_id)
            if recipient is not None and recipient.provider_message_id is None:
                recipient.provider_message_id = provider_message_id
        await session.commit()

    async def fail(
        self, session: AsyncSession, item: CampaignQueueItem, *,
        error: str, error_class: str, settings: Settings,
        provider_retry_after: float | None = None,
    ) -> str:
        """Honest failure handling with controlled retry (§19).

        TRANSIENT → RETRY with exponential backoff until max attempts, then
        FAILED. PERMANENT → FAILED immediately (never retried). CONFIGURATION
        → FAILED immediately (system issue, retrying will not fix config).
        provider_retry_after (§30): when the provider says "wait N seconds",
        the backoff NEVER fires earlier than that — we respect the hint, we
        never use it to time evasion. Returns the resulting status string.
        """
        now = datetime.now(timezone.utc)
        item.locked_at = None
        item.lease_owner = None
        item.last_error = (error or "unknown error")[:2000]
        max_attempts = settings.QBIT_MARKETING_MAX_ATTEMPTS
        if (
            error_class == "TRANSIENT"
            and item.attempts < max_attempts
        ):
            backoff = min(
                settings.QBIT_MARKETING_RETRY_BASE_SECONDS * (2 ** max(0, item.attempts - 1)),
                settings.QBIT_MARKETING_RETRY_MAX_SECONDS,
            )
            if provider_retry_after is not None and provider_retry_after > 0:
                backoff = max(backoff, float(provider_retry_after))
            item.status = QueueStatus.RETRY
            item.available_at = now + timedelta(seconds=backoff)
            await session.commit()
            return QueueStatus.RETRY
        item.status = QueueStatus.FAILED
        item.completed_at = now
        await session.commit()
        return QueueStatus.FAILED

    async def cancel_pending(self, session: AsyncSession, *, campaign_id: uuid.UUID) -> int:
        """Cancel pending items (§23) — completed provider events untouched."""
        result = await session.execute(
            update(CampaignQueueItem)
            .where(
                CampaignQueueItem.campaign_id == campaign_id,
                CampaignQueueItem.status.in_([
                    QueueStatus.WAITING, QueueStatus.RETRY, QueueStatus.PROCESSING,
                ]),
            )
            .values(
                status=QueueStatus.CANCELLED,
                completed_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        return int(result.rowcount or 0)

    async def counts(self, session: AsyncSession, *, campaign_id: uuid.UUID) -> dict:
        rows = await session.execute(
            select(CampaignQueueItem.status, func.count())
            .where(CampaignQueueItem.campaign_id == campaign_id)
            .group_by(CampaignQueueItem.status)
        )
        return {status: count for status, count in rows.all()}
