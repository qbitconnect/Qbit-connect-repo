"""Layered extraction toolkit (Actor Platform spec §5, §27).

Generic, source-agnostic helpers shared by all actors:
  1. semantic selectors  — `first_text`/`first_attr` with FALLBACK LISTS
  2. structured data     — JSON-LD (incl. @graph), OpenGraph/Twitter meta
  3. embedded JSON       — __NEXT_DATA__, window._sharedData, custom markers
  4. text patterns       — public emails / phones in the page text
  5. link harvesting     — absolute-ized <a href> inventory

Nothing here talks to the network, touches the database, or knows about
specific websites. Parsers stay pure → deterministic unit tests with fixture
HTML (spec §39). Bounded output everywhere (caps on list sizes / lengths).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

MAX_TEXT = 1000
MAX_LIST = 50

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)
PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\-\s.])?(?:\(\d{2,5}\)|\d{2,5})(?:[\-\s.]\d{2,5}){1,4}"
    r"|\+?\d{10,14}"
)
_DATE_LIKE_RE = re.compile(
    r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}([ T]\d{2}:\d{2}(:\d{2})?)?$"
)

#: markers that a page is a login/anti-bot wall rather than real content
BLOCK_MARKERS = (
    "accounts/login",
    "login • instagram",
    "authwall",
    "sign up to see",
    "please verify",
    "access denied",
    "unusual traffic",
    "captcha",
)


def first_text(soup: BeautifulSoup, selectors: list[str], *, limit: int = MAX_TEXT) -> str | None:
    """First non-empty text across a FALLBACK selector list (spec §27)."""
    for sel in selectors:
        try:
            node = soup.select_one(sel)
        except Exception:  # noqa: BLE001 — invalid selector = skip, never crash
            continue
        if node is None:
            continue
        value = node.get_text(" ", strip=True)
        if value:
            return value[:limit]
    return None


def first_attr(soup: BeautifulSoup, selectors: list[str], attr: str) -> str | None:
    for sel in selectors:
        try:
            node = soup.select_one(sel)
        except Exception:  # noqa: BLE001
            continue
        if node is None:
            continue
        value = node.get(attr)
        if value:
            return str(value).strip()[:MAX_TEXT]
    return None


def all_text(soup: BeautifulSoup, selectors: list[str], *, cap: int = MAX_LIST) -> list[str]:
    """Text of EVERY match for each selector (deduped, order preserved)."""
    out: list[str] = []
    for sel in selectors:
        try:
            nodes = soup.select(sel)
        except Exception:  # noqa: BLE001
            continue
        for node in nodes:
            value = node.get_text(" ", strip=True)
            if value and value not in out:
                out.append(value[:MAX_TEXT])
            if len(out) >= cap:
                return out
    return out


# ------------------------------------------------------------------ meta/JSON-LD
def extract_meta(soup: BeautifulSoup) -> dict[str, str]:
    """OpenGraph + Twitter + basic meta, property/name → content."""
    out: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        content = tag.get("content")
        if key and content:
            out[str(key).lower()] = str(content).strip()[:MAX_TEXT]
    return out


def _jsonld_nodes(data: Any, out: list[dict]) -> None:
    if isinstance(data, dict):
        # pure @graph containers are unwrapped — their nodes are the payload
        if set(data.keys()) <= {"@context", "@graph", "@id"} and isinstance(data.get("@graph"), list):
            for node in data["@graph"]:
                _jsonld_nodes(node, out)
        else:
            out.append(data)
            graph = data.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    _jsonld_nodes(node, out)
    elif isinstance(data, list):
        for node in data:
            _jsonld_nodes(node, out)


def extract_jsonld(soup: BeautifulSoup) -> list[dict]:
    """All JSON-LD objects (script[type=application/ld+json]), @graph-aware."""
    nodes: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        _jsonld_nodes(data, nodes)
        if len(nodes) >= MAX_LIST:
            break
    return nodes[:MAX_LIST]


def jsonld_by_type(nodes: list[dict], *types: str) -> list[dict]:
    wanted = {t.lower() for t in types}
    return [
        n for n in nodes
        if isinstance(n.get("@type"), str) and n["@type"].lower() in wanted
        or isinstance(n.get("@type"), list)
        and any(str(t).lower() in wanted for t in n["@type"])
    ]


# ------------------------------------------------------------------ embedded JSON
def extract_embedded_json(html: str, markers: list[str], *, cap: int = 5) -> list[dict]:
    """Parse JSON blobs embedded in <script> tags (spec §5 'embedded JSON').

    Strategy per script tag: if a MARKER occurs in the tag's content, strip a
    leading assignment (`window.x =`, `var x =`) or raw JSON and parse the
    largest balanced {...} / [...] block. Returns parsed objects (dict|list
    roots; dicts kept). Deterministic, no JS execution.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict] = []
    lowered_markers = [m.lower() for m in markers]
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if not text or len(text) < 20:
            continue
        low = text.lower()
        if not any(m in low for m in lowered_markers):
            continue
        for candidate in _json_candidates(text):
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, list):
                for item in data[:MAX_LIST]:
                    if isinstance(item, dict):
                        found.append(item)
            elif isinstance(data, dict):
                found.append(data)
            break
        if len(found) >= cap:
            break
    return found


