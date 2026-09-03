"""Unsubscribe architecture (Phase 7 §12, §13, §14, §51).

    Token (email link)
     ↓ sha256
    EmailUnsubscribeToken lookup (UNIQUE token_hash)
     ↓ resolve recipient
    Confirm opt-out  (public page — NO login)
     ↓
    OptOutRecord + SuppressionEntry(channel=EMAIL, reason=UNSUBSCRIBED)
     ↓
    future campaigns → eligibility → SUPPRESSED/UNSUBSCRIBED → SKIPPED

Token rules (§13, §51):
- cryptographically secure: secrets.token_urlsafe(32) — 256 bits of entropy
- the DATABASE stores ONLY the SHA-256 hash; a leaked database cannot be used
  to unsubscribe victims or forge links
- nothing predictable is encoded in the token (no lead_id, email, campaign_id)
- unsubscribe NEVER requires login; the endpoint is rate-limited against abuse
- opt-out evidence is append-once: re-visiting the link shows the same
  confirmation and never re-enables the address (§14: no silent reactivation)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.email import EmailUnsubscribeToken
from app.services.marketing.suppression import SuppressionService

logger = get_logger("qbit.marketing.unsubscribe")

TOKEN_TTL_DAYS = 365  # §13: expiration/rotation strategy — generous but finite


def generate_token() -> str:
    """Raw token — lives ONLY in the email link, never in the database."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """SHA-256 hex of the raw token (§51). Constant-shape, unkeyed."""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def token_matches(raw: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(raw), token_hash)


class UnsubscribeService:
    # ------------------------------------------------------------------ issue
    async def issue_token(
        self, session: AsyncSession, *,
        address: str, campaign_id: uuid.UUID | None,
        recipient_id: uuid.UUID | None, lead_id: uuid.UUID | None = None,
    ) -> tuple[str, EmailUnsubscribeToken]:
        """Create (or reuse) the token row for one campaign recipient and
        return (RAW token, row). The raw token is returned exactly once —
        it is never persisted, never logged (§57)."""
        raw = generate_token()
        row = EmailUnsubscribeToken(
            token_hash=hash_token(raw),
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            lead_id=lead_id,
            address=(address or "")[:320].lower(),
            channel="EMAIL",
            expires_at=datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return raw, row

    # ---------------------------------------------------------------- resolve
    async def resolve(
        self, session: AsyncSession, *, raw_token: str,
    ) -> EmailUnsubscribeToken | None:
        """Token → row (None when unknown/expired). Does NOT consume."""
        if not raw_token or len(raw_token) > 512:
            return None
        row = (await session.execute(
            select(EmailUnsubscribeToken).where(
                EmailUnsubscribeToken.token_hash == hash_token(raw_token)
            ).limit(1)
        )).scalars().first()
        if row is None:
            return None
        if row.expires_at is not None:
            # SQLite round-trips naive datetimes; compare in a consistent shape
            expires = row.expires_at
            if expires.tzinfo is None:
                from datetime import timezone as _tz

                expires = expires.replace(tzinfo=_tz.utc)
            if expires < datetime.now(timezone.utc):
                return None
        return row

    # --------------------------------------------------------------- opt out
    async def confirm_opt_out(
        self, session: AsyncSession, row: EmailUnsubscribeToken, *,
        source: str = "unsubscribe_link",
    ) -> bool:
        """Resolve → persist suppression (§13 flow step 3–4).

        Idempotent: an already-consumed token records nothing new but keeps
        returning True so the confirmation page always tells the truth.
        """
        if row.consumed_at is None:
            row.consumed_at = datetime.now(timezone.utc)
            await session.commit()
        await SuppressionService().record_opt_out(
            session, channel="EMAIL", address=row.address,
            reason="UNSUBSCRIBED", source=source, lead_id=row.lead_id,
        )
        logger.info(
            "email_unsubscribed",
            extra={"extra_fields": {"token_row": str(row.id),
                                    "campaign_id": str(row.campaign_id) if row.campaign_id else None}},
        )
        return True
