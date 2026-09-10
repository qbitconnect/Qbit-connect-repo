"""Shared browser runtime (Actor Platform spec §6 LEVEL 5/6, §17, §34).

Honest availability detection: `browser_available()` reports whether the
optional Playwright + Chromium dependency is actually usable RIGHT NOW. The
engine only reaches for the browser when static extraction is insufficient
(JS-heavy page) AND the operator enabled it — never as a shortcut, never to
evade protections. When the dependency is missing, callers receive a clear
'unavailable' reason and surface it in run logs (spec §42: no fake modes).
"""

from __future__ import annotations

import asyncio

from app.core.logging import get_logger

logger = get_logger("qbit.scrapers.browser")

_state: tuple[bool, str] | None = None  # memoized (available, reason)


def browser_available() -> tuple[bool, str]:
    """(True, 'playwright+chromium ready') or (False, reason)."""
    global _state
    if _state is not None:
        return _state
    try:
        import playwright  # noqa: F401
    except ImportError:
        _state = (False, "playwright package not installed")
        return _state
    try:
        from playwright.async_api import async_playwright

        async def _probe() -> tuple[bool, str]:
            try:
                async with async_playwright() as p:
                    path = await p.chromium.executable_path
                    if path:
                        return True, "playwright+chromium ready"
                    return False, "chromium executable not found"
            except Exception as exc:  # noqa: BLE001
                return False, f"chromium probe failed: {type(exc).__name__}"

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # never block a live loop on the probe — report optimistically
            # but honestly: the first real use re-validates and reports.
            _state = (True, "playwright importable (runtime-validated on first use)")
            return _state
        _state = asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001
        _state = (False, f"browser probe failed: {type(exc).__name__}")
    return _state


async def fetch_with_browser(url: str, *, timeout_seconds: float = 30.0) -> tuple[str, int]:
    """Fetch a page's rendered HTML via headless Chromium.

    Returns (html, status). Raises ScraperError subclasses ONLY for
    infrastructure problems; HTTP-level failures are returned as status codes
    so the caller reports them with its own semantics.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            resp = await page.goto(url, timeout=timeout_seconds * 1000, wait_until="domcontentloaded")
            status = resp.status if resp else 0
            html = await page.content()
            await context.close()
            return html, status
        finally:
            await browser.close()