def _json_candidates(text: str) -> list[str]:
    """Balanced-brace / bracket substrings, longest first (bounded)."""
    out: list[str] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1 and len(out) < 4:
            depth = 0
            in_str = False
            esc = False
            end = -1
            for i in range(start, min(len(text), start + 4_000_000)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end != -1:
                out.append(text[start:end])
            start = text.find(opener, (end if end != -1 else start) + 1)
    return out


def dig(obj: Any, *path: str) -> Any:
    """Safe deep getter for parsed JSON ('a.b.0.c' style indices)."""
    cur = obj
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
            cur = cur[int(key)]
        else:
            return None
    return cur


# ------------------------------------------------------------------ text patterns
def extract_emails(text: str, *, cap: int = 10) -> list[str]:
    seen: list[str] = []
    for match in EMAIL_RE.findall(text or ""):
        value = match.strip(".").lower()
        if value not in seen and not _looks_non_email(value):
            seen.append(value)
        if len(seen) >= cap:
            break
    return seen


def _looks_non_email(value: str) -> bool:
    """Filter common false positives (files, images, css-ish tokens)."""
    return value.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js"))


def extract_phones(text: str, *, cap: int = 10) -> list[str]:
    """Publicly DISPLAYED phone-looking strings, longest/most complete first.
    Presentation-level only: values must look like real numbers (8-14 digits,
    standard group separators); dates/timestamps are filtered out. The
    platform normalizer does the authoritative E.164 pass later."""
    out: list[str] = []
    for match in PHONE_RE.findall(text or ""):
        value = match.strip()
        if _DATE_LIKE_RE.match(value):
            continue
        digits = re.sub(r"\D", "", value)
        if len(digits) < 8 or len(digits) > 14:
            continue
        if value not in out:
            out.append(value[:40])
        if len(out) >= cap:
            break
    return out


# ------------------------------------------------------------------ links
def extract_links(soup: BeautifulSoup, base_url: str, *, cap: int = MAX_LIST) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(
            {
                "url": absolute[:MAX_TEXT],
                "text": a.get_text(" ", strip=True)[:200] or None,
            }
        )
        if len(out) >= cap:
            break
    return out


def looks_blocked(html: str, soup: BeautifulSoup | None = None) -> str | None:
    """Detect login/anti-bot walls. Returns the marker hit or None.

    Used to FAIL a run honestly (spec §36/§42): when the target serves a wall
    instead of content, the actor raises ScraperBlockedTargetError instead of
    pretending the scrape succeeded with zero results.
    """
    haystack = (html or "")[:200000].lower()
    for marker in BLOCK_MARKERS:
        if marker in haystack:
            return marker
    if soup is not None:
        title = (soup.title.get_text(" ", strip=True).lower() if soup.title else "")
        for marker in ("login • instagram", "authwall", "log in to continue"):
            if marker in title:
                return marker
    return None
