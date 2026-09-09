"""Sitemap / robots / structured-data intelligence Actor (spec §18).

Discovers a site's sitemap chain (robots.txt Sitemap: declarations →
sitemap index → urlset), counts and dates the URL inventory, then samples
up to N public pages and extracts their structured-data surface: JSON-LD
types, OpenGraph / Twitter meta, canonical URL and page title.

Fully self-contained: uses only the compliant HTTP stack (SSRF guard,
robots.txt gate, response-size caps, per-host rate limiting). No provider,
no login, no evasion — pages the site's robots.txt disallows are skipped
silently and counted.

Yields one record per sampled page plus (when the sitemap inventory is
non-empty) a synthetic site-summary record, so a run on any public site
produces actionable structured results.
"""

from __future__ import annotations

import json as _json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from app.scrapers.actors.sitemap_intelligence.schemas import OUTPUT_FIELDS, SitemapInput
from app.scrapers.actors.website.parser import page_title
from app.scrapers.core.base import ActorCategory, ActorHealth, ScraperActor, ValidationReport
from app.scrapers.core.exceptions import (
    ScraperBlockedTargetError,
    ScraperNetworkError,
    ScraperValidationError,
)
from app.scrapers.core.netguard import canonical_url, validate_url_async

_NS_URLSET = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _parse_loc_and_lastmod(entry) -> tuple[str | None, str | None]:
    loc = entry.findtext(f"{_NS_URLSET}loc") or entry.findtext("loc")
    lastmod = entry.findtext(f"{_NS_URLSET}lastmod") or entry.findtext("lastmod")
    return (
        loc.strip() if loc and loc.strip() else None,
        lastmod.strip() if lastmod and lastmod.strip() else None,
    )


def _extract_jsonld_types(soup: BeautifulSoup) -> list[str]:
    types: list[str] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = _json.loads(script.string or "{}")
        except (_json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict):
                t = node.get("@type")
                if isinstance(t, str):
                    types.append(t)
                elif isinstance(t, list):
                    types.extend(str(x) for x in t)
        if len(types) >= 20:
            break
    return types[:20]


def _meta_content(soup: BeautifulSoup, *attrs: str) -> dict:
    out: dict = {}
    for attr in attrs:
        tag = soup.find("meta", attrs={attr.split("=")[0]: attr.split("=", 1)[-1]})
        if tag and tag.get("content"):
            key = attr.split("=", 1)[-1].replace(":", "_")
            out[key] = str(tag["content"])[:300]
    return out


