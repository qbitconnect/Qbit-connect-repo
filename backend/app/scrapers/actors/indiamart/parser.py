"""IndiaMART search/supplier page parser (spec §7.E).

Layered: JSON-LD (Product/Organization) → listing-card selectors with
fallbacks → tel: inventory. Captures supplier identity, product title/price/
MOQ and publicly displayed contact info only. Unknown markup yields fewer
fields — never invented ones.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app.scrapers.core.extraction import (
    extract_emails,
    extract_jsonld,
    first_attr,
    first_text,
    jsonld_by_type,
)

PRICE_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,.]+)", re.IGNORECASE)
MOQ_RE = re.compile(r"(\d+)\s*(?:\w+\s*)?(?:pieces?|pcs|units?|kgs?|tons?|bags?|sets?)", re.IGNORECASE)


def build_search_url(keyword: str, city: str | None = None) -> str:
    base = f"https://dir.indiamart.com/search.mp?ss={quote(keyword)}"
    if city:
        base += f"&city={quote(city)}"
    return base


def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_search_page(html: str, *, source_url: str, city_hint: str | None, cap: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.card, li.lst, div[class*=productcard], div.prod-info")
    out: list[dict] = []
    for card in cards[:cap]:
        title = first_text(card, ["a.cardlinks", "span.elps", "a.prod-name", "[class*=producttitle] a", "a"])
        company_a = card.select_one("p.company.name a, a.company-name, a[class*=company]")
        company = (company_a.get_text(" ", strip=True) if company_a else None) or first_text(
            card, ["p.company", "[class*=companyname]", "[class*=company]"]
        )
        if not title and not company:
            continue
        price_text = first_text(card, ["span.price", "[class*=price]"])
        city_txt = first_text(card, ["p.sm.clg", "span.newLocationUi", "[class*=city]", "[class*=location]"])
        phone = None
        tel = card.select_one('a[href^="tel:"]')
        if tel is not None:
            phone = str(tel.get("href", "")).replace("tel:", "").strip() or None
        href = first_attr(card, ["a.cardlinks", "a.prod-name"], "href") or (
            company_a.get("href") if company_a else None
        )
        detail_url = urljoin(source_url, href) if href else None
        out.append(
            {
                "business_name": (company or title or "")[:300],
                "phone": phone,
                "city": city_txt[:150] if city_txt else city_hint,
                "source": "indiamart",
                "source_url": source_url,
                "metadata": {
                    "record_type": "listing",
                    "platform": "indiamart",
                    "product": (title[:300] if title else None),
                    "price_text": (price_text[:100] if price_text else None),
                    "price_inr": _parse_price(price_text),
                    "moq_hint": (first_text(card, ["span.cntybg", "[class*=moq]", "p"]) or "")[:100] or None,
                    "detail_url": detail_url,
                },
            }
        )
    return out


def parse_supplier_page(html: str, *, source_url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    ld_nodes = jsonld_by_type(extract_jsonld(soup), "organization", "localbusiness", "store")
    ld = ld_nodes[0] if ld_nodes else {}
    name = (
        (ld.get("name") if ld else None)
        or first_text(soup, ["h1", ".company-name", "[class*=companyname]"])
    )
    if not name:
        return None
    phone = None
    tel = soup.select_one('a[href^="tel:"]')
    if tel is not None:
        phone = str(tel.get("href", "")).replace("tel:", "").strip() or None
    text_blob = soup.get_text(" ", strip=True)[:20000]
    emails = extract_emails(text_blob)
    address = first_text(soup, ["[class*=address]", "p.add", ".cntct-add"])
    gst = first_text(soup, ["[class*=gst]", "[class*=gstnum]", "li[class*=gst]"])
    return {
        "business_name": str(name)[:300],
        "phone": phone,
        "email": emails[0] if emails else None,
        "website": first_attr(soup, ["a[class*=website]"], "href"),
        "address": (address or "")[:500] or None,
        "city": first_text(soup, ["[class*=city]", "span.desk-con"]),
        "source": "indiamart",
        "source_url": source_url,
        "metadata": {
            "record_type": "supplier_detail",
            "platform": "indiamart",
            "gst_hint": (gst[:64] if gst else None),
            "verified": bool(ld.get("trust")) or None,
        },
    }


def parse_next_page(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    nxt = soup.select_one("a[rel=next], .pagination a:last-of-type, a[class*=next]")
    if nxt is None or not nxt.get("href"):
        return None
    return urljoin(base_url, str(nxt["href"]))
