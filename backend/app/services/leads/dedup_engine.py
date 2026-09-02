"""DuplicateDetectionService — workspace-level duplicate review engine (§7, §8).

Confidence ladder (stricter than the pipeline's):
    EXACT  — normalized email / phone / source-id identical
    HIGH   — normalized website host identical
    MEDIUM — business name + city (name_key), or business name + phone
    LOW    — fuzzy business name only (difflib ratio >= 0.85)

LOW-confidence candidates are recorded for human review ONLY — weak fuzzy
matching never merges or destroys anything automatically (§7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import DuplicateConfidence, LeadDuplicateCandidate
from app.models.scrape import Lead


@dataclass
class DuplicateMatch:
    lead: Lead
    confidence: DuplicateConfidence
    matched_on: str


def _canonical_pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) <= str(b) else (b, a)


def _name_ratio(a: str | None, b: str | None) -> bool:
    """Fuzzy-name similarity: SequenceMatcher ratio, with a containment rule
    so 'Alpha Industries' vs 'Alpha Industries Pvt Ltd' counts as LOW too."""
    if not a or not b:
        return False
    x, y = a.lower().strip(), b.lower().strip()
    if SequenceMatcher(None, x, y).ratio() >= 0.75:
        return True
    shorter, longer = (x, y) if len(x) <= len(y) else (y, x)
    return len(shorter) >= 10 and shorter in longer


class DuplicateDetectionService:
    FUZZY_THRESHOLD = 0.75
    MAX_PER_LEAD = 20

    async def find_duplicates(self, session: AsyncSession, lead: Lead) -> list[DuplicateMatch]:
        """All existing leads that duplicate `lead`, best confidence first."""
        results: dict[uuid.UUID, DuplicateMatch] = {}

        async def collect(where, confidence: DuplicateConfidence, matched_on: str):
            rows = (
                await session.scalars(
                    select(Lead)
                    .where(
                        Lead.id != lead.id,
                        Lead.merged_into_id.is_(None),
                        or_(where),
                    )
                    .limit(self.MAX_PER_LEAD)
                )
            ).all()
            for row in rows:
                if row.id not in results:
                    results[row.id] = DuplicateMatch(row, confidence, matched_on)

        exact = []
        if lead.email_norm:
            exact.append(Lead.email_norm == lead.email_norm)
        if lead.phone_norm:
            exact.append(Lead.phone_norm == lead.phone_norm)
        if lead.source_id:
            exact.append(Lead.source_id == lead.source_id)
        if exact:
            await collect(or_(*exact), DuplicateConfidence.EXACT, "email/phone/source_id")
        if lead.website_norm:
            await collect(
                Lead.website_norm == lead.website_norm, DuplicateConfidence.HIGH, "website"
            )
        if lead.name_key:
            await collect(Lead.name_key == lead.name_key, DuplicateConfidence.MEDIUM, "name+city")
        elif lead.business_name and lead.phone_norm:
            await collect(
                (Lead.business_name == lead.business_name) & (Lead.phone_norm == lead.phone_norm),
                DuplicateConfidence.MEDIUM,
                "name+phone",
            )

        # fuzzy name pass (LOW) — only for leads not already matched
        if lead.business_name:
            token = lead.business_name.strip().lower()[:12]
            if token:
                fuzzy_rows = (
                    await session.scalars(
                        select(Lead)
                        .where(
                            Lead.id != lead.id,
                            Lead.merged_into_id.is_(None),
                            Lead.business_name.isnot(None),
                            Lead.business_name.ilike(f"%{token}%"),
                        )
                        .limit(100)
                    )
                ).all()
                for row in fuzzy_rows:
                    if row.id in results:
                        continue
                    if _name_ratio(lead.business_name, row.business_name):
                        results[row.id] = DuplicateMatch(row, DuplicateConfidence.LOW, "fuzzy_name")

        ordered = sorted(
            results.values(),
            key=lambda m: (
                ["EXACT", "HIGH", "MEDIUM", "LOW"].index(m.confidence.value),
                -(m.lead.quality_score or 0),
            ),
        )
        return ordered[: self.MAX_PER_LEAD]

    async def record_candidate(
        self,
        session: AsyncSession,
        lead_a: uuid.UUID,
        lead_b: uuid.UUID,
        *,
        confidence: DuplicateConfidence,
        matched_on: str | None,
        origin: str = "scan",
        detected_by_job_id: uuid.UUID | None = None,
    ) -> LeadDuplicateCandidate | None:
        """Persist a review candidate (canonical pair order). Returns None when
        the pair is already known — resolved pairs are never resurrected."""
        a, b = _canonical_pair(lead_a, lead_b)
        # autoflush is off: flush so pairs created earlier in THIS transaction
        # (e.g. the reverse direction A→B then B→A inside one scan) are visible
        await session.flush()
        existing = await session.scalar(
            select(LeadDuplicateCandidate).where(
                LeadDuplicateCandidate.lead_a_id == a,
                LeadDuplicateCandidate.lead_b_id == b,
            )
        )
        if existing is not None:
            return None
        row = LeadDuplicateCandidate(
            lead_a_id=a,
            lead_b_id=b,
            # accept enum or plain string (callers may pass pre-mapped values)
            confidence=getattr(confidence, "value", confidence),
            matched_on=matched_on,
            origin=origin,
            detected_by_job_id=detected_by_job_id,
        )
        session.add(row)
        return row

    async def scan(
        self,
        session: AsyncSession,
        *,
        origin: str = "scan",
        detected_by_job_id: uuid.UUID | None = None,
        limit_leads: int = 5000,
        created_cb=None,
    ) -> int:
        """Full scan creating PENDING candidates. Chunked by offset; indexed
        lookups only (no cross join). Returns number of NEW candidates."""
        created = 0
        chunk = 200
        processed = 0
        while processed < limit_leads:
            leads = (
                await session.scalars(
                    select(Lead)
                    .where(Lead.merged_into_id.is_(None))
                    .order_by(Lead.created_at, Lead.id)
                    .offset(processed)
                    .limit(chunk)
                )
            ).all()
            if not leads:
                break
            for lead in leads:
                matches = await self.find_duplicates(session, lead)
                for match in matches:
                    if match.confidence is DuplicateConfidence.LOW and origin != "scan":
                        continue  # LOW only from explicit scans
                    row = await self.record_candidate(
                        session, lead.id, match.lead.id,
                        confidence=match.confidence,
                        matched_on=match.matched_on,
                        origin=origin,
                        detected_by_job_id=detected_by_job_id,
                    )
                    if row is not None:
                        created += 1
                processed += 1
                if created_cb and processed % 200 == 0:
                    await created_cb(processed, created)
            await session.commit()
        await session.commit()
        return created

    async def list_candidates(
        self,
        session: AsyncSession,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[tuple[LeadDuplicateCandidate, Lead, Lead]], int]:
        from sqlalchemy import func

        query = select(LeadDuplicateCandidate).order_by(
            LeadDuplicateCandidate.created_at.desc()
        )
        if status:
            query = query.where(LeadDuplicateCandidate.status == status)
        total = await session.scalar(
            select(func.count()).select_from(query.subquery())
        )
        rows = (
            await session.execute(
                query.offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        out = []
        for row in rows:
            lead_a = await session.get(Lead, row.lead_a_id)
            lead_b = await session.get(Lead, row.lead_b_id)
            if lead_a is None or lead_b is None:
                continue
            out.append((row, lead_a, lead_b))
        return out, int(total or 0)
