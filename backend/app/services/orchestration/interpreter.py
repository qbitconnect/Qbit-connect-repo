"""QBIT CONNECT — Task Interpreter (Brief §9, §10, §26).

Parses operator instructions and queries into a structured intent specification
with explicit target counts, entity types, geo-localities, and source constraints.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.orchestration.tool_registry import SOURCE_ALIASES


@dataclass
class InterpretedTask:
    raw_query: str
    intent: str
    keywords: str
    location: str | None = None
    target_count: int = 100
    target_entity: str = "business_lead"
    requested_source: str | None = None
    source_lock: bool = False
    requested_fields: list[str] = field(
        default_factory=lambda: [
            "business_name",
            "phone",
            "email",
            "website",
            "address",
            "city",
        ]
    )
    confidence: float = 1.0
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_COUNT_PATTERNS = [
    re.compile(r"\b(\d+[\d,]*)\s*(?:records|leads|contacts|businesses|suppliers|wholesalers|manufacturers|vendors|results|items|rows|stores|shops)\b", re.IGNORECASE),
    re.compile(r"\btarget\s*[:=]\s*(\d+[\d,]*)", re.IGNORECASE),
    re.compile(r"\b(\d+[\d,]*)\s*target\b", re.IGNORECASE),
    re.compile(r"\b(?:find|extract|get|pull|collect)\s+(\d+[\d,]*)\b", re.IGNORECASE),
]

_LOCATION_PATTERNS = [
    re.compile(r"\b(?:in|around|near|across|at)\s+([A-Za-z\s]+?)(?:\s+(?:using|from|via|with|\d+)|\s*$|[.,;])", re.IGNORECASE),
]

_SOURCE_EXPLICIT_PATTERNS = [
    re.compile(r"\b(?:using|from|via|on|source[:=])\s+([A-Za-z0-9_\-\s]+?)(?:\s*(?:only|locked|\.|$))", re.IGNORECASE),
]


class TaskInterpreter:
    """Deterministic, rule-based & semantic intent interpreter."""

    def interpret(
        self,
        query: str,
        *,
        forced_source: str | None = None,
        forced_source_lock: bool | None = None,
        target_count: int | None = None,
    ) -> InterpretedTask:
        raw = query.strip()
        extracted_source = forced_source
        extracted_count = target_count
        location = None
        keywords = raw

        # 1. Target count extraction
        if extracted_count is None:
            for pat in _COUNT_PATTERNS:
                m = pat.search(raw)
                if m:
                    raw_num = m.group(1).replace(",", "")
                    try:
                        extracted_count = int(raw_num)
                        break
                    except ValueError:
                        pass
        if extracted_count is None or extracted_count <= 0:
            extracted_count = 100

        # 2. Source extraction & Source Lock check
        source_locked = False
        if forced_source_lock is not None:
            source_locked = forced_source_lock
            if forced_source:
                extracted_source = forced_source
        elif forced_source:
            extracted_source = forced_source
            source_locked = True
        else:
            # Check for source mentions in prompt
            lower_query = raw.lower()
            for pat in _SOURCE_EXPLICIT_PATTERNS:
                m = pat.search(raw)
                if m:
                    cand = m.group(1).strip().lower()
                    if cand in SOURCE_ALIASES:
                        extracted_source = SOURCE_ALIASES[cand]
                        source_locked = True
                        break

            # Check direct keyword matches for known sources if not found yet
            if not extracted_source:
                for alias, canonical_id in SOURCE_ALIASES.items():
                    # match word boundaries
                    if re.search(r"\b" + re.escape(alias) + r"\b", lower_query):
                        extracted_source = canonical_id
                        source_locked = True
                        break

        # 3. Location extraction
        for pat in _LOCATION_PATTERNS:
            m = pat.search(raw)
            if m:
                cand_loc = m.group(1).strip()
                # Ensure location is not an alias
                if cand_loc.lower() not in SOURCE_ALIASES and len(cand_loc) > 1:
                    location = cand_loc.title()
                    break

        # 4. Clean keywords: strip out metadata commands
        clean_kw = raw
        clean_kw = re.sub(r"\b(?:find|extract|scrape|search|get|collect|pull)\s+", "", clean_kw, flags=re.IGNORECASE)
        clean_kw = re.sub(r"\b\d+[\d,]*\s*(?:records|leads|contacts|businesses|suppliers|results|items|rows|stores|shops)\b", "", clean_kw, flags=re.IGNORECASE)
        if location:
            clean_kw = re.sub(r"\b(?:in|around|near|across|at)\s+" + re.escape(location) + r"\b", "", clean_kw, flags=re.IGNORECASE)
        if extracted_source:
            clean_kw = re.sub(r"\b(?:using|from|via|on)\s+[A-Za-z0-9_\-\s]+\b", "", clean_kw, flags=re.IGNORECASE)
            clean_kw = re.sub(r"\b(?:source\s*locked|source\s*lock)\b", "", clean_kw, flags=re.IGNORECASE)
        clean_kw = " ".join(clean_kw.split()).strip(" ,.:;-\t")
        if not clean_kw:
            clean_kw = raw

        # 5. Entity & Intent classification
        lower_raw = raw.lower()
        if any(w in lower_raw for w in ("supplier", "wholesaler", "manufacturer", "moq", "product", "export", "b2b")):
            intent = "supplier_search"
            entity = "supplier"
        elif any(w in lower_raw for w in ("ad", "ads", "creative", "advertiser", "campaign")):
            intent = "ad_intelligence"
            entity = "ad_creative"
        elif any(w in lower_raw for w in ("instagram", "profile", "influencer", "hashtag", "follower")):
            intent = "social_intelligence"
            entity = "social_profile"
        elif any(w in lower_raw for w in ("linkedin", "employee", "headcount")):
            intent = "company_intelligence"
            entity = "company"
        elif any(w in lower_raw for w in ("email", "emails", "contact email")):
            intent = "email_discovery"
            entity = "business_email"
        elif any(w in lower_raw for w in ("sitemap", "robots.txt", "structured data")):
            intent = "sitemap_audit"
            entity = "sitemap_entry"
        else:
            intent = "business_discovery"
            entity = "business_lead"

        reasoning = (
            f"Interpreted intent='{intent}', entity='{entity}', target={extracted_count}, "
            f"location='{location or 'unspecified'}', keywords='{clean_kw}'"
        )
        if extracted_source:
            reasoning += f", source='{extracted_source}' (source_lock={source_locked})"

        return InterpretedTask(
            raw_query=raw,
            intent=intent,
            keywords=clean_kw or raw,
            location=location,
            target_count=extracted_count,
            target_entity=entity,
            requested_source=extracted_source,
            source_lock=source_locked,
            reasoning=reasoning,
        )
