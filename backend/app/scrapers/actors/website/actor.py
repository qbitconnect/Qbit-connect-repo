"""WebsiteScraper — general public website actor (brief §26).

Capabilities: crawl permitted pages of ONE target domain (BFS, depth- and
page-limited), extract title / text / links / publicly displayed emails and
phones / social links / metadata. robots.txt respected by the shared HTTP
policy. Private, authenticated or security-bypassed content is NEVER accessed.

Streaming (§25): items are yielded per page as they are parsed; the checkpoint
cursor is the frontier (queue of pending URLs), so pause/crash resumes without
re-crawling. Same-domain restriction (§42): external links are recorded as
metadata only and never crawled.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from app.scrapers.actors.website.parser import (
    extract_address_hint,
    extract_emails,
    extract_links,
    extract_phones,
    extract_social_links,
    meta_description,
    page_title,
    visible_text,
)
from app.scrapers.actors.website.schemas import OUTPUT_FIELDS, WebsiteInput
from app.scrapers.core.base import ActorCategory, ScraperActor
from app.scrapers.core.exceptions import ScraperBlockedTargetError, ScraperNetworkError, ScraperValidationError
from app.scrapers.core.netguard import canonical_url, in_same_domain

# Pages that most often contain public contact information — tried early so
# short jobs (max_pages small) still find contacts.
_CONTACT_HINTS = (
    "/contact", "/contact-us", "/about", "/about-us", "/impressum",
    "/support", "/help", "/team", "/kontakt",
)


class WebsiteActor(ScraperActor):
    id = "website"
    name = "Website Scraper"
    version = "1.0.0"
    description = (
        "Crawl one public website (same domain only) and extract publicly "
        "available contact information: emails, phone numbers, social links "
        "and page metadata."
    )
    category = ActorCategory.WEBSITE
    author = "QBIT"
    capabilities = (
        "same-domain crawling",
        "public email extraction",
        "public phone extraction",
        "social link extraction",
        "robots.txt respect",
        "depth and page limits",
    )
    supports_pause = True
    input_schema = WebsiteInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        from urllib.parse import urlsplit

        inp = WebsiteInput.model_validate(ctx.input)
        start = str(inp.url)
        target_host = urlsplit(start).hostname or ""
        if not target_host:
            raise ScraperValidationError("Input URL has no hostname")

        # netguard validate BEFORE anything else (SSRF, brief §41)
        from app.scrapers.core.netguard import validate_url_async

        start = await validate_url_async(start, ctx.url_policy)

        queued: list[tuple[int, str]] = [(0, start)]
        enqueued: set[str] = {canonical_url(start)}
        fetched: set[str] = set()
        for hint in _CONTACT_HINTS:
            hint_url = self._hint_url(start, hint)
            cu = canonical_url(hint_url)
            if cu not in enqueued:
                enqueued.add(cu)
                queued.append((1, hint_url))
        pages_fetched = int(ctx.checkpoint.data.get("pages_fetched", 0)) if ctx.checkpoint else 0
        frontier_cursor = ctx.checkpoint.data.get("frontier", []) if ctx.checkpoint else []
        if frontier_cursor:
            queued = [(int(d), u) for d, u in frontier_cursor]
            enqueued = {canonical_url(u) for _, u in queued}

        site_title: str | None = None
        site_meta: str | None = None

        while queued:
            await ctx.check_stopped()
            ctx.check_deadline()
            await ctx.save_checkpoint(
                {
                    "frontier": queued[:200],
                    "pages_fetched": pages_fetched,
                    "enqueued_count": len(enqueued) + len(fetched),
                }
            )
            if ctx.limits.pages_exceeded(pages_fetched):
                from app.scrapers.core.exceptions import ScraperLimitReachedError

                raise ScraperLimitReachedError(
                    f"max_pages limit reached ({ctx.limits.max_pages})"
                )

            depth, url = queued.pop(0)
            cu = canonical_url(url)
            if cu in fetched:
                continue
            fetched.add(cu)

            try:
                resp = await ctx.http.get_html(url)
            except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
                # single unreachable page must not kill the crawl (§26)
                await ctx.report("PAGE_FAILED", str(exc), {"url": url})
                pages_fetched += 1
                ctx.progress.add_page()
                continue

            pages_fetched += 1
            ctx.progress.add_page()
            ctx.progress.set_stage(f"crawling ({pages_fetched} pages)")
            await ctx.report("PAGE_FETCHED", None, {"url": url, "status": resp.status_code})
            if resp.status_code >= 400:
                continue

            html = resp.text
            soup = BeautifulSoup(html, "html.parser")
            if site_title is None:
                site_title = page_title(soup)
                site_meta = meta_description(soup)

            emails = extract_emails(html, soup) if inp.extract_emails else []
            phones = extract_phones(soup) if inp.extract_phones else []
            social = extract_social_links(soup) if inp.extract_social_links else {}

            if emails or phones or social:
                item = {
                    "business_name": site_title or target_host,
                    "email": emails[0] if emails else None,
                    "phone": phones[0] if phones else None,
                    "website": f"{urlsplit(start).scheme}://{urlsplit(start).netloc}",
                    "address": extract_address_hint(soup),
                    "source": self.id,
                    "source_url": str(resp.url),
                    "social_links": social,
                    "metadata": {
                        "page_title": page_title(soup),
                        "page_description": meta_description(soup),
                        "emails_found": emails,
                        "phones_found": phones,
                        "pages_crawled": pages_fetched,
                        "depth": depth,
                    },
                }
                if inp.extract_text:
                    item["metadata"]["page_text_sample"] = visible_text(soup, 2000)
                yield item

            # Enqueue same-domain links only (§42); external links are metadata.
            if depth < inp.max_depth and not ctx.limits.pages_exceeded(pages_fetched):
                for link in extract_links(soup, str(resp.url)):
                    cu2 = canonical_url(link)
                    if in_same_domain(link, start) and cu2 not in enqueued and cu2 not in fetched:
                        enqueued.add(cu2)
                        queued.append((depth + 1, link))

        await ctx.save_checkpoint({"frontier": [], "pages_fetched": pages_fetched}, force=True)

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _hint_url(start: str, hint: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(start)
        return f"{parts.scheme}://{parts.netloc}{hint}"

    async def cleanup(self, ctx) -> None:
        await ctx.close()
