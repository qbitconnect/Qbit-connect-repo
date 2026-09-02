"""Lead field normalization + validation (Phase 4 §19, §20).

Reuses the proven Phase 3 key normalizers (services.scraping.lead_keys) so
imported, scraped and manually entered leads share ONE normalization path.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.services.scraping.lead_keys import (
    normalize_email,
    normalize_name_key,
    normalize_phone,
    normalize_website,
)

_WS = re.compile(r"\s+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

#: lead field -> max stored length (model columns)
FIELD_LIMITS = {
    "business_name": 300,
    "contact_name": 300,
    "first_name": 150,
    "last_name": 150,
    "email": 320,
    "phone": 40,
    "website": 500,
    "address": 500,
    "city": 150,
    "state": 150,
    "postal_code": 20,
    "country": 150,
    "category": 150,
    "industry": 150,
    "source": 100,
    "source_url": 1000,
    "source_id": 300,
}

TEXT_FIELDS = (
    "business_name",
    "contact_name",
    "first_name",
    "last_name",
    "address",
    "city",
    "state",
    "postal_code",
    "country",
    "category",
    "industry",
)


def clean_text(value, field: str) -> str | None:
    """Collapse whitespace, strip, enforce length cap. Returns None for empty."""
    if value is None:
        return None
    text = _WS.sub(" ", str(value)).strip()
    if not text:
        return None
    limit = FIELD_LIMITS.get(field, 300)
    return text[:limit]


def valid_email(value: str | None) -> bool:
    if not value:
        return False
    return bool(_EMAIL_RE.match(value))


def valid_website(value: str | None) -> bool:
    if not value:
        return False
    raw = str(value).strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlsplit(raw).hostname
    except ValueError:
        return False
    return bool(host and "." in host)


def normalize_lead_payload(payload: dict) -> tuple[dict, dict[str, str]]:
    """Validate + normalize a user/import-supplied lead payload.

    Returns (clean_fields, errors). Invalid email/phone/website values are
    FIELD ERRORS (rejected), not silently dropped — imports use this to build
    the rejected-rows report; the API returns 422.
    """
    clean: dict = {}
    errors: dict[str, str] = {}

    for field in TEXT_FIELDS:
        if field in payload:
            clean[field] = clean_text(payload.get(field), field)

    if "contact_name" in payload and "first_name" not in clean and payload.get("first_name") is None:
        # convenience: split a full contact name when only that was provided
        parts = (clean.get("contact_name") or "").split(" ", 1)
        if len(parts) == 2 and payload.get("split_contact_name", True):
            clean["first_name"], clean["last_name"] = parts[0][:150], parts[1][:150]

    if "email" in payload:
        raw = clean_text(payload.get("email"), "email")
        if raw:
            normalized = normalize_email(raw)
            if normalized and valid_email(normalized):
                clean["email"] = raw  # display form; email_norm is the key
                clean["email_norm"] = normalized
            else:
                errors["email"] = f"Invalid email: {raw[:80]}"
        else:
            clean["email"] = None
            clean["email_norm"] = None

    if "phone" in payload:
        raw = clean_text(payload.get("phone"), "phone")
        if raw:
            normalized = normalize_phone(raw)
            if normalized:
                clean["phone"] = raw
                clean["phone_norm"] = normalized
            else:
                errors["phone"] = f"Invalid phone (need >=7 digits): {raw[:40]}"
        else:
            clean["phone"] = None
            clean["phone_norm"] = None

    if "website" in payload:
        raw = clean_text(payload.get("website"), "website")
        if raw:
            if valid_website(raw):
                clean["website"] = raw
                clean["website_norm"] = normalize_website(raw)
            else:
                errors["website"] = f"Invalid URL: {raw[:80]}"
        else:
            clean["website"] = None
            clean["website_norm"] = None

    if clean.get("business_name") or clean.get("contact_name") or clean.get("city"):
        clean["name_key"] = normalize_name_key(
            clean.get("business_name"), clean.get("city"), clean.get("country")
        )

    for dangerous in ("email_norm", "phone_norm", "website_norm", "name_key"):
        if dangerous in clean and clean[dangerous] is None and dangerous not in errors:
            # keep the None so stale keys get cleared on update
            pass

    return clean, errors
