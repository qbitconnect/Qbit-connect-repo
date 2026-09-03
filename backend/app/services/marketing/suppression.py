"""Suppression + opt-out engine (Phase 5 §14, §15).

Suppressed contacts must NEVER enter the send queue. This module owns:
- the global suppression list (EMAIL/PHONE/LEAD/CHANNEL + reason)
- opt-out (unsubscribe) records — evidence, never silently re-enabled
- the `is_suppressed()` check used by the eligibility engine

All lookups are batched `IN` queries — the eligibility engine passes the
whole batch's addresses at once, never one query per lead.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.models.marketing import OptOutRecord, SuppressionEntry, SuppressionReason, SuppressionType
from app.services.scraping.lead_keys import normalize_email, normalize_phone

VALID_TYPES = tuple(t.value for t in SuppressionType)
VALID_REASONS = tuple(r.value for r in SuppressionReason)


def channel_key(channel: str | None) -> str:
    return (channel or "").upper() or ""


def normalize_address(entry_type: str, address: str) -> str:
    value = (address or "").strip()
    if not value:
        raise ValidationError("Suppression address must not be empty")
    if entry_type == SuppressionType.EMAIL:
        return (normalize_email(value) or value.lower())[:320]
    if entry_type == SuppressionType.PHONE:
        return (normalize_phone(value) or value)[:320]
    return value[:320]


class SuppressionService:
    # ------------------------------------------------------------------ add
    async def add(
        self, session: AsyncSession, *,
        entry_type: str, address: str, reason: str,
        channel: str | None = None, source: str | None = None,
        lead_id: uuid.UUID | None = None,
        created_by: uuid.UUID | None = None,
    ) -> SuppressionEntry:
        entry_type = (entry_type or "").upper()
        if entry_type not in VALID_TYPES:
            raise ValidationError(f"type must be one of {', '.join(VALID_TYPES)}")
        reason = (reason or SuppressionReason.MANUAL).upper()
        if reason not in VALID_REASONS:
            raise ValidationError(f"reason must be one of {', '.join(VALID_REASONS)}")
        if channel is not None:
            channel = channel.upper()
            if channel not in ("WHATSAPP", "EMAIL", "SMS"):
                raise ValidationError("channel must be WHATSAPP, EMAIL or SMS")
        normalized = normalize_address(entry_type, address)
        existing = await session.scalar(
            select(SuppressionEntry).where(
                SuppressionEntry.type == entry_type,
                SuppressionEntry.address == normalized,
                SuppressionEntry.channel_key == channel_key(channel),
            )
        )
        if existing is not None:
            return existing  # idempotent add — same entry, no duplicates
        entry = SuppressionEntry(
            type=entry_type, address=normalized, channel=channel,
            channel_key=channel_key(channel), reason=reason, source=source,
            lead_id=lead_id, created_by=created_by,
        )
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        return entry

    async def record_opt_out(
        self, session: AsyncSession, *,
        channel: str, address: str, reason: str = "UNSUBSCRIBED",
        source: str | None = None, lead_id: uuid.UUID | None = None,
    ) -> OptOutRecord:
        channel = (channel or "").upper()
        if channel not in ("WHATSAPP", "EMAIL", "SMS"):
            raise ValidationError("channel must be WHATSAPP, EMAIL or SMS")
        normalized = normalize_address(
            SuppressionType.EMAIL if channel == "EMAIL" else SuppressionType.PHONE,
            address,
        )
        existing = await session.scalar(
            select(OptOutRecord).where(
                OptOutRecord.channel_key == channel_key(channel),
                OptOutRecord.address == normalized,
            )
        )
        if existing is not None:
            return existing  # opt-outs are append-once evidence
        record = OptOutRecord(
            channel=channel, channel_key=channel_key(channel),
            address=normalized, reason=(reason or "UNSUBSCRIBED").upper(),
            source=source, lead_id=lead_id,
        )
        session.add(record)
        # an opt-out ALWAYS creates the matching suppression entry (§14/§15)
        entry_type = SuppressionType.EMAIL if channel == "EMAIL" else SuppressionType.PHONE
        session.add(SuppressionEntry(
            type=entry_type, address=normalized, channel=channel,
            channel_key=channel_key(channel), reason=SuppressionReason.UNSUBSCRIBED,
            source=source or "opt-out", lead_id=lead_id,
        ))
        await session.commit()
        await session.refresh(record)
        return record

    # ----------------------------------------------------------------- list
    async def list_entries(
        self, session: AsyncSession, *, entry_type: str | None = None,
        channel: str | None = None, search: str | None = None,
        page: int = 1, page_size: int = 50,
    ) -> tuple[list[SuppressionEntry], int]:
        query = select(SuppressionEntry)
        if entry_type:
            query = query.where(SuppressionEntry.type == entry_type.upper())
        if channel:
            query = query.where(SuppressionEntry.channel_key == channel_key(channel))
        if search:
            query = query.where(SuppressionEntry.address.ilike(f"%{search[:100]}%"))
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(SuppressionEntry.created_at.desc())
            .offset(max(0, page - 1) * page_size).limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def list_opt_outs(
        self, session: AsyncSession, *, channel: str | None = None,
        page: int = 1, page_size: int = 50,
    ) -> tuple[list[OptOutRecord], int]:
        query = select(OptOutRecord)
        if channel:
            query = query.where(OptOutRecord.channel_key == channel_key(channel))
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(OptOutRecord.created_at.desc())
            .offset(max(0, page - 1) * page_size).limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def remove(self, session: AsyncSession, entry_id: uuid.UUID) -> None:
        """Removing a suppression entry is an explicit administrative action.
        Opt-out RECORDS are never deleted (evidence); if an opt-out exists for
        the same address/channel the removal is refused (never silently
        re-enable an opted-out recipient, §15)."""
        entry = await session.get(SuppressionEntry, entry_id)
        if entry is None:
            raise NotFoundError("Suppression entry not found")
        if entry.reason == SuppressionReason.UNSUBSCRIBED:
            opt_out = await session.scalar(
                select(OptOutRecord).where(
                    OptOutRecord.channel_key == entry.channel_key,
                    OptOutRecord.address == entry.address,
                )
            )
            if opt_out is not None:
                raise ValidationError(
                    "Address has an opt-out record; it cannot be removed from suppression"
                )
        await session.delete(entry)
        await session.commit()

    # ---------------------------------------------------------------- check
    async def is_suppressed(
        self, session: AsyncSession, *,
        channel: str, email: str | None = None, phone: str | None = None,
        lead_id: uuid.UUID | None = None,
    ) -> tuple[bool, str | None]:
        """Single-recipient check (batched underneath). Returns (suppressed, reason)."""
        results = await self.check_batch(
            session, channel=channel,
            emails=[email] if email else [], phones=[phone] if phone else [],
            lead_ids=[lead_id] if lead_id else [],
        )
        suppressed_map = results["suppressed"]
        if lead_id is not None:
            return suppressed_map.get(f"lead:{lead_id}", (False, None))
        key_kind = "email" if email else "phone"
        raw = email if email else phone
        try:
            entry_type = SuppressionType.EMAIL if email else SuppressionType.PHONE
            key = f"{key_kind}:{normalize_address(entry_type, raw)}"
        except ValidationError:
            key = f"{key_kind}:{raw or ''}"
        return suppressed_map.get(key, (False, None))

    async def check_batch(
        self, session: AsyncSession, *, channel: str,
        emails: list[str], phones: list[str], lead_ids: list[uuid.UUID],
    ) -> dict:
        """Batched suppression check for a whole audience chunk.

        Returns {"suppressed": {key: (bool, reason)}} where key is
        email:<addr> / phone:<addr> / lead:<uuid>.
        """
        out: dict[str, tuple[bool, str | None]] = {}
        norm_emails = [normalize_address("EMAIL", e) for e in emails if e]
        norm_phones = [normalize_address("PHONE", p) for p in phones if p]
        address_list = norm_emails + norm_phones

        address_hits: set[str] = set()
        if address_list:
            rows = await session.execute(
                select(SuppressionEntry.address).where(
                    SuppressionEntry.address.in_(address_list),
                    or_(
                        SuppressionEntry.channel_key == channel_key(channel),
                        SuppressionEntry.channel_key == "",  # channel-wide entry
                    ),
                )
            )
            address_hits = {row for row in rows.scalars().all()}
        lead_hit_ids: set[uuid.UUID] = set()
        if lead_ids:
            rows = await session.execute(
                select(SuppressionEntry.address).where(
                    SuppressionEntry.type == SuppressionType.LEAD,
                    SuppressionEntry.address.in_([str(l) for l in lead_ids]),
                )
            )
            lead_hit_ids = {uuid.UUID(row) for row in rows.scalars().all()}

        # opt-out evidence ALSO suppresses (independent of suppression rows)
        opt_hits: set[str] = set()
        if address_list:
            rows = await session.execute(
                select(OptOutRecord.address).where(
                    OptOutRecord.address.in_(address_list),
                    or_(
                        OptOutRecord.channel_key == channel_key(channel),
                        OptOutRecord.channel_key == "",
                    ),
                )
            )
            opt_hits = {row for row in rows.scalars().all()}

        for email in norm_emails:
            out[f"email:{email}"] = (
                (email in address_hits or email in opt_hits),
                "UNSUBSCRIBED" if email in opt_hits else "SUPPRESSED",
            )
        for phone in norm_phones:
            out[f"phone:{phone}"] = (
                (phone in address_hits or phone in opt_hits),
                "UNSUBSCRIBED" if phone in opt_hits else "SUPPRESSED",
            )
        for lead_id in lead_ids:
            out[f"lead:{lead_id}"] = (
                lead_id in lead_hit_ids, "SUPPRESSED",
            )
        return {"suppressed": out}
