"""Message normalizer (Phase 8 §1, §38, §39).

Every channel's payload is reduced to ONE normalized shape before it touches
the ConversationEngine. The engine never imports a provider — future channels
(SMS/Instagram/Facebook) only add a builder here.

Stored metadata is bounded and secret-free: raw payloads are truncated and
the caller decides what (already-redacted) forensic subset to keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UnifiedInboundMessage:
    """Channel-neutral inbound message (spec §38 normalizer output).

    Only fields actually received from the provider are populated — unknown
    contact attributes are never invented (§7)."""

    channel: str                                  # WHATSAPP | EMAIL
    #: normalized contact identity used for lead matching
    contact_phone: str | None = None              # E.164-ish (WHATSAPP)
    contact_email: str | None = None              # normalized (EMAIL)
    external_contact_id: str | None = None        # provider display id (wa_id…)
    provider_message_id: str | None = None        # provider's message id
    message_type: str = "TEXT"                    # TEXT/EMAIL/IMAGE/…
    body: str | None = None
    subject: str | None = None
    sender: str | None = None
    recipient: str | None = None
    occurred_at: datetime = field(default_factory=utcnow)
    #: threading headers (EMAIL: Message-ID / In-Reply-To / References, §5)
    message_id_header: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    #: bounded, provider-derived extras (media metadata, muted status…)
    metadata: dict = field(default_factory=dict)


def normalize_whatsapp_inbound(
    *,
    sender_phone: str | None,
    provider_message_id: str | None,
    message_type: str | None,
    body: str | None,
    external_contact_id: str | None = None,
    occurred_at: datetime | None = None,
    metadata: dict | None = None,
) -> UnifiedInboundMessage:
    """WhatsApp webhook value → normalized inbound message.

    The WhatsApp event normalizer has already bounded the body (8k) and kept
    only the provider-supplied profile name — nothing is added here."""
    return UnifiedInboundMessage(
        channel="WHATSAPP",
        contact_phone=(sender_phone or "").strip() or None,
        external_contact_id=external_contact_id,
        provider_message_id=provider_message_id,
        message_type=(message_type or "TEXT").upper()[:30],
        body=body,
        sender=sender_phone,
        occurred_at=occurred_at or utcnow(),
        metadata=metadata or {},
    )


def normalize_email_inbound(
    *,
    from_email: str,
    to_email: str | None,
    subject: str | None,
    body_text: str | None,
    provider_message_id: str | None = None,
    message_id_header: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    occurred_at: datetime | None = None,
    metadata: dict | None = None,
) -> UnifiedInboundMessage | None:
    """Inbound email payload → normalized message; None when the sender
    address cannot be normalized (invalid mail is never stored, §38)."""
    from app.services.marketing.email_normalization import normalize_email

    ok, normalized, _reason = normalize_email(from_email)
    if not ok:
        return None
    return UnifiedInboundMessage(
        channel="EMAIL",
        contact_email=normalized,
        external_contact_id=normalized,
        provider_message_id=(provider_message_id or "").strip()[:300] or None,
        message_type="EMAIL",
        body=(body_text or "")[:20000] or None,
        subject=(subject or "")[:300] or None,
        sender=normalized,
        recipient=(to_email or "")[:320] or None,
        occurred_at=occurred_at or utcnow(),
        message_id_header=(message_id_header or "")[:300] or None,
        in_reply_to=(in_reply_to or "")[:300] or None,
        references=(references or "")[:1000] or None,
        metadata=metadata or {},
    )
