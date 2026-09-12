"""QBIT CONNECT — URL Analyzer Service (Product UX Layer).

Analyzes user-provided URLs to detect supported scraping sources (Google Maps,
Justdial, IndiaMART, Website Crawler, Sitemap Intelligence, Generic Directory,
Public Data) and extract parsed query parameters without executing scrapes or
changing the underlying scraping engines.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse


@dataclass
class UrlAnalysisResult:
    source: str | None
    source_name: str | None
    normalized_url: str
    input_type: str  # "search_query" | "search_url" | "direct_url" | "sitemap" | "dataset" | "unknown"
    query: str | None
    location: str | None
    confidence: float
    message: str = ""
    suggested_actor_id: str | None = None
    input_payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UrlAnalyzer:
    """Reusable URL parser & source detector for Scraper Workbench input."""

    @classmethod
    def analyze(cls, raw_url: str) -> UrlAnalysisResult:
        u = (raw_url or "").strip()
        if not u:
            return UrlAnalysisResult(
                source=None,
                source_name=None,
                normalized_url="",
                input_type="unknown",
                query=None,
                location=None,
                confidence=0.0,
                message="Please provide a valid URL.",
            )

        if not u.startswith(("http://", "https://")):
            u = "https://" + u

        try:
            parsed = urlparse(u)
            host = (parsed.netloc or "").lower()
            path = parsed.path or "/"
            qs = parse_qs(parsed.query)
        except Exception as exc:
            return UrlAnalysisResult(
                source=None,
                source_name=None,
                normalized_url=u,
                input_type="unknown",
                query=None,
                location=None,
                confidence=0.0,
                message=f"Malformed URL: {exc}",
            )

        # 1. Google Maps URLs
        if "google." in host and ("/maps" in path or host.startswith("maps.google.")):
            query = None
            location = None
            search_match = re.search(r"/maps/search/([^/@]+)", path)
            if search_match:
                raw_q = unquote_plus(search_match.group(1)).replace("+", " ").strip()
                query = raw_q
                loc_match = re.search(r"\b(?:in|near|around)\s+([A-Za-z\s]+)$", raw_q, re.IGNORECASE)
                if loc_match:
                    location = loc_match.group(1).strip()
            elif "q" in qs:
                raw_q = qs["q"][0]
                query = raw_q
                loc_match = re.search(r"\b(?:in|near|around)\s+([A-Za-z\s]+)$", raw_q, re.IGNORECASE)
                if loc_match:
                    location = loc_match.group(1).strip()

            return UrlAnalysisResult(
                source="google-maps",
                source_name="Google Maps",
                normalized_url=u,
                input_type="search_url" if query else "direct_url",
                query=query or "Google Maps Search",
                location=location,
                confidence=0.95,
                suggested_actor_id="google-maps",
                message="Google Maps URL detected. Provider configuration required for compliant execution.",
                input_payload={"query": query or u, "max_records": 100},
            )

        # 2. Justdial URLs
        if "justdial.com" in host:
            city = None
            category = None
            query = None
            segments = [s for s in path.strip("/").split("/") if s]
            if len(segments) >= 2:
                city = unquote(segments[0]).title()
                category = unquote(segments[1]).replace("-", " ").title()
                query = f"{category} in {city}"
            elif len(segments) == 1:
                city = unquote(segments[0]).title()
            if "q" in qs:
                query = qs["q"][0]
            if "city" in qs:
                city = qs["city"][0].title()

            return UrlAnalysisResult(
                source="justdial",
                source_name="JustDial",
                normalized_url=u,
                input_type="search_url",
                query=query or category or "JustDial Listings",
                location=city,
                confidence=0.95,
                suggested_actor_id="justdial",
                message="JustDial search/listing URL detected.",
                input_payload={
                    "mode": "search_url",
                    "search_url": u,
                    "city": city or "Delhi",
                    "category": category or "Business",
                    "max_results": 100,
                },
            )

        # 3. IndiaMART URLs
        if "indiamart.com" in host:
            query = None
            city = None
            if "ss" in qs:
                query = unquote_plus(qs["ss"][0])
            if "city" in qs:
                city = unquote_plus(qs["city"][0]).title()
            if not query:
                cat_match = re.search(r"/(?:impcat|search)/([a-zA-Z0-9_-]+)", path)
                if cat_match:
                    query = cat_match.group(1).replace("-", " ").replace("_", " ").title()

            return UrlAnalysisResult(
                source="indiamart",
                source_name="IndiaMART",
                normalized_url=u,
                input_type="search_url",
                query=query or "IndiaMART Supplier Directory",
                location=city,
                confidence=0.95,
                suggested_actor_id="indiamart",
                message="IndiaMART supplier directory URL detected.",
                input_payload={
                    "mode": "supplier_search" if city else "product_search",
                    "keyword": query or "suppliers",
                    "city": city or "",
                    "max_results": 100,
                },
            )

        # 4. Meta Ads Library
        if "facebook.com" in host and "/ads/library" in path:
            keyword = qs.get("q", [None])[0]
            return UrlAnalysisResult(
                source="meta-ads-library",
                source_name="Meta Ads Library",
                normalized_url=u,
                input_type="search_url",
                query=keyword or "Ad Archive",
                location=qs.get("country", ["ALL"])[0],
                confidence=0.95,
                suggested_actor_id="meta-ads-library",
                message="Meta Ads Library archive URL detected.",
                input_payload={"keyword": keyword or "technology", "mode": "search"},
            )

        # 5. Sitemap XML URLs
        if path.endswith(".xml") or "sitemap" in path.lower():
            return UrlAnalysisResult(
                source="sitemap-intelligence",
                source_name="Sitemap Intelligence",
                normalized_url=u,
                input_type="sitemap",
                query=host,
                location=None,
                confidence=0.9,
                suggested_actor_id="sitemap-intelligence",
                message="XML Sitemap URL detected.",
                input_payload={"url": u, "max_records": 100},
            )

        # 6. Public Data CSV / JSON
        if path.endswith((".csv", ".json", ".tsv")):
            fmt = "csv" if path.endswith((".csv", ".tsv")) else "json"
            return UrlAnalysisResult(
                source="public-data",
                source_name="Public Data Ingestion",
                normalized_url=u,
                input_type="dataset",
                query=path.split("/")[-1],
                location=None,
                confidence=0.9,
                suggested_actor_id="public-data",
                message=f"Tabular public dataset ({fmt.upper()}) detected.",
                input_payload={"url": u, "format": fmt, "max_records": 100},
            )

        # 7. Instagram URLs
        if "instagram.com" in host:
            handle = path.strip("/").split("/")[0] if path.strip("/") else None
            return UrlAnalysisResult(
                source="instagram",
                source_name="Instagram",
                normalized_url=u,
                input_type="direct_url",
                query=f"@{handle}" if handle else "Instagram Profile",
                location=None,
                confidence=0.9,
                suggested_actor_id="instagram",
                message="Instagram public profile URL detected.",
                input_payload={"mode": "profile", "username": handle or ""},
            )

        # 8. LinkedIn URLs
        if "linkedin.com" in host:
            slug = path.strip("/").split("/")[-1] if path.strip("/") else None
            return UrlAnalysisResult(
                source="linkedin-public",
                source_name="LinkedIn",
                normalized_url=u,
                input_type="direct_url",
                query=slug or "LinkedIn Company",
                location=None,
                confidence=0.9,
                suggested_actor_id="linkedin-public",
                message="LinkedIn public company page URL detected.",
                input_payload={"slug": slug or "", "mode": "company"},
            )

        # 9. Generic Domain / Website / Universal Web
        return UrlAnalysisResult(
            source="universal-web",
            source_name="Universal Web Scraper",
            normalized_url=u,
            input_type="direct_url",
            query=host,
            location=None,
            confidence=0.75,
            suggested_actor_id="universal-web",
            message="Public website detected. Universal extraction / website crawl strategy applicable.",
            input_payload={"url": u, "strategy": "auto", "max_records": 100},
        )
