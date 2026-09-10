"""UniversalWebActor — §30 declared-fields extraction + Actor Platform §5
auto strategy.

strategy=selectors (default, unchanged): the user declares WHAT to extract
(field → CSS selector → attribute/text). Deterministic, no magic.

strategy=auto (spec §5 'URL → SCRAPE'): the layered engine chooses:
  HTTP fetch → HTML parse → JSON-LD + OpenGraph → embedded JSON →
  contact patterns → link/table inventory → JS-heavy detection.
A headless-browser fallback exists (LEVEL 5) but is used ONLY when the page
is JS-heavy AND the optional Playwright dependency is genuinely available;
otherwise the run reports `browser_fallback: unavailable` honestly and ships
what static extraction found. No CAPTCHA handling, no evasion, ever.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from app.scrapers.actors.universal.schemas import OUTPUT_FIELDS, ExtractStrategy, UniversalWebInput
from app.scrapers.core.base import ActorCategory, ScraperActor
from app.scrapers.core.exceptions import ScraperLimitReachedError, ScraperNetworkError
from app.scrapers.core.extraction import (
    dig,
    extract_emails,
    extract_embedded_json,
    extract_jsonld,
    extract_links,
    extract_meta,
    extract_phones,
    jsonld_by_type,
)
from app.scrapers.core.netguard import canonical_url, in_same_domain, normalize_url, validate_url_async

_FIELD_LIMIT = 1000

#: heuristic: rendered text below this on a big HTML doc suggests a JS app
JS_HEAVY_MIN_TEXT = 300
EMBEDDED_MARKERS = ("__NEXT_DATA__", "_sharedData", "__INITIAL_STATE__", "window.__data", "nuxt")


def _read_field(node, spec):
    element = node.select_one(spec.selector)
    if element is None:
        return None
    if spec.attribute == "text":
        value = element.get_text(" ", strip=True)
    else:
        value = element.get(spec.attribute) or ""
    value = str(value).strip()[:_FIELD_LIMIT]
    return value or None


def _page_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(" ", strip=True)[:300]
    og = soup.find("meta", attrs={"property": "og:title"})
    return og.get("content", "").strip()[:300] or None if og else None


def _auto_record(soup: BeautifulSoup, html: str, *, url: str, source: str) -> dict:
    """Spec §5 auto pipeline over one fetched page (pure, no I/O)."""
    meta = extract_meta(soup)
    title = _page_title(soup)
    text_blob = soup.get_text(" ", strip=True)
    emails = extract_emails(text_blob[:50000])
    phones = extract_phones(text_blob[:50000])
    jsonld = extract_jsonld(soup)
    org_types = jsonld_by_type(jsonld, "organization", "localbusiness", "corporation", "store")
    ld_org = org_types[0] if org_types else (jsonld[0] if jsonld else {})
    canonical = None
    link_tag = soup.find("link", rel="canonical")
    if link_tag is not None and link_tag.get("href"):
        canonical = str(link_tag["href"])[:500]
    # tables: row/column counts only (content stays queryable via dataset)
    tables = []
    for table in soup.find_all("table")[:20]:
        rows = table.find_all("tr")
        tables.append({"rows": len(rows), "columns": max((len(r.find_all(["td", "th"])) for r in rows), default=0)})
    js_heavy = len(text_blob) < JS_HEAVY_MIN_TEXT and len(html) > 50_000
    metadata = {
        "record_type": "auto_page",
        "extraction_strategy": "auto",
        "title": title,
        "og": {k: v for k, v in meta.items() if k.startswith(("og:", "twitter:"))} or None,
        "json_ld_types": [n.get("@type") for n in jsonld if n.get("@type")][:20] or None,
        "json_ld_org": {
            k: str(ld_org.get(k))[:300]
            for k in ("name", "description", "telephone", "email", "address", "url")
            if ld_org.get(k)
        } or None,
        "emails": emails or None,
        "phones": phones or None,
        "canonical": canonical,
        "tables": tables or None,
        "link_count": len(soup.find_all("a", href=True)),
        "images": sum(1 for _ in soup.find_all("img")),
        "text_chars": len(text_blob),
        "js_heavy": js_heavy or None,
        "embedded_json_found": bool(extract_embedded_json(html, list(EMBEDDED_MARKERS))) or None,
    }
    email = (emails or [None])[0]
    phone = (phones or [None])[0]
    website = metadata["json_ld_org"].get("url") if metadata["json_ld_org"] else None
    return {
        "business_name": (metadata["json_ld_org"].get("name") if metadata["json_ld_org"] else None) or title or url,
        "email": email,
        "phone": phone,
        "website": website or meta.get("og:url"),
        "address": metadata["json_ld_org"].get("address") if metadata["json_ld_org"] else None,
        "source": source,
        "source_url": url,
        "metadata": {k: v for k, v in metadata.items() if v is not None},
    }


class UniversalWebActor(ScraperActor):
    id = "universal-web"
    name = "Universal Web Scraper"
    version = "2.0.0"
    description = (
        "Paste any public URL and get structured output. auto strategy: the "
        "engine detects the best extraction path (JSON-LD, OpenGraph, "
        "embedded JSON, contacts, links, tables) from plain HTTP — a browser "
        "is used only when the page needs it AND the optional Playwright "
        "runtime is available. selectors strategy: declare exact fields as "
        "CSS selectors for deterministic capture."
    )
    category = ActorCategory.UNIVERSAL
    author = "QBIT"
    capabilities = (
        "auto-detect extraction (json-ld / og / embedded json / contacts)",
        "CSS selector fields",
        "repeating item lists",
        "optional same-domain pagination",
        "JS-heavy page detection",
        "optional headless fallback (when installed)",
        "robots.txt respect",
    )
    supports_pause = True
    input_schema = UniversalWebInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = UniversalWebInput.model_validate(ctx.input)

        url = await validate_url_async(str(inp.url), ctx.url_policy)
        seen: set[str] = {canonical_url(url)}
        queue: list[str] = [url]
        fetched = int(ctx.checkpoint.data.get("pages_fetched", 0)) if ctx.checkpoint else 0
        frontier = ctx.checkpoint.data.get("frontier") if ctx.checkpoint else None
        if frontier:
            queue = list(frontier)
        yielded = 0

        while queue:
            await ctx.check_stopped()
            ctx.check_deadline()
            current = queue.pop(0)
            fetched += 1
            ctx.progress.add_page()
            ctx.progress.set_stage(f"extracting page {fetched}")
            try:
                resp = await ctx.http.get_html(current)
            except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
                await ctx.report("PAGE_FAILED", str(exc), {"url": current})
                continue
            await ctx.report("PAGE_FETCHED", None, {"url": current})
            if resp.status_code >= 400:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            if inp.strategy == ExtractStrategy.AUTO:
                record = _auto_record(soup, resp.text, url=str(resp.url), source=self.id)
                # LEVEL 5 — browser fallback, only when warranted AND real
                if record["metadata"].get("js_heavy"):
                    from app.services.scraping.browser import browser_available

                    ok, reason = browser_available()
                    if ok:
                        try:
                            from app.services.scraping.browser import fetch_with_browser

                            html2, status2 = await fetch_with_browser(str(resp.url))
                            if status2 < 400 and html2:
                                soup2 = BeautifulSoup(html2, "html.parser")
                                enriched = _auto_record(
                                    soup2, html2, url=str(resp.url), source=self.id
                                )
                                enriched["metadata"]["browser_fallback"] = "used"
                                enriched["metadata"]["static_text_chars"] = record["metadata"].get("text_chars")
                                record = enriched
                        except Exception as exc:  # noqa: BLE001 — browser is optional
                            await ctx.report(
                                "BROWSER_FALLBACK_FAILED", f"{type(exc).__name__}: {exc}", {"url": current}
                            )
                    else:
                        record["metadata"]["browser_fallback"] = f"unavailable: {reason}"
                yielded += 1
                yield record
                if ctx.limits.records_exceeded(yielded):
                    raise ScraperLimitReachedError(
                        f"max_records limit reached ({ctx.limits.max_records})"
                    )
                await ctx.save_checkpoint({"frontier": queue[:100], "pages_fetched": fetched})
                if fetched >= inp.max_pages and queue:
                    raise ScraperLimitReachedError(f"max_pages limit reached ({inp.max_pages})")
                continue

            if inp.item_selector:
                nodes = soup.select(inp.item_selector)[:1000]
            else:
                nodes = [soup]

            for node in nodes:
                await ctx.check_stopped()
                record: dict = {}
                for spec in inp.fields:
                    value = _read_field(node, spec)
                    if value is not None:
                        record[spec.name] = value
                if not record:
                    continue
                record["source"] = self.id
                record["source_url"] = str(resp.url)
                record["metadata"] = {"extraction_strategy": "selectors", "fields_declared": [f.name for f in inp.fields]}
                yield record
                yielded += 1
                if ctx.limits.records_exceeded(yielded):
                    raise ScraperLimitReachedError(
                        f"max_records limit reached ({ctx.limits.max_records})"
                    )

            await ctx.save_checkpoint({"frontier": queue[:100], "pages_fetched": fetched})

            if fetched >= inp.max_pages and queue:
                raise ScraperLimitReachedError(f"max_pages limit reached ({inp.max_pages})")

            if inp.pagination_next_selector:
                # Pagination is explicit user configuration, but out-of-domain
                # hops require follow_same_domain=True (§42).
                nxt = soup.select_one(inp.pagination_next_selector)
                if nxt is not None and nxt.get("href"):
                    next_url = normalize_url(str(resp.url), str(nxt["href"]))
                    allowed = (
                        in_same_domain(next_url, url) or inp.follow_same_domain
                    )
                    if allowed and canonical_url(next_url) not in seen:
                        seen.add(canonical_url(next_url))
                        queue.append(next_url)

        await ctx.save_checkpoint({"frontier": [], "pages_fetched": fetched}, force=True)

    async def cleanup(self, ctx) -> None:
        await ctx.close()
