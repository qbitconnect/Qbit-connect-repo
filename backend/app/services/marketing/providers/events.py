"""Provider event normalization (Phase 7 §26).

Webhook payloads are translated into NormalizedEvent values. The generic
email schema is:

    {
      "events": [
        {
          "id": "<provider event id>",          # required — idempotency key
          "type": "send|delivered|bounce|complaint|open|click|reply|fail",
          "message_id": "<provider message id>",
          "recipient": "user@example.com",
          "timestamp": 1730000000,               # unix seconds (optional)
          "hard": true,                          # bounce hardness (optional)
          "reason": "mailbox unavailable",       # optional
          "meta": {...}                          # optional extra payload
        }
      ]
    }

Vendor-specific adapters can subclass EmailEventNormalizer and translate their
native schema into this one. Nothing is invented: unknown types are dropped
honestly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.marketing.providers.base import NormalizedEvent

EVENT_TYPE_MAP = {
    "send": "SENT",
    "sent": "SENT",
    "delivered": "DELIVERED",
    "delivery": "DELIVERED",
    "bounce": "BOUNCED",
    "bounced": "BOUNCED",
    "complaint": "COMPLAINED",
    "complained": "COMPLAINED",
    "spamreport": "COMPLAINED",
    "open": "OPENED",
    "opened": "OPENED",
    "click": "CLICKED",
    "clicked": "CLICKED",
    "reply": "REPLIED",
    "replied": "REPLIED",
    "fail": "FAILED",
    "failed": "FAILED",
    "rejected": "FAILED",
    "unsubscribed": "UNSUBSCRIBED",
    "unsubscribe": "UNSUBSCRIBED",
}


def normalize_event_type(raw: str | None) -> str | None:
    if not raw:
        return None
    return EVENT_TYPE_MAP.get(str(raw).strip().lower())


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _parse_ts(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None
    return None


class EmailEventNormalizer:
    """Generic email webhook payload → NormalizedEvent list."""

    def normalize(self, payload: dict) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        raw_events = payload.get("events")
        if raw_events is None and isinstance(payload.get("event"), (str, dict)):
            raw_events = [payload]
        if not isinstance(raw_events, list):
            return events
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            event_type = normalize_event_type(raw.get("type") or raw.get("event"))
            provider_event_id = str(raw.get("id") or "").strip()
            if not event_type or not provider_event_id:
                continue  # honestly skip malformed entries
            events.append(
                NormalizedEvent(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                    provider_message_id=(
                        str(raw.get("message_id")) if raw.get("message_id") else None
                    ),
                    recipient=(str(raw["recipient"]).strip() if raw.get("recipient") else None),
                    timestamp=_parse_ts(raw.get("timestamp")),
                    hard_bounce=bool(raw.get("hard", False)) if event_type == "BOUNCED" else False,
                    reason=(str(raw["reason"])[:500] if raw.get("reason") else None),
                    payload=raw.get("meta") or {},
                )
            )
        return events


class WhatsAppEventNormalizer:
    """WhatsApp Cloud API webhook → NormalizedEvent list.

    statuses: sent / delivered / read / failed
    messages (inbound): text replies → REPLIED + inbox ingestion handled by
    the conversations service (spec §32) — this normalizer only normalizes.
    """

    STATUS_MAP = {
        "sent": "SENT",
        "delivered": "DELIVERED",
        "read": "READ",
        "failed": "FAILED",
        "deleted": "FAILED",
    }

    def normalize(self, payload: dict) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        entry_list = payload.get("entry")
        if not isinstance(entry_list, list):
            return events
        for entry in entry_list:
            for change in entry.get("changes", []) if isinstance(entry, dict) else []:
                value = (change or {}).get("value", {}) if isinstance(change, dict) else {}
                field_name = (change or {}).get("field", "")
                for status in value.get("statuses", []) or []:
                    etype = self.STATUS_MAP.get(str(status.get("status", "")).lower())
                    if not etype:
                        continue
                    errors = status.get("errors") or []
                    reason = None
                    if errors:
                        first = errors[0] or {}
                        reason = (
                            f"{first.get('title', 'error')} ({first.get('code', 'unknown')})"
                        )
                    ts = _parse_ts(status.get("timestamp"))
                    # Cloud API: the status entry's `id` IS the message id (wamid).
                    message_id = str(status.get("id")) if status.get("id") else None
                    events.append(
                        NormalizedEvent(
                            provider_event_id=str(
                                status.get("id")
                                or f"{value.get('metadata', {}).get('phone_number_id', 'wa')}:{status.get('timestamp')}"
                            ),
                            event_type=etype,
                            provider_message_id=message_id,
                            recipient=(
                                str(status["recipient_id"]) if status.get("recipient_id") else None
                            ),
                            timestamp=ts,
                            reason=reason,
                            payload={
                                "status_id": status.get("id"),
                                "field": field_name,
                            },
                        )
                    )
                for message in value.get("messages", []) or []:
                    if message.get("type") == "text":
                        events.append(
                            NormalizedEvent(
                                provider_event_id=str(message.get("id")),
                                event_type="REPLIED",
                                provider_message_id=str(message.get("id")),
                                recipient=None,
                                timestamp=_parse_ts(message.get("timestamp")),
                                payload={
                                    "text": (message.get("text") or {}).get("body"),
                                    "from": value.get("contacts", [{}])[0].get(
                                        "wa_id", message.get("from")
                                    )
                                    if value.get("contacts")
                                    else message.get("from"),
                                    "field": field_name,
                                },
                            )
                        )
        return events


email_event_normalizer = EmailEventNormalizer()
whatsapp_event_normalizer = WhatsAppEventNormalizer()