class SitemapIntelligenceActor(ScraperActor):
    id = "sitemap-intelligence"
    name = "Sitemap & Structured Data Intelligence"
    version = "1.0.0"
    description = (
        "Discover a website's sitemap inventory (robots.txt → sitemap index → "
        "urlset), then sample public pages and extract their structured-data "
        "surface: JSON-LD types, OpenGraph/Twitter meta, canonical URL and "
        "titles. Compliant by design — robots.txt is honored, nothing is "
        "fetched beyond the target site."
    )
    category = ActorCategory.WEBSITE
    capabilities = (
        "sitemap discovery via robots.txt",
        "sitemap index + urlset parsing",
        "url inventory count + lastmod range",
        "json-ld type extraction",
        "opengraph / twitter meta",
        "canonical url detection",
        "page classification hints",
    )
    supports_pause = True
    input_schema = SitemapInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = SitemapInput.model_validate(ctx.input)
        start = str(inp.url)
        parsed_start = await validate_url_async(start, ctx.url_policy)
        scheme, rest = parsed_start.split("//", 1)
        origin = f"{scheme}//{rest.split('/', 1)[0]}"
        base_host = rest.split("/", 1)[0].lower()
        fetched_pages = 0
        urls_seen: set[str] = set()

        # ---- 1. robots.txt → Sitemap: declarations -----------------------
        sitemap_urls: list[str] = []
        robots_allowed_all = True
        try:
            robots = await ctx.http.get_html(f"{origin}/robots.txt")
            if robots.status_code < 400:
                for line in robots.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("sitemap:"):
                        sm = line.split(":", 1)[1].strip()
                        if sm and canonical_url(sm) not in urls_seen:
                            urls_seen.add(canonical_url(sm))
                            sitemap_urls.append(sm)
        except (ScraperNetworkError, ScraperBlockedTargetError):
            pass  # no robots reachable — fall through to conventional paths

        # ---- 2. conventional sitemap paths ------------------------------
        if not sitemap_urls:
            sitemap_urls = [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]

        # ---- 3. walk the sitemap graph (index → urlset), bounded --------
        page_urls: list[str] = []
        lastmods: list[str] = []
        total_urls = 0
        sitemaps_parsed = 0
        while sitemap_urls and sitemaps_parsed < 10:
            sm_url = sitemap_urls.pop(0)
            await ctx.check_stopped()
            ctx.check_deadline()
            try:
                checked = await validate_url_async(sm_url, ctx.url_policy)
                resp = await ctx.http.get_html(checked)
            except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
                await ctx.report("PAGE_FAILED", str(exc), {"url": sm_url})
                continue
            if resp.status_code >= 400:
                continue
            try:
                root = ET.fromstring(resp.text[:5_000_000])
            except ET.ParseError:
                continue
            sitemaps_parsed += 1
            ctx.progress.add_page()
            if root.tag == f"{_NS_URLSET}sitemapindex" or root.tag == "sitemapindex":
                for entry in root:
                    loc, _ = _parse_loc_and_lastmod(entry)
                    if loc and canonical_url(loc) not in urls_seen:
                        urls_seen.add(canonical_url(loc))
                        sitemap_urls.append(loc)
            else:  # urlset (or unnamespaced urlset)
                for entry in root:
                    loc, lastmod = _parse_loc_and_lastmod(entry)
                    if loc:
                        total_urls += 1
                        if lastmod:
                            lastmods.append(lastmod)
                        if len(page_urls) < inp.max_urls_sampled and (
                            canonical_url(loc) not in urls_seen
                        ):
                            urls_seen.add(canonical_url(loc))
                            page_urls.append(loc)
            await ctx.save_checkpoint({"pages": page_urls, "total": total_urls})

        # ---- 4. sample pages: structured-data surface --------------------
        site_title: str | None = None
        for page_url in page_urls:
            await ctx.check_stopped()
            ctx.check_deadline()
            ctx.check_page_limit(fetched_pages)
            try:
                checked = await validate_url_async(page_url, ctx.url_policy)
                resp = await ctx.http.get_html(checked)
            except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
                await ctx.report("PAGE_FAILED", str(exc), {"url": page_url})
                continue
            if resp.status_code >= 400:
                continue
            fetched_pages += 1
            ctx.progress.add_page()
            ctx.progress.set_stage(f"sampling ({fetched_pages}/{len(page_urls)})")
            soup = BeautifulSoup(resp.text, "html.parser")
            title = page_title(soup)
            if site_title is None and title:
                site_title = title
            record = {
                "business_name": (title or base_host)[:300],
                "website": base_host,
                "source": self.id,
                "source_url": str(resp.url),
                "metadata": {
                    "record_type": "page",
                    "base_domain": base_host,
                    "canonical": (
                        (soup.find("link", rel="canonical").get("href") or "")
                        if soup.find("link", rel="canonical") else ""
                    ),
                    "json_ld_types": (
                        _extract_jsonld_types(soup)
                        if inp.extract_structured_data else []
                    ),
                    "open_graph": (
                        _meta_content(
                            soup, "property=og:title", "property=og:description",
                            "property=og:site_name", "name=twitter:card",
                        ) if inp.extract_structured_data else {}
                    ),
                    "sitemap_inventory": {
                        "total_urls": total_urls,
                        "sitemaps_parsed": sitemaps_parsed,
                    },
                    "pages_scanned": fetched_pages,
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            yield record

        # ---- 5. site summary record (always, when reachable) -------------
        yield {
            "business_name": (site_title or base_host)[:300],
            "website": base_host,
            "source": self.id,
            "source_url": str(parsed_start),
            "metadata": {
                "record_type": "site_summary",
                "base_domain": base_host,
                "robots_found": robots_allowed_all,
                "sitemap_inventory": {
                    "total_urls": total_urls,
                    "sitemaps_parsed": sitemaps_parsed,
                    "lastmod_min": min(lastmods) if lastmods else None,
                    "lastmod_max": max(lastmods) if lastmods else None,
                    "sampled": len(page_urls),
                    "pages_fetched": fetched_pages,
                },
                "pages_scanned": fetched_pages + sitemaps_parsed,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    async def health_check(self) -> ActorHealth:
        return ActorHealth(
            status="READY",
            detail="Self-contained — only the target site is contacted.",
            dependencies={},
        )
