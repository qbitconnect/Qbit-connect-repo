"""Suppression registry + unsubscribe token processing (Phase 7 §13, §14, §25, §47).

Compliance rules baked in:
- Unsubscribed / hard-bounced / complained addresses are suppressed for ALL
  future campaigns on that channel (spec §14, §24, §25).
- Suppression rows are NEVER silently removed for UNSUBSCRIBED/HARD_BOUNCE/
  COMPLAINT reasons — only an explicit compliant re-subscription flow may
  clear them (implemented as `resubscribe()`, audited).
- Unsubscribe tokens: 32 random bytes (urlsafe), only the SHA-256 hash is
  stored, no ids encoded, single-use, expiry-tracked (§13, §51).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.models.marketing import (
    MarketingConsent,
    Suppression,
    UnsubscribeToken,
)
from app.services.marketing.normalization import normalize_email, normalize_phone

TERMINAL_REASONS = {"UNSUBSCRIBED", "HARD_BOUNCE", "COMPLAINT"}


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SuppressionService:
    async def is_suppressed(self, session: AsyncSession, *, channel: str, address_norm: str) -> Suppression | None:
        return await session.scalar(
            select(Suppression).where(
                Suppression.channel == channel.upper(),
                Suppression.address_norm == address_norm,
            )
        )

    async def add(
        self,
        session: AsyncSession,
        *,
        channel: str,
        address: str,
        reason: str,
        source: str = "manual",
        lead_id: uuid.UUID | None = None,
        notes: str | None = None,
        metadata: dict | None = None,
        created_by: uuid.UUID | None = None,
    ) -> Suppression:
        """Idempotent upsert; updating an existing row only widens evidence."""
        channel = channel.upper()
        if channel == "EMAIL":
            normalized = normalize_email(address)
            address_norm = normalized.normalized or address.strip().lower()
        elif channel == "WHATSAPP":
            normalized = normalize_phone(address)
            address_norm = normalized.normalized or address.strip()
        else:
            address_norm = address.strip().lower()
        if not address_norm:
            raise ValidationError("Cannot suppress an empty address")

        existing = await self.is_suppressed(session, channel=channel, address_norm=address_norm)
        if existing is not None:
            if notes and not existing.notes:
                existing.notes = notes[:1000]
            return existing
        row = Suppression(
            channel=channel,
            address=address.strip(),
            address_norm=address_norm,
            reason=reason.upper(),
            source=source,
            lead_id=lead_id,
            notes=(notes or None) and notes[:1000],
            metadata_json=metadata or {},
            created_by=created_by,
        )
        session.add(row)
        await session.flush()
        return row

    async def remove(
        self,
        session: AsyncSession,
        *,
        channel: str,
        address_norm: str,
        actor: uuid.UUID | None,
    ) -> bool:
        """Manual removal. TERMINAL reasons (unsubscribed/bounce/complaint)
        are refused — use the audited re-subscription flow instead (§14)."""
        row = await self.is_suppressed(session, channel=channel, address_norm=address_norm)
        if row is None:
            return False
        if row.reason in TERMINAL_REASONS:
            raise ValidationError(
                f"Suppression reason {row.reason} is terminal; use the compliant "
                "re-subscription flow with explicit consent evidence."
            )
        await session.delete(row)
        await session.flush()
        return True

    async def resubscribe(
        self,
        session: AsyncSession,
        *,
        channel: str,
        address_norm: str,
        evidence: dict,
        actor: uuid.UUID | None,
    ) -> bool:
        """Explicit, audited re-subscription with consent evidence (§14)."""
        row = await self.is_suppressed(session, channel=channel, address_norm=address_norm)
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        consent = await session.scalar(
            select(MarketingConsent).where(
                MarketingConsent.channel == channel.upper(),
                MarketingConsent.address_norm == address_norm,
            )
        )
        if consent is None:
            consent = MarketingConsent(
                channel=channel.upper(),
                address_norm=address_norm,
                opt_in_status="OPTED_IN",
                source="resubscribe",
                evidence=evidence,
            )
            session.add(consent)
        else:
            consent.opt_in_status = "OPTED_IN"
            consent.source = "resubscribe"
            consent.evidence = evidence
        await session.flush()
        return True

    async def list_suppressions(
        self,
        session: AsyncSession,
        *,
        channel: str | None = None,
        reason: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Suppression]:
        query = select(Suppression).order_by(Suppression.created_at.desc())
        if channel:
            query = query.where(Suppression.channel == channel.upper())
        if reason:
            query = query.where(Suppression.reason == reason.upper())
        query = query.limit(min(limit, 1000)).offset(max(offset, 0))
        return list((await session.scalars(query)).all())


class UnsubscribeService:
    """One-time opt-out tokens + the unsubscribe flow (§13, §14)."""

    def __init__(self, *, ttl_days: int, base_url: str) -> None:
        self.ttl_days = ttl_days
        self.base_url = base_url.rstrip("/")

    def build_url(self, raw_token: str) -> str:
        return f"{self.base_url}/unsubscribe/{raw_token}"

    async def issue_token(
        self,
        session: AsyncSession,
        *,
        channel: str,
        address: str,
        address_norm: str,
        lead_id: uuid.UUID | None,
        campaign_id: uuid.UUID | None,
        recipient_id: uuid.UUID | None,
    ) -> str:
        """Create a single-use token; the RAW value is returned exactly once
        (it goes into the email body) and only its hash is persisted (§51)."""
        raw = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        row = UnsubscribeToken(
            token_hash=_hash_token(raw),
            channel=channel.upper(),
            lead_id=lead_id,
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            address_norm=address_norm,
            expires_at=now + timedelta(days=self.ttl_days),
        )
        session.add(row)
        await session.flush()
        return raw

    async def resolve(self, session: AsyncSession, raw_token: str) -> UnsubscribeToken:
        """Resolve a raw token (constant-time compare via hash lookup)."""
        if not raw_token or len(raw_token) > 200:
            raise NotFoundError("Unsubscribe link is invalid")
        row = await session.scalar(
            select(UnsubscribeToken).where(UnsubscribeToken.token_hash == _hash_token(raw_token))
        )
        if row is None:
            raise NotFoundError("Unsubscribe link is invalid")
        now = datetime.now(timezone.utc)
        expires_at = row.expires_at
        if expires_at is not None:
            # SQLite returns naive UTC datetimes — normalize before comparing
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < now:
                raise ValidationError("This unsubscribe link has expired")
        used_at = row.used_at
        if used_at is not None:
            raise ValidationError("This unsubscribe link has already been used")
        return row

    async def confirm(
        self,
        session: AsyncSession,
        raw_token: str,
        *,
        ip: str | None = None,
    ) -> UnsubscribeToken:
        """Confirm opt-out: mark token used + create EMAIL suppression +
        OPTED_OUT consent evidence. Idempotency guard: used tokens raise."""
        token = await self.resolve(session, raw_token)
        now = datetime.now(timezone.utc)
        token.used_at = now
        token.used_ip = (ip or "")[:64]

        suppression = SuppressionService()
        address_norm = token.address_norm
        if not address_norm:
            # Tokens always carry the normalized address for EMAIL channel.
            raise ValidationError("Unsubscribe token has no bound address")
        await suppression.add(
            session,
            channel=token.channel,
            address=address_norm,
            reason="UNSUBSCRIBED",
            source="unsubscribe",
            lead_id=token.lead_id,
            metadata={"campaign_id": str(token.campaign_id) if token.campaign_id else None},
        )
        consent = await session.scalar(
            select(MarketingConsent).where(
                MarketingConsent.channel == token.channel,
                MarketingConsent.address_norm == address_norm,
            )
        )
        if consent is None:
            session.add(
                MarketingConsent(
                    channel=token.channel,
                    address_norm=address_norm,
                    lead_id=token.lead_id,
                    opt_in_status="OPTED_OUT",
                    source="unsubscribe",
                    evidence={"token_used_at": now.isoformat()},
                )
            )
        else:
            consent.opt_in_status = "OPTED_OUT"
            consent.source = "unsubscribe"
        await session.flush()
        return token
