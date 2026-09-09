"""BusinessDirectoryActor — generic directory architecture (brief §29).

NOT a list of hardcoded websites. Source adapters (adapters.py) declare how to
walk a directory and parse entries; the actor orchestrates fetch → parse →
yield → checkpoint. The shipped `generic` adapter is fully declarative
(CSS selectors from job input) — deterministic, inspectable, no magic.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from app.scrapers.actors.business_directory.adapters import get_adapter
from app.scrapers.actors.business_directory.schemas import OUTPUT_FIELDS, BusinessDirectoryInput
from app.scrapers.core.base import ActorCategory, ScraperActor
from app.scrapers.core.exceptions import ScraperLimitReachedError, ScraperNetworkError
from app.scrapers.core.netguard import validate_url_async


class BusinessDirectoryActor(ScraperActor):
    id = "business-directory"
    name = "Business Directory"
    version = "1.0.0"
    description = (
        "Extract business entries from public directory websites through "
        "declarative source adapters (CSS-selector based). Bring your own "
        "directory configuration — no hardcoded sites."
    )
    category = ActorCategory.DIRECTORY
    author = "QBIT"
    capabilities = (
        "pluggable source adapters",
        "declarative CSS extraction",
        "pagination support",
        "same-listing-page scope",
        "robots.txt respect",
    )
    supports_pause = True
    input_schema = BusinessDirectoryInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = BusinessDirectoryInput.model_validate(ctx.input)
        adapter = get_adapter(inp.adapter)

        yielded = 0
        list_page_count = 0
        page_url = None
        if ctx.checkpoint and ctx.checkpoint.data.get("next_page"):
            page_url = ctx.checkpoint.data["next_page"]

        pages = list(adapter.pages(inp.config))
        if page_url:
            pages = [page_url]

        for list_url in pages:
            while list_url and list_page_count < inp.config.max_list_pages:
                await ctx.check_stopped()
                ctx.check_deadline()
                list_url = await validate_url_async(list_url, ctx.url_policy)
                ctx.progress.set_stage(f"listing page {list_page_count + 1}")
                try:
                    resp = await ctx.http.get_html(list_url)
                except ScraperNetworkError as exc:
                    await ctx.report("PAGE_FAILED", str(exc), {"url": list_url})
                    break
                await ctx.report("PAGE_FETCHED", None, {"url": list_url})
                list_page_count += 1
                ctx.progress.add_page()
                if resp.status_code >= 400:
                    break

                soup = BeautifulSoup(resp.text, "html.parser")
                for raw in adapter.parse(inp.config, soup, str(resp.url)):
                    if yielded >= inp.max_results:
                        raise ScraperLimitReachedError(
                            f"max_results limit reached ({inp.max_results})"
                        )
                    await ctx.check_stopped()
                    raw["source"] = self.id
                    raw.setdefault("source_url", str(resp.url))
                    yield raw
                    yielded += 1

                await ctx.save_checkpoint({"next_page": list_url, "yielded": yielded})
                list_url = adapter.next_page(inp.config, soup, str(resp.url))

        await ctx.save_checkpoint({"next_page": None, "yielded": yielded}, force=True)

    async def cleanup(self, ctx) -> None:
        await ctx.close()
