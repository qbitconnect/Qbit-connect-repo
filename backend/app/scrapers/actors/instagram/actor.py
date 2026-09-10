"""Instagram Intelligence Actor (spec §7.A).

PUBLIC data only, logged-out web surfaces:
  profile / posts / comments / hashtag / search modes over instagram.com's
  public pages. When Instagram serves a login wall instead of content, the
  run FAILS with TARGET_BLOCKED and the reason is visible in the UI (spec
  §36/§42 — no silent pretending).

No login, no cookies, no CAPTCHA handling, no private data — ever.
"""

from __future__ import annotations

from app.scrapers.actors.instagram.parser import (
    parse_embedded_posts,
    parse_hashtag_page,
    parse_profile,
)
from app.scrapers.actors.instagram.schemas import OUTPUT_FIELDS, InstagramInput, InstagramMode
from app.scrapers.core.base import ActorCategory, ActorHealth, ScraperActor
from app.scrapers.core.exceptions import (
    ScraperBlockedTargetError,
    ScraperNetworkError,
    ScraperValidationError,
)
from app.scrapers.core.extraction import dig, extract_embedded_json, looks_blocked
from app.scrapers.core.netguard import validate_url_async

BASE = "https://www.instagram.com"
EMBEDDED_MARKERS = ("_sharedData", "__NEXT_DATA__", "xdt_api__v1", "edge_owner_to_timeline_media")


