"""UniversalWebActor — controlled, configurable extraction foundation (§30).

The user declares WHAT to extract (field → CSS selector → attribute/text) and
optionally a repeating `item_selector` for list pages. Deterministic by
design: no magic "scrape anything" behavior, no ML guessing. Same safety
envelope as all actors (SSRF guard, robots, limits, cooperative controls).
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from app.scrapers.actors.universal.schemas import OUTPUT_FIELDS, UniversalWebInput
from app.scrapers.core.base import ActorCategory, ScraperActor
from app.scrapers.core.exceptions import ScraperLimitReachedError, ScraperNetworkError
from app.scrapers.core.netguard import canonical_url, in_same_domain, normalize_url, validate_url_async

_FIELD_LIMIT = 1000


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


class UniversalWebActor(ScraperActor):
    id = "universal-web"
    name = "Universal Web Scraper"
    version = "1.0.0"
    description = (
        "Configurable, deterministic extraction from a public page: define "
        "fields as CSS selectors (plus an optional repeating item selector "
        "for list pages). No magic — you describe exactly what to capture."
    )
    category = ActorCategory.UNIVERSAL
    author = "QBIT"
    capabilities = (
        "CSS selector fields",
        "repeating item lists",
        "optional same-domain pagination",
        "robots.txt respect",
        "deterministic output",
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
                record["metadata"] = {"fields_declared": [f.name for f in inp.fields]}
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
