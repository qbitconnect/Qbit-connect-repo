"""Lead normalizer (brief §9, doc 08 §5).

Maps any actor's raw item to the canonical lead shape:
- whitespace/title-case cleanup, E.164-style phone digits, email lowercase,
  website host normalization
- unknown/source-specific keys preserved under `metadata`
- output validated against the normalized-lead schema; invalid records are
  rejected BEFORE dedup/storage (brief §20: invalid records never become leads)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.services.scraping.lead_keys import normalize_email, normalize_name_key, normalize_phone, normalize_website

CANONICAL_FIELDS = (
    "lead_id",
    "business_name",
    "contact_name",
    "phone",
    "email",
    "website",
    "address",
    "city",
    "state",
    "country",
    "category",
    "source",
    "source_url",
    "rating",
    "review_count",
    "social_links",
    "metadata",
    "scraped_at",
)


class NormalizedLead(BaseModel):
    """Validated, canonical lead item (brief §9)."""

    lead_id: str | None = None
    business_name: str | None = Field(None, max_length=300)
    contact_name: str | None = Field(None, max_length=300)
    phone: str | None = Field(None, max_length=40)
    email: str | None = Field(None, max_length=320)
    website: str | None = Field(None, max_length=500)
    address: str | None = Field(None, max_length=500)
    city: str | None = Field(None, max_length=150)
    state: str | None = Field(None, max_length=150)
    country: str | None = Field(None, max_length=150)
    category: str | None = Field(None, max_length=150)
    source: str | None = Field(None, max_length=100)
    source_url: str | None = Field(None, max_length=1000)
    rating: float | None = Field(None, ge=0, le=5)
    review_count: int | None = Field(None, ge=0)
    social_links: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    scraped_at: datetime | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        return normalize_email(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalize_phone(v)

    @field_validator("website")
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        return normalize_website(v)

    @field_validator("business_name", "contact_name", "city", "state", "country", "category", "address")
    @classmethod
    def _collapse_ws(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = re.sub(r"\s+", " ", str(v)).strip()
        return cleaned[:300] or None


_WS = re.compile(r"\s+")
_PHONE_KEEP = re.compile(r"[+\d]")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_item(item: dict, *, source: str) -> dict | None:
    """Validate + canonicalize one raw item.

    Returns the normalized dict, or None when the item is invalid
    (missing both name and any contact key). Never raises for dirty data —
    failures are surfaced by the pipeline counter.
    """
    if not isinstance(item, dict):
        return None
    payload: dict[str, Any] = {}
    metadata = dict(item.get("metadata") or {})
    for key, value in item.items():
        if key in CANONICAL_FIELDS and key not in ("metadata", "scraped_at"):
            payload[key] = value
        elif key not in CANONICAL_FIELDS and key != "lead_id":
            metadata.setdefault(key, value)

    if payload.get("email") and not _EMAIL_RE.match(str(payload["email"])):
        # Salvage: a malformed email is metadata, not a lead email field.
        metadata.setdefault("unparsed", {})["email"] = payload.pop("email")
    if payload.get("phone"):
        digit_count = len(re.sub(r"\D", "", str(payload["phone"])))
        if digit_count < 7:
            metadata.setdefault("unparsed", {})["phone"] = payload.pop("phone")

    has_contact = any(
        payload.get(k) for k in ("email", "phone", "website", "address")
    )
    if not payload.get("business_name") and not has_contact and not payload.get("contact_name"):
        return None
    if not has_contact and not payload.get("website"):
        # A name-only record with no way to identify/contact the business is
        # not useful — keep it only when a source_url proves provenance.
        if not payload.get("source_url"):
            return None

    payload["source"] = payload.get("source") or source
    payload["metadata"] = metadata
    payload["scraped_at"] = payload.get("scraped_at") or datetime.now(timezone.utc)
    normalized = NormalizedLead(**{k: v for k, v in payload.items() if k in CANONICAL_FIELDS})
    data = normalized.model_dump(mode="json")
    # scraped_at must remain a real datetime for the DB columns; the JSONL
    # writers serialize it via json.dumps(default=str).
    data["scraped_at"] = normalized.scraped_at
    # derived dedup keys ride along for the pipeline (stored on the lead row)
    data["email_norm"] = normalize_email(data.get("email"))
    data["phone_norm"] = normalize_phone(data.get("phone"))
    data["website_norm"] = normalize_website(data.get("website"))
    data["name_key"] = normalize_name_key(
        data.get("business_name"), data.get("city"), data.get("country")
    )
    return data