class InstagramActor(ScraperActor):
    id = "instagram"
    name = "Instagram Intelligence"
    version = "1.0.0"
    description = (
        "Public Instagram intelligence: profile pages, posts, hashtag pages "
        "and discovery from logged-out web surfaces. Collects ONLY what "
        "Instagram publicly displays — follower counters, public bio, "
        "publicly displayed business contact info. Login walls are reported "
        "honestly as blocked runs; no authentication is ever attempted."
    )
    category = ActorCategory.SOCIAL_MEDIA
    capabilities = (
        "public profile intelligence (followers/bio/category)",
        "public posts + captions where served",
        "hashtag pages",
        "topsearch discovery (public endpoint)",
        "bulk usernames",
        "public business contact extraction",
        "login-wall detection (honest block reporting)",
    )
    supports_pause = True
    input_schema = InstagramInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = InstagramInput.model_validate(ctx.input)
        mode = inp.mode
        if mode == InstagramMode.HASHTAG and not inp.hashtag:
            raise ScraperValidationError("hashtag is required for hashtag mode")
        if mode == InstagramMode.SEARCH and not inp.keyword:
            raise ScraperValidationError("keyword is required for search mode")
        if mode in (InstagramMode.PROFILE, InstagramMode.POSTS) and not inp.target_usernames() and not inp.profile_url:
            raise ScraperValidationError("username or profile_url is required")

        if inp.profile_url and mode in (InstagramMode.PROFILE, InstagramMode.POSTS):
            targets = [str(inp.profile_url)]
            names = [inp.target_usernames()[0] if inp.target_usernames() else _username_from_url(str(inp.profile_url))]
        elif mode == InstagramMode.HASHTAG:
            targets = [f"{BASE}/explore/tags/{inp.hashtag}/"]
            names = [inp.hashtag]
        elif mode == InstagramMode.SEARCH:
            targets = [f"{BASE}/web/search/topsearch/?query={inp.keyword}"]
            names = [inp.keyword]
        else:
            targets = [f"{BASE}/{name}/" for name in inp.target_usernames()]
            names = list(inp.target_usernames())

        produced = 0
        for target, name in zip(targets, names):
            await ctx.check_stopped()
            ctx.check_deadline()
            ctx.check_page_limit(ctx.progress.pages_fetched)
            ctx.progress.set_stage(f"fetching {name}")
            try:
                checked = await validate_url_async(target, ctx.url_policy)
                resp = await ctx.http.get_html(checked)
            except (ScraperNetworkError, ScraperBlockedTargetError) as exc:
                await ctx.report("PAGE_FAILED", str(exc), {"url": target})
                continue
            if resp.status_code in (301, 302, 303, 307, 308):
                await ctx.report(
                    "TARGET_BLOCKED",
                    f"Redirected (likely login wall) for {name}",
                    {"url": target, "status": resp.status_code},
                )
                raise ScraperBlockedTargetError(
                    f"Instagram redirected the request for {name!r} — the public "
                    "page is not accessible logged-out from this network."
                )
            if resp.status_code >= 400:
                await ctx.report("PAGE_FAILED", f"HTTP {resp.status_code}", {"url": target})
                if resp.status_code == 404:
                    continue
                raise ScraperBlockedTargetError(
                    f"Instagram returned HTTP {resp.status_code} for {name!r}"
                )
            blocked = looks_blocked(resp.text)
            if blocked:
                raise ScraperBlockedTargetError(
                    f"Instagram served a login/verification wall ({blocked!r}) for {name!r}"
                )
            if mode == InstagramMode.SEARCH:
                async for rec in self._search_records(ctx, resp, str(inp.keyword)):
                    produced += 1
                    yield rec
                await ctx.save_checkpoint({"last_target": target, "produced": produced})
                continue
            if mode == InstagramMode.HASHTAG:
                record = parse_hashtag_page(resp.text, tag=inp.hashtag, source_url=str(resp.url))
                if record:
                    produced += 1
                    yield record
                continue
            record = parse_profile(resp.text, username=name, source_url=str(resp.url))
            if record is None:
                await ctx.report(
                    "PARSER_EMPTY",
                    f"Profile page for {name} served no parsable public content",
                    {"url": target},
                )
                continue
            produced += 1
            yield record
            if mode == InstagramMode.POSTS:
                embedded = extract_embedded_json(resp.text, list(EMBEDDED_MARKERS))
                for post in parse_embedded_posts(
                    embedded, username=name, cap=inp.max_results
                ):
                    await ctx.check_stopped()
                    produced += 1
                    yield post
            await ctx.save_checkpoint({"last_target": target, "produced": produced})

        if produced == 0:
            raise ScraperBlockedTargetError(
                "No public content could be retrieved — Instagram is not "
                "serving logged-out pages to this network, or no target "
                "resolved. Nothing was fabricated."
            )

    async def _search_records(self, ctx, resp, keyword: str) -> int:
        """Public topsearch JSON endpoint → discovery records (async gen)."""
        import json as _json

        try:
            data = _json.loads(resp.text)
        except ValueError:
            await ctx.report("PARSER_EMPTY", "search endpoint returned non-JSON", {})
            return
        users = dig_users(data)
        produced = 0
        for user in users:
            await ctx.check_stopped()
            produced += 1
            yield {
                "business_name": user.get("full_name") or user.get("username") or keyword,
                "source": self.id,
                "source_url": f"{BASE}/{user.get('username', '')}/",
                "metadata": {
                    "record_type": "search_result",
                    "platform": "instagram",
                    "query": keyword,
                    "username": user.get("username"),
                    "followers": _to_int(user.get("follower_count")),
                    "verified": bool(user.get("is_verified")) or None,
                    "private": bool(user.get("is_private")) or None,
                },
            }
            if produced >= 50:
                return

    async def health_check(self) -> ActorHealth:
        return ActorHealth(
            status="READY",
            detail=(
                "Public logged-out surfaces only; runs fail honestly with "
                "TARGET_BLOCKED when Instagram serves login walls."
            ),
            dependencies={},
        )


def dig_users(data) -> list[dict]:
    if isinstance(data, list):
        flat = data
    elif isinstance(data, dict):
        flat = (
            data.get("users")
            or dig(data, "users") or []
        )
    else:
        flat = []
    out = []
    for item in flat if isinstance(flat, list) else []:
        user = item.get("user") if isinstance(item, dict) else None
        if isinstance(user, dict):
            out.append(user)
    return out


def _username_from_url(url: str) -> str:
    parts = [p for p in url.split("/") if p]
    return parts[-1] if parts else url


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
