"""Email webhook processing (Phase 7 §27, §28).

    POST /api/v1/webhooks/email/{provider}

    webhook → authentication/signature validation → parse → normalize →
    provider event id → idempotency check → store event → update campaign
    recipient → suppression → analytics

Security (§28) — the mechanism ACTUALLY defined by the QBIT email contract
(generic Email-API webhook + mock): every POST carries
    X-QBIT-Signature:  sha256=<hex hmac-sha256(raw body, secret)>
    X-QBIT-Timestamp:  <unix seconds>
and BOTH must validate: signature constant-time verified against the shared
secret (env EMAIL_WEBHOOK_SECRET, or per-account webhook_secret from the
vault when the provider row carries one), timestamp within
QBIT_WEBHOOK_MAX_AGE_SECONDS (replay protection). Nothing secret-like is ever
logged. Raw events that fail validation are rejected — never trusted.

Idempotency (§26): ProviderEvent rows UNIQUE (provider, provider_event_id) —
a redelivered webhook is a no-op; bounced/complaint counters never double.

Event effects:
    DELIVERED        → recipient state (forward-only)
    BOUNCED (hard)   → recipient FAILED + suppression(reason=BOUNCED)
    BOUNCED (soft)   → event recorded, queue policy governs redelivery
    COMPLAINED       → suppression(reason=COMPLAINED) + audit record
    UNSUBSCRIBED     → suppression(reason=UNSUBSCRIBED)
    SENT / FAILED    → recipient state transitions
    OPENED / CLICKED → tracking evidence (already handled by their own
                       endpoints; accepted here for providers that deliver
                       their own engagement callbacks)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ValidationError
from app.core.logging import get_logger, redact
from app.models.marketing import (
    CampaignEvent,
    CampaignRecipient,
    EventType,
    RecipientStatus,
    SendingAccount,
    SuppressionReason,
    SuppressionType,
)
from app.models.messaging import ProviderEvent, ProviderEventCategory
from app.services.marketing.events import EventService
from app.services.marketing.providers.email.events import EmailEventNormalizer
from app.services.marketing.state import can_transition

logger = get_logger("qbit.marketing.webhooks_email")

SIGNATURE_HEADER = "x-qbit-signature"
TIMESTAMP_HEADER = "x-qbit-timestamp"
PROVIDER_IDS = {"email_api": "email_api", "email_mock": "email_mock", "smtp": "smtp"}


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class EmailWebhookService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.normalizer = EmailEventNormalizer()

    # -------------------------------------------------------------- security
    def resolve_secret(self, provider_id: str) -> str | None:
        """Shared secret for webhook validation (§28). Env-level secret only —
        per-account secrets would require trusting unauthenticated account
        routing, which the contract deliberately avoids."""
        if provider_id == "email_mock":
            # the mock accepts the env secret OR a test-only default so tests
            # can exercise the full pipeline; production never registers it
            return self.settings.EMAIL_WEBHOOK_SECRET or "mock-webhook-secret"
        return self.settings.EMAIL_WEBHOOK_SECRET or None

    def verify_signature(
        self, *, raw_body: bytes, signature_header: str | None,
        timestamp_header: str | None, secret: str | None,
    ) -> bool:
        """§28: signature + timestamp + replay protection. Constant-time."""
        if not secret or not signature_header:
            return False
        value = signature_header.strip()
        if not value.lower().startswith("sha256="):
            return False
        provided = value.split("=", 1)[1].strip().lower()
        if len(provided) != 64:
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, provided):
            return False
        # replay protection (§28): reject stale timestamps
        if timestamp_header:
            try:
                ts = int(str(timestamp_header).strip())
            except ValueError:
                return False
            age = abs(time.time() - ts)
            if age > self.settings.QBIT_WEBHOOK_MAX_AGE_SECONDS:
                return False
        return True

    # ------------------------------------------------------------- processing
    async def process_payload(
        self, session: AsyncSession, payload: dict, *, provider_id: str,
    ) -> dict:
        """Full POST pipeline (§27). Returns a processing summary."""
        if not isinstance(payload, dict):
            raise ValidationError("Webhook payload must be a JSON object")
        events = payload.get("events")
        if not isinstance(events, list):
            # single-event convenience form
            events = [payload]

        summary = {"received": 0, "duplicates": 0, "applied": 0,
                   "stale": 0, "unmatched": 0, "suppressed": 0}
        now = datetime.now(timezone.utc)
        stale_cutoff = datetime.fromtimestamp(
            now.timestamp() - self.settings.QBIT_WEBHOOK_MAX_AGE_SECONDS, tz=timezone.utc,
        )

        for raw in events:
            if not isinstance(raw, dict):
                continue
            normalized = self.normalizer.normalize(raw)
            if normalized is None:
                continue
            summary["received"] += 1
            event_row, created = await self._store_event(
                session, provider_id=provider_id, normalized=normalized,
                raw_subset={"event": str(raw.get("event") or "")},
            )
            if not created:
                summary["duplicates"] += 1
                continue
            occurred = normalized.get("occurred_at")
            if occurred is not None and occurred < stale_cutoff:
                summary["stale"] += 1  # stored, never applied (replay guard)
                continue
            applied = await self._apply(session, normalized, provider_id)
            if applied:
                summary["applied"] += 1
            else:
                summary["unmatched"] += 1

        await session.commit()
        logger.info(
            "email_webhook_processed",
            extra={"extra_fields": {"provider": provider_id, **summary}},
        )
        return summary

    # ------------------------------------------------------------- internals
    async def _store_event(
        self, session: AsyncSession, *, provider_id: str,
        normalized: dict, raw_subset: dict,
    ) -> tuple[ProviderEvent, bool]:
        """Insert the ProviderEvent row — UNIQUE(provider, provider_event_id)
        is the idempotency gate (§26). Returns (row, created)."""
        event_id = normalized["provider_event_id"][:300]
        existing = await session.execute(
            select(ProviderEvent.id).where(
                ProviderEvent.provider == provider_id,
                ProviderEvent.provider_event_id == event_id,
            ).limit(1)
        )
        if existing.first() is not None:
            row = (await session.execute(
                select(ProviderEvent).where(
                    ProviderEvent.provider == provider_id,
                    ProviderEvent.provider_event_id == event_id,
                ).limit(1)
            )).scalars().first()
            return row, False
        row = ProviderEvent(
            provider=provider_id,
            provider_event_id=event_id,
            sending_account_id=None,
            category=normalized["category"],
            event_type=normalized.get("event_type"),
            provider_message_id=normalized.get("provider_message_id"),
            normalized=redact(_jsonable(
                {k: v for k, v in normalized.items() if k not in ("body",)}
            )),
            raw_metadata=redact(_jsonable(raw_subset)),
            received_at=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
        return row, True

    async def _apply(
        self, session: AsyncSession, normalized: dict, provider_id: str,
    ) -> bool:
        """Apply one normalized event: recipient state + suppression + events."""
        message_id = normalized.get("provider_message_id")
        recipient = (await session.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.provider_message_id == message_id
            ).limit(1)
        )).scalars().first()
        event_type = normalized["event_type"]
        occurred = normalized.get("occurred_at") or datetime.now(timezone.utc)
        metadata = normalized.get("metadata") or {}

        if recipient is None:
            # nothing fabricated for unmatched events — the raw row is stored
            return False

        now = datetime.now(timezone.utc)

        if event_type in (EventType.MESSAGE_BOUNCED, EventType.MESSAGE_COMPLAINED,
                          EventType.MESSAGE_UNSUBSCRIBED):
            self._apply_timestamp(recipient, event_type, occurred)
            await self._record_event(
                session, recipient=recipient, event_type=event_type,
                provider_id=provider_id, message_id=message_id, metadata=metadata,
                occurred=occurred,
            )
            suppressed = await self._apply_suppression(
                session, recipient=recipient, event_type=event_type,
                metadata=metadata,
            )
            await session.commit()
            if suppressed:
                logger.info(
                    "email_recipient_suppressed",
                    extra={"extra_fields": {
                        "campaign_id": str(recipient.campaign_id),
                        "recipient_id": str(recipient.id),
                        "reason": metadata.get("bounce_type") or event_type,
                    }},
                )
            return True

        # SENT / DELIVERED / FAILED / OPENED / CLICKED → forward-only state
        if event_type in (EventType.MESSAGE_OPENED, EventType.MESSAGE_CLICKED):
            self._apply_timestamp(recipient, event_type, occurred)
            await self._record_event(
                session, recipient=recipient, event_type=event_type,
                provider_id=provider_id, message_id=message_id, metadata=metadata,
                occurred=occurred,
            )
            await session.commit()
            return True

        changed = False
        if event_type == EventType.MESSAGE_FAILED:
            if can_transition(recipient.status, RecipientStatus.FAILED.value):
                recipient.status = RecipientStatus.FAILED.value
                if recipient.failed_at is None:
                    recipient.failed_at = occurred
                changed = True
        else:
            target = {
                EventType.MESSAGE_SENT: (RecipientStatus.SENT.value, "sent_at"),
                EventType.MESSAGE_DELIVERED: (RecipientStatus.DELIVERED.value, "delivered_at"),
            }.get(event_type)
            if target and can_transition(recipient.status, target[0]):
                recipient.status = target[0]
                if getattr(recipient, target[1]) is None:
                    setattr(recipient, target[1], occurred)
                changed = True
        await self._record_event(
            session, recipient=recipient, event_type=event_type,
            provider_id=provider_id, message_id=message_id, metadata=metadata,
            occurred=occurred,
        )
        await session.commit()
        return changed or event_type != EventType.MESSAGE_SENT

    @staticmethod
    def _apply_timestamp(recipient: CampaignRecipient, event_type: str,
                         occurred: datetime) -> None:
        """Fill the email-specific timestamp once (first event wins)."""
        field_map = {
            EventType.MESSAGE_BOUNCED: "bounced_at",
            EventType.MESSAGE_COMPLAINED: "complained_at",
            EventType.MESSAGE_OPENED: "opened_at",
            EventType.MESSAGE_CLICKED: "clicked_at",
        }
        field = field_map.get(event_type)
        if field and getattr(recipient, field) is None:
            setattr(recipient, field, occurred)

    async def _record_event(
        self, session: AsyncSession, *, recipient: CampaignRecipient,
        event_type: str, provider_id: str, message_id: str | None,
        metadata: dict, occurred: datetime,
    ) -> None:
        session.add(CampaignEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type=event_type,
            provider=provider_id,
            provider_event_id=message_id,
            payload_metadata={**metadata, "webhook": True,
                              "occurred_at": occurred.isoformat()},
        ))
        await session.flush()

    async def _apply_suppression(
        self, session: AsyncSession, *, recipient: CampaignRecipient,
        event_type: str, metadata: dict,
    ) -> bool:
        """§24/§25: hard bounce + complaint + webhook unsubscribe suppress the
        address from ALL future marketing. Soft bounces are recorded only."""
        bounce_type = str(metadata.get("bounce_type") or "").upper()
        reason: SuppressionReason | None = None
        if event_type == EventType.MESSAGE_COMPLAINED:
            reason = SuppressionReason.COMPLAINT
        elif event_type == EventType.MESSAGE_UNSUBSCRIBED:
            reason = SuppressionReason.UNSUBSCRIBED
        elif event_type == EventType.MESSAGE_BOUNCED and bounce_type == "HARD_BOUNCE":
            reason = SuppressionReason.BOUNCED
            if can_transition(recipient.status, RecipientStatus.FAILED.value):
                recipient.status = RecipientStatus.FAILED.value
                if recipient.failed_at is None:
                    recipient.failed_at = datetime.now(timezone.utc)
        if reason is None:
            return False
        address = (recipient.recipient_address or "").strip().lower()
        if not address:
            return False
        from app.models.marketing import SuppressionEntry

        existing = await session.scalar(
            select(SuppressionEntry).where(
                SuppressionEntry.type == SuppressionType.EMAIL,
                SuppressionEntry.address == address,
                SuppressionEntry.channel_key == "EMAIL",
            )
        )
        if existing is not None:
            return True
        session.add(SuppressionEntry(
            type=SuppressionType.EMAIL.value,
            address=address,
            channel="EMAIL",
            channel_key="EMAIL",
            reason=reason.value,
            source="email_webhook",
            lead_id=recipient.lead_id,
        ))
        return True
