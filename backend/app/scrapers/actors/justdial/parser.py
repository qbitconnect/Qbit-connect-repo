"""JustDial listing/business page parser (spec §7.D).

Layered extraction (spec §27):
  1. JSON-LD (LocalBusiness blocks when served — carries telephone/address)
  2. semantic selectors with FALLBACK lists (listing cards)
  3. tel: link inventory
  4. public presentation phone decoding (`mobilesv` icon classes — the site
     renders phone digits as CSS class glyphs in its public HTML; the
     well-documented static glyph table below DECODES THE PUBLIC PAGE. If
     the served classes don't match the table, the phone is left empty —
     never guessed.)

Privacy: only publicly displayed contact info is returned (spec §7.D).
"""

from __future__ import annotations

import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from app.scrapers.core.extraction import (
    extract_emails,
    extract_jsonld,
    first_attr,
    first_text,
    jsonld_by_type,
)

# --- public glyph table for JustDial's rendered phone digits -----------------
# (classes rendered in the public HTML as <span class="mobilesv icon-XX">)
TEL_GLYPHS = {
    "ji": "1", "ba": "2", "cb": "3", "dc": "4", "ed": "5",
    "fe": "6", "gf": "7", "hg": "8", "ih": "9", "aj": "0",
    "ak": "+", "al": "-", "am": "(", "an": ")",
}

RATING_RE = re.compile(r"(\d(?:\.\d)?)")
VOTES_RE = re.compile(r"([\d,]+)")


def slug(value: str) -> str:
    """'new delhi' → 'New-Delhi'; 'electricians' → 'Electricians'."""
    return "-".join(w.capitalize() for w in re.split(r"[\s_]+", value.strip()) if w)


def build_search_url(city: str, category: str, keyword: str | None = None) -> str:
    """https://www.justdial.com/<City>/<Category>[-Keyword]"""
    path = f"{slug(city)}/{slug(category)}"
    if keyword:
        path += f"-{slug(keyword)}"
    return f"https://www.justdial.com/{path}"


def decode_mobilesv(soup: BeautifulSoup) -> str | None:
    """Decode the public mobilesv glyph spans → phone string, or None."""
    spans = soup.select("span.mobilesv")
    if not spans:
        return None
    out: list[str] = []
    for span in spans:
        classes = (span.get("class") or [])
        glyph = None
        for cls in classes:
            if cls.startswith("icon-"):
                glyph = TEL_GLYPHS.get(cls.removeprefix("icon-"))
                break
        if glyph is None:
            return None  # unknown glyph → refuse to guess (no fabrication)
        out.append(glyph)
    value = "".join(out).strip()
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7 or len(digits) > 15:
        return None
    return value


def _jsonld_business(soup: BeautifulSoup) -> dict | None:
    nodes = jsonld_by_type(
        extract_jsonld(soup),
        "localbusiness", "organization", "store", "restaurant", "professionalService",
    )
    return nodes[0] if nodes else None


def parse_listing_page(html: str, *, source_url: str, city_hint: str | None, cap: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.cntanr, div.store-details, div[class*=businessCard]")
    out: list[dict] = []
    for card in cards[:cap]:
        name = first_text(card, [
            "h2.business-name span.lng_cont_name",
            "span.lng_cont_name",
            "h2.business-name",
            ".store-name",
            "[class*=business-name]",
        ])
        if not name:
            continue
        address = first_text(card, [
            "span.cont_sw_addr", "span.address-rte", "[class*=address]",
        ])
        rating_txt = first_text(card, ["span.total_rate", "[class*=rating-box]", "[class*=totalrate]"])
        votes_txt = first_text(card, ["span.rating_count", "[class*=rating-count]", "[class*=votes]"])
        rating = None
        if rating_txt:
            m = RATING_RE.search(rating_txt)
            rating = float(m.group(1)) if m else None
        votes = None
        if votes_txt:
            m = VOTES_RE.search(votes_txt)
            votes = int(m.group(1).replace(",", "")) if m else None
        detail_href = first_attr(card, ["a.business-name", "h2 a", "a[href*=/biz/]", "a"], "href")
        phone = decode_mobilesv(card)
        if phone is None:
            tel = first_attr(card, ['a[href^="tel:"]'], "href")
            if tel:
                phone = tel.replace("tel:", "").strip()
        out.append(
            {
                "business_name": name[:300],
                "phone": phone,
                "address": address[:500] if address else None,
                "city": city_hint,
                "rating": rating,
                "review_count": votes,
                "source": "justdial",
                "source_url": source_url,
                "metadata": {
                    "record_type": "listing",
                    "platform": "justdial",
                    "detail_url": (
                        f"https://www.justdial.com{detail_href}"
                        if detail_href and detail_href.startswith("/")
                        else detail_href
                    ),
                },
            }
        )
    return out


def parse_business_page(html: str, *, source_url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    ld = _jsonld_business(soup)
    name = (
        (ld.get("name") if ld else None)
        or first_text(soup, ["h1", ".business-name", "[class*=storeName]"])
    )
    if not name:
        return None
    phone = decode_mobilesv(soup)
    if phone is None and ld and ld.get("telephone"):
        phone = str(ld.get("telephone"))
    if phone is None:
        tel = first_attr(soup, ['a[href^="tel:"]'], "href")
        if tel:
            phone = tel.replace("tel:", "").strip()
    address = (ld.get("address") or {}) if ld else {}
    address_text = first_text(soup, ["span.cont_sw_addr", "[class*=address]"]) or (
        ", ".join(
            str(address.get(k))
            for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")
            if address.get(k)
        ) or None
    )
    website = first_attr(soup, ["a[class*=website], a[href*=http][rel*=nofollow]"], "href")
    text_blob = soup.get_text(" ", strip=True)[:20000]
    emails = extract_emails(text_blob)
    return {
        "business_name": str(name)[:300],
        "phone": phone,
        "email": emails[0] if emails else None,
        "website": website,
        "address": (address_text or "")[:500] or None,
        "city": address.get("addressLocality") if address else None,
        "postal_code": address.get("postalCode") if address else None,
        "rating": (float(ld["aggregateRating"]["ratingValue"]) if ld and isinstance(ld.get("aggregateRating"), dict) else None),
        "review_count": (ld["aggregateRating"].get("ratingCount") or ld["aggregateRating"].get("reviewCount")) if ld and isinstance(ld.get("aggregateRating"), dict) else None,
        "source": "justdial",
        "source_url": source_url,
        "metadata": {
            "record_type": "business_detail",
            "platform": "justdial",
        },
    }


def parse_next_page(soup_or_html, base_url: str) -> str | None:
    soup = (
        soup_or_html
        if hasattr(soup_or_html, "select")
        else BeautifulSoup(soup_or_html, "html.parser")
    )
    nxt = soup.select_one("a[rel=next], .pagination a:last-of-type, a[class*=next]")
    if nxt is None or not nxt.get("href"):
        return None
    from urllib.parse import urljoin

    return urljoin(base_url, str(nxt["href"]))


def keyword_qs(keyword: str) -> str:
    return quote(keyword)
