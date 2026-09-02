"""EmailFinderActor — public business email discovery (brief §27).

Crawls the target site's most likely contact pages (same domain only) and
extracts publicly displayed email addresses with provenance (source page) and
a heuristic type + confidence classification.

IMPORTANT (§27): a scraped email is NOT marketing consent. Provenance is kept
on every record (`source_url`, `metadata.source_page`); consent must be
established separately by the operator. Only publicly displayed addresses are
collected — never private or obfuscated ones, and no evasion of any control.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.scrapers.actors.email_finder.schemas import OUTPUT_FIELDS, EmailFinderInput
from app.scrapers.actors.website.parser import extract_emails, page_title
from app.scrapers.core.base import ActorCategory, ScraperActor
from app.scrapers.core.exceptions import ScraperBlockedTargetError, ScraperNetworkError, ScraperValidationError
from app.scrapers.core.netguard import canonical_url, in_same_domain, validate_url_async

_CONTACT_HINTS = (
    "/contact", "/contact-us", "/kontakt", "/about", "/about-us", "/impressum",
    "/support", "/help", "/team", "/sales", "/legal", "/privacy",
)
# (type, confidence) by local-part keyword — deterministic, documented heuristics.
_LOCAL_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("sales", "HIGH", ("sales", "vertrieb", "buy", "shop", "orders")),
    ("support", "HIGH", ("support", "help", "helpdesk", "service", "care")),
    ("info", "HIGH", ("info", "information", "office", "hello", "hallo")),
    ("contact", "HIGH", ("contact", "kontakt", "reach", "connect")),
    ("general", "MEDIUM", ("mail", "email", "hi")),
)
_FREE_HOSTS = (
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com",
    "icloud.com", "protonmail.com", "gmx.de", "web.de", "mail.com",
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def classify_email(email: str, source_page: str, site_domain: str) -> tuple[str, str]:
    """(type, confidence) — deterministic heuristics (brief §27 types)."""
    local = email.split("@")[0].lower()
    host = email.split("@")[-1].lower()
    for email_type, confidence, keywords in _LOCAL_RULES:
        if any(k in local for k in keywords):
            return email_type, confidence
    if host.endswith(site_domain) or site_domain.endswith(host):
        return "general", "MEDIUM"
    if host in _FREE_HOSTS:
        return "other", "LOW"
    if "contact" in source_page:
        return "contact", "MEDIUM"
    return "other", "LOW"


class EmailFinderActor(ScraperActor):
    id = "email-finder"
    name = "Email Finder"
    version = "1.0.0"
    description = (
        "Discover publicly displayed business contact email addresses on a "
        "website, with source-page provenance and a heuristic type/confidence "
        "classification. Scraped email does not imply marketing consent."
    )
    category = ActorCategory.EMAIL
    author = "QBIT"
    capabilities = (
        "public email discovery",
        "contact page prioritization",
        "type + confidence classification",
        "source-page provenance",
        "same-domain restriction",
        "robots.txt respect",
    )
    supports_pause = True
    input_schema = EmailFinderInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = EmailFinderInput.model_validate(ctx.input)

        if inp.website is not None:
            start = str(inp.website)
        else:
            start = f"https://{inp.domain}"
        try:
            start = await validate_url_async(start, ctx.url_policy)
        except ScraperValidationError:
            if inp.website is None:
                # bare domain guess failed (NXDOMAIN/private) — clear message
                raise ScraperValidationError(
                    f"Could not validate https://{inp.domain} as a safe public target"
                )
            raise
        from urllib.parse import urlsplit

        site_domain = (urlsplit(start).hostname or "").removeprefix("www.")
        base = f"{urlsplit(start).scheme}://{urlsplit(start).netloc}"

        queued: list[tuple[int, str]] = [(0, start)]
        enqueued: set[str] = {canonical_url(start)}
        fetched: set[str] = set()
        for hint in _CONTACT_HINTS:
            hint_url = f"{base}{hint}"
            cu = canonical_url(hint_url)
            if cu not in enqueued:
                enqueued.add(cu)
                queued.append((1, hint_url))
        pages_fetched = int(ctx.checkpoint.data.get("pages_fetched", 0)) if ctx.checkpoint else 0
        frontier = ctx.checkpoint.data.get("frontier") if ctx.checkpoint else None
        if frontier:
            queued = [(int(d), u) for d, u in frontier]
            enqueued = {canonical_url(u) for _, u in queued}
        found_emails: set[str] = set(
            ctx.checkpoint.data.get("found_emails", []) if ctx.checkpoint else []
        )
        business_name: str | None = None

        while queued:
            await ctx.check_stopped()
            ctx.check_deadline()
            await ctx.save_checkpoint(
                {
                    "frontier": queued[:200],
                    "pages_fetched": pages_fetched,
                    "found_emails": sorted(found_emails)[:500],
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
            pages_fetched += 1
            ctx.progress.add_page()
            ctx.progress.set_stage(f"scanning ({pages_fetched} pages)")
            try:
                resp = await ctx.http.get_html(url)
            except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
                await ctx.report("PAGE_FAILED", str(exc), {"url": url})
                continue
            await ctx.report("PAGE_FETCHED", None, {"url": url})
            if resp.status_code >= 400:
                continue

            html = resp.text
            soup = BeautifulSoup(html, "html.parser")
            if business_name is None:
                business_name = page_title(soup) or site_domain

            page_emails = [
                e for e in extract_emails(html, soup)
                if e not in found_emails and _EMAIL_RE.fullmatch(e)
            ]
            for email in page_emails:
                found_emails.add(email)
                email_type, confidence = classify_email(email, url, site_domain)
                yield {
                    "business_name": business_name,
                    "email": email,
                    "website": base,
                    "source": self.id,
                    "source_url": str(resp.url),
                    "metadata": {
                        "source_page": str(resp.url),
                        "email_type": email_type,
                        "confidence": confidence,
                        "pages_scanned": pages_fetched,
                        "consent": "not_implied",
                    },
                }

            if depth < inp.crawl_depth:
                for link in _contact_links(soup, str(resp.url), start):
                    cu2 = canonical_url(link)
                    if cu2 not in enqueued and cu2 not in fetched:
                        enqueued.add(cu2)
                        queued.append((depth + 1, link))

        await ctx.save_checkpoint({"frontier": [], "pages_fetched": pages_fetched}, force=True)

    async def cleanup(self, ctx) -> None:
        await ctx.close()


def _contact_links(soup: BeautifulSoup, base_url: str, start_url: str) -> list[str]:
    from app.scrapers.core.netguard import normalize_url

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        raw = str(a["href"]).strip()
        if not raw or raw.startswith(("#", "javascript:", "data:", "tel:", "mailto:")):
            continue
        absolute = normalize_url(base_url, raw)
        if in_same_domain(absolute, start_url) and any(
            h in absolute.lower() for h in ("contact", "about", "support", "impressum", "team")
        ):
            links.append(absolute)
    return links[:30]
