"""HTML parsing for the website actor (brief §26).

Pure functions: response HTML + base URL → structured fragments. No network,
no state — trivially unit-testable. Only PUBLICLY PRESENT data is extracted;
this module never attempts authentication, obfuscation decoding or any
evasion (forbidden by brief §17/§55).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.scrapers.core.netguard import normalize_url

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)
_PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{2,4}(?:[\s.\-]?\d{2,4}){1,4}"
)
_SOCIAL_PATTERNS = {
    "facebook": re.compile(r"https?://(?:www\.|m\.)?facebook\.com/[^\"'\s<>]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[^\"'\s<>]+", re.I),
    "twitter": re.compile(r"https?://(?:www\.)?twitter\.com/[^\"'\s<>]+", re.I),
    "x": re.compile(r"https?://(?:www\.)?x\.com/[^\"'\s<>]+", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in)/[^\"'\s<>]+", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/[^\"'\s<>]+", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/[^\"'\s<>]+", re.I),
}
# mailto:/tel: links are the most reliable contact signals on a page.
_MAILTO_RE = re.compile(r"^mailto:([^?]+)", re.I)
_TEL_RE = re.compile(r"^tel:([^?]+)", re.I)
_CRAWLABLE_TAGS = ("a",)
_SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".pdf", ".zip", ".gz", ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2",
    ".ttf", ".eot", ".dmg", ".exe", ".apk",
)


def page_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return soup.title.string.strip()[:300] or None
    return None


def meta_description(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", attrs={"name": "description"})
    if tag and tag.get("content"):
        return str(tag["content"]).strip()[:1000] or None
    return None


def visible_text(soup: BeautifulSoup, limit: int = 20000) -> str:
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return text[:limit]


def extract_emails(html: str, soup: BeautifulSoup) -> list[str]:
    """Publicly displayed emails: mailto: links first, then body text."""
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        match = _MAILTO_RE.match(str(a["href"]).strip())
        if match:
            email = match.group(1).strip().strip(".").lower()
            if _EMAIL_RE.fullmatch(email):
                found.append(email)
    for candidate in _EMAIL_RE.findall(html):
        email = candidate.strip(".").lower()
        # exclude common false positives from the visible text scan
        if email.endswith((".png", ".jpg", ".gif", ".webp", ".svg")):
            continue
        if email not in found:
            found.append(email)
    return _unique(found)[:25]


def extract_phones(soup: BeautifulSoup) -> list[str]:
    """Publicly displayed phones: tel: links first, then tel-like text."""
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        match = _TEL_RE.match(str(a["href"]).strip())
        if match:
            raw = match.group(1).strip()
            digits = re.sub(r"\D", "", raw)
            if 7 <= len(digits) <= 15 and raw not in found:
                found.append(raw)
    return _unique(found)[:10]


def extract_social_links(soup: BeautifulSoup) -> dict[str, str]:
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        for platform, pattern in _SOCIAL_PATTERNS.items():
            if platform in found:
                continue
            if pattern.match(href):
                found[platform] = href.split("?")[0]
    return found


def extract_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Absolute, normalized, crawlable links (same-site filtering is the
    crawler's decision — this only resolves and sanitizes)."""
    links: list[str] = []
    for tag in soup.find_all(_CRAWLABLE_TAGS, href=True):
        raw = str(tag["href"]).strip()
        if not raw or raw.startswith(("#", "javascript:", "data:", "tel:", "mailto:")):
            continue
        absolute = normalize_url(base_url, raw)
        path = urlsplit(absolute).path.lower()
        if path.endswith(_SKIP_EXTENSIONS):
            continue
        if absolute not in links:
            links.append(absolute)
    return links[:500]


def extract_address_hint(soup: BeautifulSoup) -> str | None:
    """Best-effort postal address from semantic tags (no guessing)."""
    for tag in soup.find_all("address"):
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
        if 10 <= len(text) <= 500:
            return text
    return None


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
