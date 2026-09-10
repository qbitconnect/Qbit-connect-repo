"""IndiaMART Lead Actor (spec §7.E).

Public IndiaMART surfaces: product search, supplier search, supplier/product
URLs, bulk URLs and pagination + detail enrichment. Contact info is captured
only when the page publicly exposes it (tel: links, JSON-LD, rendered text).
Blocks fail the run honestly (spec §36/§42).
"""

from __future__ import annotations

from app.scrapers.actors.indiamart.parser import (
    build_search_url,
    parse_next_page,
    parse_search_page,
    parse_supplier_page,
)
from app.scrapers.actors.indiamart.schemas import OUTPUT_FIELDS, IndiaMartInput, IndiaMartMode
from app.scrapers.core.base import ActorCategory, ActorHealth, ScraperActor
from app.scrapers.core.exceptions import (
    ScraperBlockedTargetError,
    ScraperLimitReachedError,
    ScraperNetworkError,
    ScraperValidationError,
)
from app.scrapers.core.extraction import looks_blocked
from app.scrapers.core.netguard import validate_url_async


class IndiaMartActor(ScraperActor):
    id = "indiamart"
    name = "IndiaMART Supplier & Product Finder"
    version = "1.0.0"
    description = (
        "IndiaMART supplier and product intelligence: keyword searches, "
        "category scans, supplier/product pages, bulk URLs and pagination. "
        "Captures company identity, product title/price/MOQ hints, city and "
        "publicly exposed contact details with per-field provenance. "
        "Public pages only — walls are reported, never bypassed."
    )
    category = ActorCategory.ECOMMERCE
    capabilities = (
        "product search",
        "supplier search",
        "supplier/product URL parsing",
        "bulk URLs",
        "pagination",
        "detail enrichment",
        "price / MOQ hints",
    )
    supports_pause = True

    def validate_policy(self, model) -> dict[str, str]:
        return model.validate_policy()

    input_schema = IndiaMartInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = IndiaMartInput.model_validate(ctx.input)
        errors = inp.validate_policy()
        if errors:
            raise ScraperValidationError("; ".join(f"{k}: {v}" for k, v in errors.items()))

        fetches: list[tuple[str, str]] = []
        if inp.mode == IndiaMartMode.PRODUCT_SEARCH:
            fetches.append((build_search_url(inp.keyword, inp.city), "search"))
        elif inp.mode == IndiaMartMode.SUPPLIER_SEARCH:
            fetches.append((build_search_url(inp.keyword, inp.city), "search"))
        else:
            fetches = [(str(u), "detail") for u in inp.urls]

        produced = 0
        page = 1
        while fetches:
            url, kind = fetches.pop(0)
            await ctx.check_stopped()
            ctx.check_deadline()
            ctx.check_page_limit(ctx.progress.pages_fetched)
            ctx.progress.set_stage(f"page {page}: {url.split('/')[-1][:50] or url[:50]}")
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
                    f"IndiaMART served a block/verification wall ({blocked!r}) at {url}"
                )
            if kind == "search":
                records = parse_search_page(
                    resp.text, source_url=str(resp.url), city_hint=inp.city,
                    cap=inp.max_results - produced,
                )
                for record in records:
                    await ctx.check_stopped()
                    produced += 1
                    yield record
                await ctx.save_checkpoint({"last_page": url, "produced": produced})
                nxt = parse_next_page(resp.text, str(resp.url))
                if nxt and produced < inp.max_results and page < inp.max_pages:
                    fetches.append((nxt, "search"))
                    page += 1
                elif produced >= inp.max_results:
                    raise ScraperLimitReachedError(f"max_results limit reached ({inp.max_results})")
            else:
                record = parse_supplier_page(resp.text, source_url=str(resp.url))
                if record is None:
                    await ctx.report("PARSER_EMPTY", f"No supplier content parsed: {url}", {})
                else:
                    produced += 1
                    yield record
                await ctx.save_checkpoint({"last_page": url, "produced": produced})

        if produced == 0:
            raise ScraperBlockedTargetError(
                "No public IndiaMART content could be retrieved — walls or "
                "unrecognized markup. Nothing fabricated."
            )

    async def health_check(self) -> ActorHealth:
        return ActorHealth(
            status="READY",
            detail="Public search/supplier pages only; walls reported as blocked.",
            dependencies={},
        )
