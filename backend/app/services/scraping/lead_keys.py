"""Normalized key helpers for deduplication (brief §21).

Keys are stable, lowercase, punctuation-insensitive:
- email   → lowercase trimmed
- phone   → digits with optional leading + (>=7 digits required)
- website → lowercase bare host (www. stripped)
- name_key → normalized business_name + "|" + city + "|" + country
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_NON_DIGIT = re.compile(r"[^\d+]")
_WS = re.compile(r"\s+")


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    value = str(email).strip().lower()
    if "@" not in value or len(value) > 320:
        return None
    return value


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    raw = str(phone).strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7 or len(digits) > 15:
        return None
    return ("+" + digits) if plus else digits


def normalize_website(website: str | None) -> str | None:
    if not website:
        return None
    value = str(website).strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def normalize_name_key(
    business_name: str | None, city: str | None = None, country: str | None = None
) -> str | None:
    if not business_name:
        return None
    name = _WS.sub(" ", str(business_name)).strip().lower()
    name = re.sub(r"[^\w\s&'-]", "", name)
    parts = [name]
    if city:
        parts.append(_WS.sub(" ", str(city)).strip().lower())
    if country:
        parts.append(_WS.sub(" ", str(country)).strip().lower())
    key = "|".join(p for p in parts if p)
    return key[:500] or None
