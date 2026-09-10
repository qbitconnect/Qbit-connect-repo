"""Meta Ads Library Actor (spec §7.B).

Public, logged-out Ad Library surfaces only. Ads are parsed from the page's
embedded JSON when Facebook serves it; a login/anti-bot wall fails the run
honestly with TARGET_BLOCKED. No authentication, no evasion, ever.

AD CHANGE DETECTION (spec §7.B): each ad carries a deterministic
`content_hash`; runs are snapshots (one dataset each), and the platform
compares consecutive snapshots (new/unchanged/modified/stopped/resumed) via
the dataset changes endpoint.
"""

from __future__ import annotations

from app.scrapers.actors.meta_ads_library.parser import build_search_url, parse_ads, parse_page_summary
from app.scrapers.actors.meta_ads_library.schemas import OUTPUT_FIELDS, MetaAdsInput
from app.scrapers.core.base import ActorCategory, ActorHealth, ScraperActor
from app.scrapers.core.exceptions import (
    ScraperBlockedTargetError,
    ScraperNetworkError,
    ScraperValidationError,
)
from app.scrapers.core.netguard import validate_url_async


class MetaAdsLibraryActor(ScraperActor):
    id = "meta-ads-library"
    name = "Meta Ads Library"
    version = "1.0.0"
    description = (
        "Extract publicly archived ads from the Meta Ad Library (Facebook / "
        "Instagram / Messenger / Threads surfaces): advertiser, copy, CTA, "
        "landing URL, media, platforms, dates and the permanent archive URL. "
        "Runs on the public logged-out library; blocks are reported honestly. "
        "Every run is a snapshot — compare runs to detect new / modified / "
        "stopped ads."
    )
    category = ActorCategory.ADS
    capabilities = (
        "ad library keyword / page / URL search",
        "ad copy, CTA, landing URL, media",
        "advertiser + page identity",
        "platforms and date bounds",
        "permanent archive URL per ad",
        "snapshot hashing for change detection",
    )
    supports_pause = True
    input_schema = MetaAdsInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = MetaAdsInput.model_validate(ctx.input)
        policy_errors = inp.validate_policy()
        if policy_errors:
            raise ScraperValidationError("; ".join(f"{k}: {v}" for k, v in policy_errors.items()))

        target = str(inp.ad_library_url) if inp.ad_library_url else build_search_url(
            keyword=inp.keyword, country=inp.country,
            active_status=inp.active_status, page_name=inp.page_name,
        )
        await ctx.check_stopped()
        ctx.progress.set_stage("fetching ad library")
        try:
            checked = await validate_url_async(target, ctx.url_policy)
            resp = await ctx.http.get_html(checked)
        except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
            raise ScraperBlockedTargetError(f"Ad Library unreachable: {exc.message}") from exc
        if resp.status_code >= 400:
            raise ScraperBlockedTargetError(
                f"Meta returned HTTP {resp.status_code} — the Ad Library page "
                "is not publicly accessible from this network."
            )
        ads, block = parse_ads(resp.text, cap=inp.max_ads)
        if block:
            raise ScraperBlockedTargetError(
                f"Meta served a login/verification wall ({block!r}); no ads were parsed."
            )
        source_summary = None
        produced = 0
        for ad in ads:
            await ctx.check_stopped()
            ctx.check_record_limit(produced)
            produced += 1
            yield {
                "business_name": ad.get("page_name") or f"Advertiser {ad['ad_id']}",
                "website": ad.get("landing_url"),
                "source": self.id,
                "source_url": str(resp.url),
                "metadata": {
                    "record_type": "ad",
                    "platform": "meta_ads",
                    "ad_id": ad["ad_id"],
                    "archive_url": ad.get("archive_url"),
                    "body": ad.get("body"),
                    "cta": ad.get("cta"),
                    "image_urls": ad.get("image_urls") or [],
                    "video_urls": ad.get("video_urls") or [],
                    "platforms": ad.get("platforms") or [],
                    "start_date": ad.get("start_date"),
                    "end_date": ad.get("end_date"),
                    "content_hash": ad.get("content_hash"),
                    "change_key": f"meta-ad:{ad['ad_id']}",
                },
            }
        if produced == 0:
            source_summary = parse_page_summary(resp.text)
            if source_summary is None:
                raise ScraperBlockedTargetError(
                    "No ads and no page summary could be parsed from the "
                    "public Ad Library response — the structure may have "
                    "changed or the query matched nothing. Nothing was fabricated."
                )
            yield {
                "business_name": source_summary["title"],
                "source": self.id,
                "source_url": str(resp.url),
                "metadata": {
                    "record_type": "page_summary",
                    "platform": "meta_ads",
                    "query": inp.keyword or inp.page_name or str(inp.ad_library_url),
                    "description": source_summary.get("description"),
                    "ads_parsed": 0,
                },
            }
        await ctx.save_checkpoint({"target": target, "ads": produced}, force=True)

    async def health_check(self) -> ActorHealth:
        return ActorHealth(
            status="READY",
            detail="Public Ad Library only; walls are reported as blocked runs.",
            dependencies={},
        )
