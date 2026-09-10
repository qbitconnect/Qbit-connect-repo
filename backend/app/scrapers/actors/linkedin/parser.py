"""LinkedIn public page parser (spec §7.C) — logged-out surfaces only.

Layered: JSON-LD → OpenGraph → meta description. The og:description of a
public company page carries the tagline; og:title carries the display name.
Authwall responses are detected by the shared `looks_blocked` gate and the
caller fails the run honestly. No login, no cookies, no evasion (spec §36).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.scrapers.core.extraction import (
    dig,
    extract_emails,
    extract_jsonld,
    extract_meta,
    first_text,
    jsonld_by_type,
)

_EMPLOYEE_RE = re.compile(r"([\d,.]+)\s*employees", re.IGNORECASE)
_FOLLOWER_RE = re.compile(r"([\d,.]+)\s*followers", re.IGNORECASE)


def _count(text: str | None, pattern: re.Pattern) -> int | None:
    if not text:
        return None
    match = pattern.search(text)
    if not match:
        return None
    raw, unit = match.group(1), (match.group(2) if pattern.groups > 1 else None)
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    return int(value * {"m": 1_000_000, "k": 1_000}.get((unit or "").lower(), 1))


def parse_company(html: str, *, source_url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = extract_meta(soup)
    og_title = meta.get("og:title") or first_text(soup, ["h1"])
    if not og_title:
        return None
    # og:title is 'Name on LinkedIn: "Tagline"' or 'Name | LinkedIn'
    name = re.split(r"\s+on LinkedIn|\s*\|\s*", og_title)[0].strip() or og_title
    tagline = meta.get("og:description") or meta.get("description")
    jsonld = extract_jsonld(soup)
    org = jsonld_by_type(jsonld, "organization", "corporation", "localbusiness")
    ld = org[0] if org else {}
    website = dig(ld, "sameAs") if isinstance(dig(ld, "sameAs"), str) else None
    logo = dig(ld, "logo") if isinstance(dig(ld, "logo"), str) else None
    emails = extract_emails(tagline or "")
    return {
        "business_name": name[:300],
        "website": website or meta.get("og:url"),
        "email": emails[0] if emails else None,
        "source": "linkedin",
        "source_url": source_url,
        "metadata": {
            "record_type": "company",
            "platform": "linkedin",
            "tagline": (tagline or "")[:1000] or None,
            "employees_hint": _count(tagline, _EMPLOYEE_RE),
            "followers_hint": _count(tagline, _FOLLOWER_RE),
            "logo": logo,
            "industry": ld.get("industry") or None,
            "founded": ld.get("foundingDate") or None,
            "linkedin_url": source_url,
        },
    }


def parse_profile(html: str, *, source_url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = extract_meta(soup)
    og_title = meta.get("og:title")
    if not og_title:
        return None
    # 'Full Name on LinkedIn: "Headline"'
    name_match = re.match(r'^(.*?)\s+on LinkedIn', og_title)
    headline_match = re.search(r'“([^”]+)”', og_title) or re.search(r'"([^"]+)"', og_title)
    name = (name_match.group(1) if name_match else og_title).strip()
    headline = headline_match.group(1) if headline_match else None
    about = meta.get("og:description") or meta.get("description")
    return {
        "business_name": name[:300],
        "contact_name": name[:300],
        "source": "linkedin",
        "source_url": source_url,
        "metadata": {
            "record_type": "profile",
            "platform": "linkedin",
            "headline": (headline or "")[:500] or None,
            "about": (about or "")[:1000] or None,
            "linkedin_url": source_url,
        },
    }
