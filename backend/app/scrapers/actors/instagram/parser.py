"""Instagram public-page parser — pure functions over fetched HTML (§7.A).

Layered strategy on the LOGGED-OUT public profile/tag page:
  1. JSON-LD (Person/SocialMediaPosting blocks when served)
  2. OpenGraph/Twitter meta (og:title / og:description carry the public
     follower/following/post counters and the biography snippet)
  3. Embedded JSON blobs (_sharedData / __NEXT_DATA__ style) when present
  4. Text patterns for public contact info (business email/phone that the
     account itself displays publicly)

Privacy rule (spec §7.A): ONLY public surfaces are parsed. Nothing here
attempts login, private data, or de-obfuscation beyond what the page itself
publicly renders.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.scrapers.core.extraction import (
    dig,
    extract_emails,
    extract_jsonld,
    extract_meta,
    extract_phones,
    first_text,
    jsonld_by_type,
)

FOLLOWERS_RE = re.compile(r"([\d,.]+)\s*(m|k)?\s*followers", re.IGNORECASE)
FOLLOWING_RE = re.compile(r"([\d,.]+)\s*(m|k)?\s*following", re.IGNORECASE)
POSTS_RE = re.compile(r"([\d,.]+)\s*(m|k)?\s*posts", re.IGNORECASE)


def _count(raw: str | None, unit: str | None) -> int | None:
    if not raw:
        return None
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    mult = {"m": 1_000_000, "k": 1_000}.get((unit or "").lower(), 1)
    return int(value * mult)


def parse_follow_counters(text: str | None) -> dict[str, int | None]:
    if not text:
        return {"followers": None, "following": None, "posts": None}
    return {
        "followers": _count(*FOLLOWERS_RE.search(text).groups()) if FOLLOWERS_RE.search(text) else None,
        "following": _count(*FOLLOWING_RE.search(text).groups()) if FOLLOWING_RE.search(text) else None,
        "posts": _count(*POSTS_RE.search(text).groups()) if POSTS_RE.search(text) else None,
    }


def parse_profile(html: str, *, username: str, source_url: str) -> dict | None:
    """Build a lead-shaped record from a public profile page. Returns None
    when nothing usable was parsed (caller decides what that means)."""
    soup = BeautifulSoup(html, "html.parser")
    meta = extract_meta(soup)
    og_title = meta.get("og:title")
    og_desc = meta.get("og:description") or meta.get("description")
    display_name = None
    if og_title:
        # og:title is usually 'Name (@username) • Instagram photos and videos'
        display_name = re.split(r"\s*\(@", og_title)[0].strip() or None
        if display_name and display_name.lower().endswith("• instagram"):
            display_name = None
    counters = parse_follow_counters(og_desc)
    biography = first_text(soup, ["meta[name=description]::-webkit-outer", "span[class*=bio]"])
    # og:description's tail after the counters often holds the public bio
    bio = biography or (og_desc.split("•")[-1].strip() if og_desc and "•" in og_desc else None)

    jsonld = jsonld_by_type(extract_jsonld(soup), "person", "socialmediaposting", "profilepage")
    ld = jsonld[0] if jsonld else {}
    website = (
        dig(ld, "sameAs") if isinstance(dig(ld, "sameAs"), str) else None
    ) or meta.get("og:url")

    emails = extract_emails(og_desc or "") if og_desc else []
    phones = extract_phones(og_desc or "") if og_desc else []

    if not display_name and not counters["followers"]:
        return None

    metadata = {
        "record_type": "profile",
        "platform": "instagram",
        "username": username,
        "display_name": display_name,
        "followers": counters["followers"],
        "following": counters["following"],
        "posts_count": counters["posts"],
        "biography": (bio or "")[:1000] or None,
        "verified": bool(ld.get("verificationStatus")) or None,
        "og_description": og_desc,
        "profile_url": source_url,
    }
    return {
        "business_name": display_name or username,
        "website": website,
        "email": emails[0] if emails else None,
        "phone": phones[0] if phones else None,
        "source": "instagram",
        "source_url": source_url,
        "metadata": {k: v for k, v in metadata.items() if v is not None},
    }


def parse_embedded_posts(embedded: list[dict], *, username: str, cap: int) -> list[dict]:
    """Post-shaped records from embedded JSON (when Instagram serves it)."""
    out: list[dict] = []
    for blob in embedded:
        # shortcode-carrying nodes appear under various paths; search shallow
        candidates: list[dict] = []
        for edge_key in ("edge_owner_to_timeline_media", "edges", "xdt_api__v1__feed__user_timeline_graphql_connection"):
            node = dig(blob, *edge_key.split("."))
            if isinstance(node, dict) and isinstance(node.get("edges"), list):
                candidates.extend(e.get("node", {}) for e in node["edges"] if isinstance(e, dict))
            elif isinstance(node, list):
                candidates.extend(item for item in node if isinstance(item, dict))
        for node in candidates:
            shortcode = node.get("shortcode") or dig(node, "code")
            if not shortcode:
                continue
            caption = dig(node, "edge_media_to_caption", "edges", "0", "node", "text") or node.get("caption")
            out.append(
                {
                    "business_name": f"{username} post {shortcode}",
                    "source": "instagram",
                    "source_url": f"https://www.instagram.com/p/{shortcode}/",
                    "metadata": {
                        "record_type": "post",
                        "platform": "instagram",
                        "username": username,
                        "post_id": str(node.get("id") or shortcode),
                        "caption": (str(caption)[:1000] if caption else None),
                        "likes": dig(node, "edge_media_preview_like", "count") or node.get("like_count"),
                        "comments": dig(node, "edge_media_to_comment", "count") or node.get("comment_count"),
                        "media_type": str(node.get("__typename") or node.get("media_type") or "") or None,
                        "taken_at": node.get("taken_at") or node.get("taken_at_timestamp"),
                    },
                }
            )
            if len(out) >= cap:
                return out
    return out


def parse_hashtag_page(html: str, *, tag: str, source_url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = extract_meta(soup)
    og_desc = meta.get("og:description")
    counters = parse_follow_counters(og_desc) if og_desc else {}
    title = meta.get("og:title") or first_text(soup, ["h1", "title"])
    media_count = dig(next(iter(extract_jsonld(soup)), {}), "interactionStatistic", "userInteractionCount")
    if not title and not media_count:
        return None
    return {
        "business_name": f"#{tag}",
        "source": "instagram",
        "source_url": source_url,
        "metadata": {
            "record_type": "hashtag",
            "platform": "instagram",
            "hashtag": tag,
            "title": title,
            "media_count": media_count or counters.get("posts"),
            "og_description": og_desc,
        },
    }
