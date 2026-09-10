"""Meta Ads Library parser — layered extraction over public Ad Library HTML.

The Ad Library renders ads from embedded JSON (a large `window.__initialData`
style blob) on the public logged-out page. Strategy (spec §27):
  1. embedded JSON — walk blobs for ad-card-shaped dicts (id + body/creative)
  2. JSON-LD / OpenGraph fallback — page-level summary
  3. nothing found → caller reports honestly (block or parser-empty)

Also computes a STABLE content hash per ad so the platform's dataset-level
change detection (new / unchanged / modified / stopped / resumed) is
deterministic across runs (spec §7.B 'AD CHANGE DETECTION').
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.scrapers.core.extraction import (
    dig,
    extract_jsonld,
    extract_meta,
    looks_blocked,
)

_AD_ID_KEYS = ("ad_archive_id", "adDeliveryInfoID", "id", "ad_id")
_BODY_KEYS = ("body", "ad_creative_link_titles", "page_body", "text")
_CTA_KEYS = ("cta_text", "call_to_action", "link_title")
_LANDING_KEYS = ("link_url", "landing_url", "ad_creative_link_url")


def build_search_url(*, keyword: str | None, country: str, active_status: str, page_name: str | None = None) -> str:
    """Deterministic public Ad Library URL (logged-out web surface)."""
    params = {
        "active_status": active_status or "all",
        "country": (country or "IN").upper(),
        "q": keyword or page_name or "",
        "media_type": "all",
    }
    query = urlencode({k: v for k, v in params.items() if v})
    return urlunsplit(("https", "www.facebook.com", "/ads/library/", query, ""))


def ad_content_hash(ad: dict) -> str:
    """Stable hash over the ad's CREATIVE content (copy/cta/landing/media)."""
    material = json.dumps(
        {
            "body": ad.get("body"),
            "cta": ad.get("cta"),
            "landing_url": ad.get("landing_url"),
            "image_urls": ad.get("image_urls") or [],
            "platforms": ad.get("platforms") or [],
        },
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _first(d: dict, keys: tuple[str, ...]):
    for key in keys:
        value = dig(d, *key.split("."))
        if value:
            return value
    return None


def _walk_ad_cards(obj, out: list[dict]) -> None:
    """Recursively collect dicts shaped like ad cards (bounded)."""
    if len(out) >= 500 or obj is None:
        return
    if isinstance(obj, dict):
        ad_id = _first(obj, _AD_ID_KEYS)
        if isinstance(ad_id, (str, int)) and str(ad_id).strip():
            body = _first(obj, _BODY_KEYS)
            if body or obj.get("collatedButtons") or obj.get("snapshot"):
                out.append(obj)
        for value in obj.values():
            _walk_ad_cards(value, out)
    elif isinstance(obj, list):
        for item in obj[:200]:
            _walk_ad_cards(item, out)


def _normalize_card(card: dict) -> dict | None:
    ad_id = _first(card, _AD_ID_KEYS)
    if ad_id is None:
        return None
    snapshot = card.get("snapshot") if isinstance(card.get("snapshot"), dict) else card
    body = _first(snapshot, _BODY_KEYS)
    cta = _first(snapshot, _CTA_KEYS)
    landing = _first(snapshot, _LANDING_KEYS)
    images: list[str] = []
    videos: list[str] = []
    for key, bucket in (("original_image_url", images), ("video_preview_image_url", videos), ("video_hd_url", videos)):
        value = dig(snapshot, *key.split("."))
        if isinstance(value, str) and value.startswith("http"):
            bucket.append(value)
    extra_images = snapshot.get("extra_images") if isinstance(snapshot.get("extra_images"), list) else []
    for item in extra_images[:10]:
        url = item.get("url") if isinstance(item, dict) else None
        if url:
            images.append(url)
    page = snapshot.get("page") if isinstance(snapshot.get("page"), dict) else card.get("page")
    page_name = dig(page, "name") if page else None
    platforms = dig(card, "platforms") or dig(snapshot, "platforms")
    if isinstance(platforms, list):
        platforms = [str(p) for p in platforms][:10]
    return {
        "ad_id": str(ad_id)[:100],
        "page_name": (page_name or None),
        "body": (str(body)[:2000] if body else None),
        "cta": (str(cta)[:100] if cta else None),
        "landing_url": (str(landing)[:500] if landing else None),
        "image_urls": images[:10],
        "video_urls": videos[:10],
        "platforms": platforms or [],
        "start_date": snapshot.get("startDate") or snapshot.get("start_date"),
        "end_date": snapshot.get("endDate") or snapshot.get("end_date"),
        "archive_url": (
            f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else None
        ),
    }


def parse_ads(html: str, *, cap: int) -> tuple[list[dict], str | None]:
    """Returns (ads, block_marker). ads=[] + block=None means parser-empty."""
    block = looks_blocked(html)
    if block:
        return [], block
    soup = BeautifulSoup(html, "html.parser")
    embedded = _embedded_blobs(soup)
    cards: list[dict] = []
    for blob in embedded:
        _walk_ad_cards(blob, cards)
        if len(cards) >= cap:
            break
    ads: list[dict] = []
    seen: set[str] = set()
    for card in cards:
        normalized = _normalize_card(card)
        if normalized is None or normalized["ad_id"] in seen:
            continue
        seen.add(normalized["ad_id"])
        normalized["content_hash"] = ad_content_hash(normalized)
        ads.append(normalized)
        if len(ads) >= cap:
            break
    return ads, None


def _embedded_blobs(soup: BeautifulSoup) -> list[dict]:
    blobs: list[dict] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if len(text) < 100 or ("ad" not in text.lower()):
            continue
        # Ad Library blobs are assignment-style; try balanced-brace parses
        for candidate in _balanced(text, "{", "}", max_len=6_000_000):
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, dict):
                blobs.append(data)
            break
        if len(blobs) >= 3:
            break
    return blobs


def _balanced(text: str, opener: str, closer: str, *, max_len: int) -> list[str]:
    out = []
    start = text.find(opener)
    while start != -1 and len(out) < 3:
        depth = 0
        in_str = esc = False
        end = -1
        for i in range(start, min(len(text), start + max_len)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end != -1:
            out.append(text[start:end])
        start = text.find(opener, (end if end != -1 else start) + 1)
    return out


def parse_page_summary(html: str) -> dict | None:
    """OG/JSON-LD summary when no ad cards were parseable."""
    soup = BeautifulSoup(html, "html.parser")
    meta = extract_meta(soup)
    title = meta.get("og:title") or (soup.title.get_text(strip=True) if soup.title else None)
    desc = meta.get("og:description") or meta.get("description")
    if not title:
        return None
    return {"title": title[:300], "description": (desc or "")[:1000] or None}


URL_QS_RE = re.compile(r"[?&](q|country|active_status)=")
QS = parse_qs
