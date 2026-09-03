"""WhatsApp webhook processing (Phase 6 §17–§20).

    GET  /api/v1/webhooks/whatsapp   verification challenge (hub.verify_token)
    POST /api/v1/webhooks/whatsapp   X-Hub-Signature-256 → parse → normalize →
                                     store (idempotent) → update recipients →
                                     conversations → analytics

Security (§18):
- the GET challenge compares hub.verify_token against the configured token
- every POST requires a valid X-Hub-Signature-256 HMAC-SHA256 of the RAW body
  keyed with the app secret (constant-time comparison; missing/invalid → 401)
- payloads larger than QBIT_WEBHOOK_MAX_BODY_BYTES are rejected (413)
- events older than QBIT_WEBHOOK_MAX_AGE_SECONDS are stored but NOT applied
  (replay/stale-event protection without data loss)
- nothing secret-like is ever logged (no signatures, no tokens, no headers)

Idempotency (§20):
- ProviderEvent rows are UNIQUE (provider, provider_event_id); a redelivered
  webhook is a no-op, so delivered/read counters can never double-count
- event ids are stable composites: "<wamid>:<status>" for delivery receipts,
  the provider message id for inbound messages

Webhook code NEVER manipulates UI state directly — it writes events/rows and
the UI reads them through the normal API/analytics path (§17).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ValidationError
from app.core.logging import get_logger, redact
from app.models.marketing import CampaignEvent, CampaignRecipient, EventType, SendingAccount
from app.models.messaging import ProviderEvent, ProviderEventCategory
from app.services.marketing.events import EventService
from app.services.marketing.inbox import InboxService
from app.services.marketing.state import apply_event

logger = get_logger("qbit.marketing.webhooks")

PROVIDER_ID = "whatsapp_cloud"

#: provider status string → canonical campaign event type
STATUS_EVENT_MAP = {
    "sent": EventType.MESSAGE_SENT,
    "delivered": EventType.MESSAGE_DELIVERED,
    "read": EventType.MESSAGE_READ,
    "failed": EventType.MESSAGE_FAILED,
    "deleted": EventType.MESSAGE_FAILED,  # recipient deleted the message
}

SIGNATURE_HEADER = "x-hub-signature-256"


class WhatsAppEventNormalizer:
    """Raw WhatsApp webhook value → normalized event dicts (§17)."""

    def normalize_delivery(self, status_entry: dict) -> dict | None:
        message_id = str(status_entry.get("id") or "").strip()
        status = str(status_entry.get("status") or "").strip().lower()
        if not message_id or not status:
            return None
        event_type = STATUS_EVENT_MAP.get(status)
        if event_type is None:
            return None
        errors = status_entry.get("errors") if isinstance(status_entry.get("errors"), list) else []
        return {
            "category": ProviderEventCategory.DELIVERY.value,
            "provider_event_id": f"{message_id}:{status}",
            "provider_message_id": message_id,
            "event_type": event_type,
            "occurred_at": self._ts(status_entry.get("timestamp")),
            "metadata": {
                "status": status,
                "recipient_id": status_entry.get("recipient_id"),
                "errors": [
                    {
                        "code": err.get("code"),
                        "title": str(err.get("title") or "")[:200],
                        "message": str(err.get("message") or "")[:500],
                    }
                    for err in errors if isinstance(err, dict)
                ],
            },
        }

    def normalize_inbound(self, message_entry: dict, profile_name: str | None = None) -> dict | None:
        message_id = str(message_entry.get("id") or "").strip()
        sender = str(message_entry.get("from") or "").strip()
        if not message_id or not sender:
            return None
        mtype = str(message_entry.get("type") or "unknown").strip().lower()
        body = None
        if mtype == "text":
            text = message_entry.get("text") or {}
            body = str(text.get("body") or "")[:8000] or None
        normalized = {
            "category": ProviderEventCategory.INBOUND.value,
            "provider_event_id": f"{message_id}:inbound",
            "provider_message_id": message_id,
            "event_type": "MESSAGE_INBOUND",
            "occurred_at": self._ts(message_entry.get("timestamp")),
            "metadata": {
                "from": sender,
                "type": mtype,
                # profile_name is provider-supplied display data, not personal
                # information we inferred — kept only as the external contact id
                "profile_name": (profile_name or "")[:200] or None,
            },
            "body": body,
            "message_type": mtype,
            "sender": sender,
        }
        return normalized

    @staticmethod
    def _ts(raw) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc) if raw else None
        except (TypeError, ValueError, OSError):
            return None


def _jsonable(value):
    """Recursively convert datetimes to ISO strings for JSON columns."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class WhatsAppWebhookService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.normalizer = WhatsAppEventNormalizer()
        self.inbox = InboxService()

    # ------------------------------------------------------------- GET flow
    def verify_challenge(self, *, mode: str | None, token: str | None, challenge: str | None) -> str:
        """Meta subscription handshake (§19 GET). Raises on any mismatch."""
        expected = self.settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
        if not expected:
            raise ValidationError(
                "Webhook verification token is not configured on the server"
            )
        if (mode or "") != "subscribe":
            raise ValidationError("hub.mode must be 'subscribe'")
        if not token or not hmac.compare_digest(token, expected):
            raise ValidationError("hub.verify_token does not match")
        return (challenge or "").strip()

    # ------------------------------------------------------------- POST flow
    def resolve_app_secret(self) -> str | None:
        return self.settings.WHATSAPP_APP_SECRET or None

    def verify_signature(self, *, raw_body: bytes, signature_header: str | None) -> bool:
        """X-Hub-Signature-256 validation (§18). Constant-time; no secrets in
        errors (a plain False → the endpoint answers 401)."""
        secret = self.resolve_app_secret()
        if not secret or not signature_header:
            return False
        value = signature_header.strip()
        if not value.lower().startswith("sha256="):
            return False
        expected = hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        provided = value.split("=", 1)[1].strip().lower()
        if len(provided) != 64:
            return False
        return hmac.compare_digest(expected, provided)

    # ---------------------------------------------------------- processing
    async def process_payload(self, session: AsyncSession, payload: dict) -> dict:
        """Full POST pipeline (§19): parse → normalize → store (idempotent) →
        update recipients / conversations. Returns a processing summary."""
        if not isinstance(payload, dict):
            raise ValidationError("Webhook payload must be a JSON object")
        entries = payload.get("entry")
        if not isinstance(entries, list):
            raise ValidationError("Webhook payload has no entry list")

        summary = {"received": 0, "duplicates": 0, "applied": 0, "inbound": 0, "stale": 0, "unmatched": 0}
        now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(seconds=self.settings.QBIT_WEBHOOK_MAX_AGE_SECONDS)

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                account = await self._account_for(session, value)
                field_name = str(change.get("field") or "messages")

                for status_entry in value.get("statuses") or []:
                    if not isinstance(status_entry, dict):
                        continue
                    normalized = self.normalizer.normalize_delivery(status_entry)
                    if normalized is None:
                        continue
                    summary["received"] += 1
                    event_row, created = await self._store_event(
                        session, account=account, normalized=normalized,
                        raw_subset={"field": field_name, "status": normalized["metadata"]["status"]},
                    )
                    if not created:
                        summary["duplicates"] += 1
                        continue
                    occurred = normalized.get("occurred_at")
                    if occurred is not None and occurred < stale_cutoff:
                        summary["stale"] += 1  # stored, never applied (§18)
                        continue
                    applied = await self._apply_delivery(session, account, normalized, event_row)
                    if applied:
                        summary["applied"] += 1
                    else:
                        summary["unmatched"] += 1

                contacts_profile = {}
                contacts = value.get("contacts")
                if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
                    profile = contacts[0].get("profile")
                    if isinstance(profile, dict):
                        contacts_profile = profile

                for message_entry in value.get("messages") or []:
                    if not isinstance(message_entry, dict):
                        continue
                    normalized = self.normalizer.normalize_inbound(
                        message_entry, profile_name=contacts_profile.get("name"),
                    )
                    if normalized is None:
                        continue
                    summary["received"] += 1
                    _event_row, created = await self._store_event(
                        session, account=account, normalized=normalized,
                        raw_subset={"field": field_name, "from": normalized["metadata"]["from"]},
                    )
                    if not created:
                        summary["duplicates"] += 1
                        continue
                    await self._apply_inbound(session, account, normalized)
                    summary["inbound"] += 1

        await session.commit()
        logger.info(
            "whatsapp_webhook_processed",
            extra={"extra_fields": {k: v for k, v in summary.items()}},
        )
        return summary

    # ------------------------------------------------------------- internals
    async def _account_for(self, session: AsyncSession, value: dict) -> SendingAccount | None:
        """Resolve the sending account by phone_number_id (multi-account §3)."""
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        phone_number_id = str(metadata.get("phone_number_id") or "").strip()
        if not phone_number_id:
            return None
        return (await session.execute(
            select(SendingAccount).where(
                SendingAccount.phone_number_id == phone_number_id,
                SendingAccount.channel == "WHATSAPP",
            ).limit(1)
        )).scalars().first()

    async def _store_event(
        self, session: AsyncSession, *, account: SendingAccount | None,
        normalized: dict, raw_subset: dict,
    ) -> tuple[ProviderEvent, bool]:
        """Insert the ProviderEvent row — UNIQUE(provider, provider_event_id)
        is the idempotency gate (§20). Returns (row, created)."""
        event_id = normalized["provider_event_id"][:300]
        existing = await session.execute(
            select(ProviderEvent.id).where(
                ProviderEvent.provider == PROVIDER_ID,
                ProviderEvent.provider_event_id == event_id,
            ).limit(1)
        )
        if existing.first() is not None:
            row = (await session.execute(
                select(ProviderEvent).where(
                    ProviderEvent.provider == PROVIDER_ID,
                    ProviderEvent.provider_event_id == event_id,
                ).limit(1)
            )).scalars().first()
            return row, False
        row = ProviderEvent(
            provider=PROVIDER_ID,
            provider_event_id=event_id,
            sending_account_id=account.id if account else None,
            category=normalized["category"],
            event_type=normalized.get("event_type"),
            provider_message_id=normalized.get("provider_message_id"),
            normalized=redact(_jsonable({k: v for k, v in normalized.items() if k != "body"})),
            raw_metadata=redact(raw_subset),
            received_at=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
        return row, True

    async def _apply_delivery(
        self, session: AsyncSession, account: SendingAccount | None,
        normalized: dict, event_row: ProviderEvent,
    ) -> bool:
        """DELIVERY: match the recipient by provider_message_id, apply the
        state machine (§21), append the CampaignEvent (analytics follow)."""
        message_id = normalized.get("provider_message_id")
        recipient = (await session.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.provider_message_id == message_id
            ).limit(1)
        )).scalars().first()

        occurred = normalized.get("occurred_at") or datetime.now(timezone.utc)
        if recipient is not None:
            apply_event(recipient, normalized["event_type"], timestamp=occurred)
            await session.commit()
            await EventService().record(
                session, campaign_id=recipient.campaign_id, recipient_id=recipient.id,
                event_type=normalized["event_type"], provider=PROVIDER_ID,
                provider_event_id=message_id,
                metadata={
                    "webhook": True,
                    "provider_event_row": str(event_row.id),
                    **(normalized.get("metadata") or {}),
                },
            )
            return True
        # no campaign recipient matched (e.g. a manual message) — the raw
        # ProviderEvent is still stored; nothing is fabricated
        return False

    async def _apply_inbound(
        self, session: AsyncSession, account: SendingAccount | None,
        normalized: dict,
    ) -> None:
        """INBOUND (§22): normalized message → lead match → conversation →
        message row; then, when possible, link a campaign REPLIED event."""
        conversation, _message = await self.inbox.record_inbound_message(
            session,
            account=account,
            contact_phone=normalized.get("sender"),
            external_contact_id=normalized["metadata"].get("profile_name"),
            provider_message_id=normalized.get("provider_message_id"),
            message_type=normalized.get("message_type") or "TEXT",
            body=normalized.get("body"),
            occurred_at=normalized.get("occurred_at"),
            metadata={"provider": PROVIDER_ID, "type": normalized.get("message_type")},
        )
        await self.inbox.link_reply_to_campaign(
            session, account=account, contact_phone=normalized.get("sender"),
            occurred_at=normalized.get("occurred_at"),
        )
        _ = conversation
