"""Deduplication engine (brief §21).

Matching keys and confidence:
    exact email match            → HIGH
    exact phone match            → HIGH
    exact website host match     → HIGH
    business name + city/country → MEDIUM
    fuzzy name only              → LOW (not used for auto-merge)

Policy (configurable per job, default `auto`):
- HIGH confidence  → the new item UPDATES the existing lead (fills empty
  fields, merges metadata, bumps seen_count). Counted as duplicate.
- MEDIUM confidence → information-preserving default: the item is inserted as
  a NEW lead with `metadata.possible_duplicate_of` set — low/medium confidence
  never destroys information. In `strict` policy MEDIUM matches are treated as
  duplicates (updated, not inserted).
- LOW / no match → insert.

Lookups are batched per pipeline batch: one query per key-column per batch.
A bounded in-memory LRU of seen keys catches repeats inside long jobs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scrape import Lead


class MatchConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    NONE = "NONE"


@dataclass
class MatchResult:
    lead_id: uuid.UUID | None
    confidence: MatchConfidence
    matched_on: str | None = None


@dataclass
class DedupStats:
    high: int = 0
    medium: int = 0
    misses: int = 0
    seen_keys: set = field(default_factory=set)


class Deduplicator:
    def __init__(self, *, policy: str = "auto", lru_size: int = 20000) -> None:
        self.policy = policy  # "auto" | "strict"
        self.stats = DedupStats()
        self._lru: dict[str, MatchResult] = {}
        self._lru_size = lru_size

    # ------------------------------------------------------------ batched
    async def find_match(self, session: AsyncSession, item: dict) -> MatchResult:
        """Find an existing lead matching `item` (batched-lookup friendly).

        Flushes pending pipeline inserts first (the API session factory runs
        with autoflush=False) so items of the SAME batch are matchable —
        otherwise duplicates inside one batch would all be inserted.
        """
        await session.flush()
        keys = self._keys_of(item)
        cached = self._cache_get(keys)
        if cached is not None:
            return cached

        candidates: list[uuid.UUID] = []
        matched_on: str | None = None
        confidence = MatchConfidence.NONE

        or_filters = []
        if keys["email"]:
            or_filters.append(Lead.email_norm == keys["email"])
        if keys["phone"]:
            or_filters.append(Lead.phone_norm == keys["phone"])
        if keys["website"]:
            or_filters.append(Lead.website_norm == keys["website"])
        if or_filters:
            row = await session.scalar(
                select(Lead.id).where(or_(*or_filters)).limit(1)
            )
            if row:
                candidates.append(row)
                confidence = MatchConfidence.HIGH
                if keys["email"] and row is not None:
                    matched_on = "email"
                elif keys["phone"]:
                    matched_on = "phone"
                else:
                    matched_on = "website"

        if not candidates and keys["name_key"]:
            row = await session.scalar(
                select(Lead.id).where(Lead.name_key == keys["name_key"]).limit(1)
            )
            if row:
                candidates.append(row)
                confidence = MatchConfidence.MEDIUM
                matched_on = "name+location"

        if not candidates and keys["email"] is None and keys["phone"] is None and keys["website"] is None and keys["name_key"]:
            # fuzzy name-only → LOW confidence: reported, never auto-merged
            like = f"%{keys['name_key'].split('|')[0][:60]}%"
            row = await session.scalar(
                select(Lead.id).where(Lead.business_name.ilike(like)).limit(1)
            )
            if row:
                result = MatchResult(lead_id=row, confidence=MatchConfidence.NONE, matched_on="fuzzy_name")
                self._cache_put(keys, result)
                return result

        if candidates:
            result = MatchResult(lead_id=candidates[0], confidence=confidence, matched_on=matched_on)
            # cache MATCHES only: caching misses would poison the LRU for items
            # that are duplicates of records inserted later in the same batch
            self._cache_put(keys, result)
        else:
            result = MatchResult(lead_id=None, confidence=MatchConfidence.NONE)
            self.stats.misses += 1
        return result

    def should_merge(self, match: MatchResult) -> bool:
        """Whether a match results in an update (True) or a new insert (False)."""
        if match.confidence is MatchConfidence.HIGH:
            return True
        if match.confidence is MatchConfidence.MEDIUM:
            return self.policy == "strict"
        return False

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _keys_of(item: dict) -> dict[str, str | None]:
        return {
            "email": item.get("email_norm") or None,
            "phone": item.get("phone_norm") or None,
            "website": item.get("website_norm") or None,
            "name_key": item.get("name_key") or None,
        }

    def _cache_get(self, keys: dict[str, str | None]) -> MatchResult | None:
        for value in keys.values():
            if value and value in self._lru:
                return self._lru[value]
        return None

    def _cache_put(self, keys: dict[str, str | None], result: MatchResult) -> None:
        for value in keys.values():
            if not value:
                continue
            if len(self._lru) >= self._lru_size:
                self._lru.pop(next(iter(self._lru)))
            self._lru[value] = result

    def note_decision(self, match: MatchResult, merged: bool) -> None:
        if match.confidence is MatchConfidence.HIGH:
            self.stats.high += 1
        elif match.confidence is MatchConfidence.MEDIUM:
            self.stats.medium += 1
        if merged:
            self._remember_merged(match)

    def _remember_merged(self, match: MatchResult) -> None:
        # cached MatchResult already routes future repeats to the same lead
        return
