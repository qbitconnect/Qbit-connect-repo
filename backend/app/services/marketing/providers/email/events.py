"""Email event normalization (Phase 7 §24, §25, §26).

    provider webhook/event payload
        ↓ EmailEventNormalizer
    normalized event dict
        ↓ ProviderEvent store (UNIQUE provider_event_id — idempotent)
    CampaignEvent → recipient state → suppression → analytics

Normalized event vocabulary (§26):
    SENT, DELIVERED, BOUNCED, COMPLAINED, FAILED  (+ OPENED, CLICKED from
    our own tracking endpoints; REPLIED from the inbound-email interface)

Bounce classification (§24):
    HARD_BOUNCE  — permanent (bad mailbox) → recipient FAILED + suppression
    SOFT_BOUNCE  — temporary (mailbox full, greylisting) → recorded, the
                   queue's retry policy governs any redelivery attempt
Complaint (§25): always suppressed from future marketing + audit record.

The documented webhook envelope (generic Email-API contract):
    {"provider_event_id": "...", "message_id": "...", "event": "delivered",
     "timestamp": 1730000000,
     "bounce": {"type": "hard" | "soft", "diagnostic": "..."},
     "recipient": "someone@example.com"}
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.marketing import EventType
from app.models.messaging import ProviderEventCategory


class EmailEventNormalizer:
    """Raw provider event → normalized dict for the webhook pipeline."""

    EVENT_MAP = {
        "sent": EventType.MESSAGE_SENT,
        "delivered": EventType.MESSAGE_DELIVERED,
        "opened": EventType.MESSAGE_OPENED,
        "clicked": EventType.MESSAGE_CLICKED,
        "failed": EventType.MESSAGE_FAILED,
        "bounced": EventType.MESSAGE_BOUNCED,
        "complaint": EventType.MESSAGE_COMPLAINED,
        "complained": EventType.MESSAGE_COMPLAINED,
        "unsubscribed": EventType.MESSAGE_UNSUBSCRIBED,
    }

    def normalize(self, payload: dict) -> dict | None:
        """Normalize one provider event. Returns None for unknown/unusable
        payloads (they are rejected honestly, never guessed into shape)."""
        if not isinstance(payload, dict):
            return None
        event_raw = str(payload.get("event") or payload.get("event_type") or "").strip().lower()
        if not event_raw:
            return None
        event_type = self.EVENT_MAP.get(event_raw)
        if event_type is None:
            return None
        message_id = str(payload.get("message_id") or payload.get("provider_message_id") or "").strip()
        if not message_id:
            return None
        provider_event_id = str(payload.get("provider_event_id") or "").strip()
        if not provider_event_id:
            # stable composite id so redelivered webhooks dedupe (§26 idempotency)
            provider_event_id = f"{message_id}:{event_raw}"

        metadata: dict = {"event": event_raw}
        occurred_at = self._ts(payload.get("timestamp"))

        bounce_type = None
        if event_type == EventType.MESSAGE_BOUNCED:
            bounce = payload.get("bounce") if isinstance(payload.get("bounce"), dict) else {}
            raw_type = str(bounce.get("type") or payload.get("bounce_type") or "").strip().lower()
            if raw_type in ("hard", "hard_bounce", "permanent"):
                bounce_type = "HARD_BOUNCE"
            elif raw_type in ("soft", "soft_bounce", "transient"):
                bounce_type = "SOFT_BOUNCE"
            else:
                # unclassified bounce: treat conservatively as soft (recorded,
                # retried by policy) — never fabricate a hard-bounce suppression
                bounce_type = "SOFT_BOUNCE"
            metadata["bounce_type"] = bounce_type
            diagnostic = str(bounce.get("diagnostic") or payload.get("diagnostic") or "").strip()
            if diagnostic:
                metadata["diagnostic"] = diagnostic[:500]

        recipient = str(payload.get("recipient") or "").strip() or None
        return {
            "category": ProviderEventCategory.DELIVERY.value,
            "provider_event_id": provider_event_id[:300],
            "provider_message_id": message_id[:300],
            "event_type": event_type,
            "occurred_at": occurred_at,
            "metadata": metadata,
            "recipient": recipient,
        }

    @staticmethod
    def _ts(raw) -> datetime | None:
        if raw in (None, ""):
            return None
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
        try:
            parsed = datetime.fromisoformat(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError):
            return None
