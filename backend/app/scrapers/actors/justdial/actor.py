"""JustDial Lead Actor (spec §7.D).

Dedicated JustDial scraper over the site's PUBLIC listing/business pages:
category+city search, pasted search URLs, business pages, bulk URLs and
detail enrichment. Phones are decoded ONLY from what the public page itself
renders (glyph spans / tel: links / JSON-LD); unknown encodings leave the
field empty rather than guessing. Blocks and walls fail the run honestly.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from app.scrapers.actors.justdial.parser import (
    build_search_url,
    parse_business_page,
    parse_listing_page,
    parse_next_page,
)
from app.scrapers.actors.justdial.schemas import OUTPUT_FIELDS, JustDialInput, JustDialMode
from app.scrapers.core.base import ActorCategory, ActorHealth, ScraperActor
from app.scrapers.core.exceptions import (
    ScraperBlockedTargetError,
    ScraperLimitReachedError,
    ScraperNetworkError,
    ScraperValidationError,
)
from app.scrapers.core.extraction import looks_blocked
from app.scrapers.core.netguard import validate_url_async


class JustDialActor(ScraperActor):
    id = "justdial"
    name = "JustDial Lead Finder"
    version = "1.0.0"
    description = (
        "JustDial business listings: category + city search, pasted search "
        "URLs, business pages and bulk URLs. Captures business name, public "
        "phone (as rendered), address, rating, votes and detail-page "
        "enrichment (website / hours / services) with field-level provenance. "
        "Respects robots.txt; blocks are reported, never bypassed."
    )
    category = ActorCategory.DIRECTORY
    capabilities = (
        "category + city search",
        "search URL passthrough",
        "business page parsing",
        "bulk URLs",
        "detail enrichment",
        "public phone presentation decoding",
        "reviews/ratings capture",
    )
    supports_pause = True
    input_schema = JustDialInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = JustDialInput.model_validate(ctx.input)
        errors = inp.validate_policy()
        if errors:
            raise ScraperValidationError("; ".join(f"{k}: {v}" for k, v in errors.items()))

        # ---- plan the fetch list ------------------------------------------
        fetches: list[tuple[str, str]] = []  # (url, kind)
        if inp.mode == JustDialMode.SEARCH:
            base = build_search_url(inp.city, inp.category, inp.keyword)
            fetches.append((base, "listing"))
        elif inp.mode == JustDialMode.SEARCH_URL:
            fetches.append((str(inp.search_url), "listing"))
        else:
            fetches = [(str(u), "business") for u in inp.urls]

        produced = 0
        page = 1
        while fetches:
            url, kind = fetches.pop(0)
            await ctx.check_stopped()
            ctx.check_deadline()
            ctx.check_page_limit(ctx.progress.pages_fetched)
            ctx.progress.set_stage(f"page {page}: {url.split('/')[-1][:50]}")
            try:
                checked = await validate_url_async(url, ctx.url_policy)
                resp = await ctx.http.get_html(checked)
            except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
                await ctx.report("PAGE_FAILED", str(exc), {"url": url})
                continue
            if resp.status_code >= 400:
                await ctx.report(
                    "TARGET_BLOCKED" if resp.status_code in (403, 429) else "PAGE_FAILED",
                    f"HTTP {resp.status_code}",
                    {"url": url},
                )
                continue
            blocked = looks_blocked(resp.text)
            if blocked:
                raise ScraperBlockedTargetError(
                    f"JustDial served a block/verification wall ({blocked!r}) at {url}"
                )
            if kind == "listing":
                records = parse_listing_page(
                    resp.text, source_url=str(resp.url), city_hint=inp.city, cap=inp.max_results - produced
                )
                for record in records:
                    await ctx.check_stopped()
                    if inp.include_details and record["metadata"].get("detail_url"):
                        record = await self._enrich(ctx, record, inp)
                    produced += 1
                    yield record
                await ctx.save_checkpoint({"last_page": url, "produced": produced})
                nxt = parse_next_page(resp.text, str(resp.url))
                if nxt and produced < inp.max_results and page < inp.max_pages:
                    fetches.append((nxt, "listing"))
                    page += 1
                elif produced >= inp.max_results:
                    raise ScraperLimitReachedError(f"max_results limit reached ({inp.max_results})")
            else:
                record = parse_business_page(resp.text, source_url=str(resp.url))
                if record:
                    produced += 1
                    yield record
                await ctx.save_checkpoint({"last_page": url, "produced": produced})

        if produced == 0:
            raise ScraperBlockedTargetError(
                "No public listings could be retrieved from JustDial — the "
                "site served walls or unrecognized markup. Nothing fabricated."
            )

    async def _enrich(self, ctx, record: dict, inp: JustDialInput) -> dict:
        """Detail-page enrichment (spec §7.D mode 5) — best-effort."""
        detail_url = record["metadata"].get("detail_url")
        if not detail_url:
            return record
        try:
            checked = await validate_url_async(detail_url, ctx.url_policy)
            resp = await ctx.http.get_html(checked)
        except (ScraperNetworkError, ScraperBlockedTargetError):
            return record
        if resp.status_code >= 400:
            return record
        detail = parse_business_page(resp.text, source_url=str(resp.url))
        if detail is None:
            return record
        # listing record stays the anchor; empty fields are filled only
        # from the detail page (fill-empty-only, provenance kept)
        for field in ("phone", "website", "email", "address", "postal_code", "rating", "review_count"):
            if not record.get(field) and detail.get(field):
                record[field] = detail[field]
        record["metadata"]["enriched_from"] = str(resp.url)
        record["metadata"]["services"] = detail["metadata"].get("services")
        return record

    async def health_check(self) -> ActorHealth:
        return ActorHealth(
            status="READY",
            detail="Public listing pages only; walls are reported as blocked.",
            dependencies={},
        )
