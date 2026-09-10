"""LinkedIn Public Intelligence Actor (spec §7.C).

PUBLIC logged-out pages only. Most deep LinkedIn content is behind an
authwall — that outcome FAILS the run with TARGET_BLOCKED and the reason is
visible in run logs. No login is required by default, no passwords are ever
stored or requested, no account cookies are used, and no authentication or
security control is bypassed (spec §36 hard boundary).
"""

from __future__ import annotations

from app.scrapers.actors.linkedin.parser import parse_company, parse_profile
from app.scrapers.actors.linkedin.schemas import OUTPUT_FIELDS, LinkedInInput
from app.scrapers.core.base import ActorCategory, ActorHealth, ScraperActor
from app.scrapers.core.exceptions import (
    ScraperBlockedTargetError,
    ScraperNetworkError,
    ScraperValidationError,
)
from app.scrapers.core.extraction import looks_blocked
from app.scrapers.core.netguard import validate_url_async


class LinkedInActor(ScraperActor):
    id = "linkedin-public"
    name = "LinkedIn Public Intelligence"
    version = "1.0.0"
    description = (
        "Public, logged-out LinkedIn pages: company pages (name, tagline, "
        "industry hints, employee-range hints) and public profiles (name, "
        "headline). When LinkedIn serves an authwall the run fails honestly "
        "with TARGET_BLOCKED — the platform never logs in, never uses "
        "cookies, and never bypasses access controls."
    )
    category = ActorCategory.SOCIAL_MEDIA
    capabilities = (
        "public company pages",
        "public profile headline/name",
        "bulk public URLs",
        "company slug discovery",
        "authwall detection (honest block reporting)",
    )
    supports_pause = True
    input_schema = LinkedInInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = LinkedInInput.model_validate(ctx.input)
        targets = inp.target_urls()
        if not targets:
            raise ScraperValidationError("provide url, urls or company_slug")
        mode = inp.mode if inp.mode in ("profile", "company") else "company"

        produced = 0
        walled = 0
        for target in targets:
            await ctx.check_stopped()
            ctx.check_deadline()
            ctx.check_page_limit(ctx.progress.pages_fetched)
            ctx.progress.set_stage(f"fetching {target.split('/company/')[-1][:60]}")
            try:
                checked = await validate_url_async(target, ctx.url_policy)
                resp = await ctx.http.get_html(checked)
            except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
                await ctx.report("PAGE_FAILED", str(exc), {"url": target})
                walled += 1
                continue
            if resp.status_code in (999, 401, 403) or resp.status_code >= 400:
                await ctx.report(
                    "TARGET_BLOCKED",
                    f"LinkedIn returned HTTP {resp.status_code} (authwall/limit)",
                    {"url": target, "status": resp.status_code},
                )
                walled += 1
                continue
            blocked = looks_blocked(resp.text)
            if blocked:
                await ctx.report(
                    "TARGET_BLOCKED",
                    f"LinkedIn served an authwall ({blocked!r})",
                    {"url": target},
                )
                walled += 1
                continue
            parser = parse_profile if mode == "profile" else parse_company
            record = parser(resp.text, source_url=str(resp.url))
            if record is None:
                await ctx.report("PARSER_EMPTY", f"No public content parsed for {target}", {})
                continue
            produced += 1
            yield record
            await ctx.save_checkpoint({"last": target, "produced": produced})

        if produced == 0:
            if walled:
                raise ScraperBlockedTargetError(
                    f"LinkedIn served authwalls/limits for all {walled} target(s); "
                    "public content was not accessible logged-out. Nothing fabricated."
                )
            raise ScraperValidationError(
                "No public LinkedIn content could be parsed for any target."
            )

    async def health_check(self) -> ActorHealth:
        return ActorHealth(
            status="READY",
            detail=(
                "Logged-out public pages only; authwalls are expected on many "
                "networks and are reported as blocked, never bypassed."
            ),
            dependencies={},
        )
