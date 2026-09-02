"""PolicyHttpClient — the ONLY network path actors may use (doc 08 §2, §4).

Enforces per-job resource policy (brief §17, §32, §43):
- per-host rate limit (token bucket) + optional inter-request delay
- global concurrency cap per job
- request timeout + hard response size cap (zip-bomb / huge-page protection)
- retry with exponential backoff + jitter for RETRYABLE conditions only
  (network errors, timeouts, 429/5xx) honoring Retry-After
- SSRF validation via netguard on the initial URL AND every redirect hop
- optional robots.txt awareness (urllib.robotparser), cached per host
- content-type sanity check for HTML/JSON/text

Forbidden by design (never implemented here or in actors): CAPTCHA solving,
anti-bot evasion, stealth fingerprinting, rate-limit evasion, IP rotation to
defeat platform restrictions, ban bypass.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.core.logging import get_logger
from app.scrapers.core.exceptions import (
    ScraperBlockedTargetError,
    ScraperNetworkError,
    ScraperTimeoutError,
)
from app.scrapers.core.netguard import UrlPolicy, validate_url

logger = get_logger("qbit.scrapers.http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_REDIRECTS = 5
CHUNK = 64 * 1024


@dataclass
class HttpPolicy:
    """Per-job network policy (defaults come from Settings, overridable via
    the job's advanced config)."""

    request_timeout: float = 20.0
    max_response_bytes: int = 5 * 1024 * 1024
    requests_per_second: float = 1.0
    min_delay: float = 0.0
    concurrency: int = 4
    max_retries: int = 3
    backoff_base: float = 1.5
    backoff_max: float = 30.0
    user_agent: str = "QBITConnect/0.3 (+self-hosted; respectful crawler)"
    respect_robots: bool = True

    @classmethod
    def from_settings(cls, settings, **overrides) -> "HttpPolicy":
        base = cls(
            request_timeout=float(settings.QBIT_SCRAPER_REQUEST_TIMEOUT_SECONDS),
            max_response_bytes=settings.QBIT_SCRAPER_MAX_RESPONSE_MB * 1024 * 1024,
            requests_per_second=settings.QBIT_SCRAPER_RPS_PER_HOST,
            concurrency=settings.QBIT_SCRAPER_CONCURRENCY,
            max_retries=settings.QBIT_SCRAPER_MAX_RETRIES,
            user_agent=settings.QBIT_SCRAPER_USER_AGENT,
        )
        for key, value in overrides.items():
            if value is None:
                continue
            if hasattr(base, key):
                setattr(base, key, value)
        return base


class _HostLimiter:
    """Token-bucket pacing per host + global concurrency."""

    def __init__(self, policy: HttpPolicy) -> None:
        self._policy = policy
        self._lock = asyncio.Lock()
        self._next_slot: dict[str, float] = {}
        self._sem = asyncio.Semaphore(max(1, policy.concurrency))

    async def acquire(self, host: str) -> None:
        await self._sem.acquire()
        async with self._lock:
            now = time.monotonic()
            interval = max(1.0 / max(self._policy.requests_per_second, 0.01), self._policy.min_delay)
            earliest = self._next_slot.get(host, 0.0)
            slot = max(now, earliest)
            self._next_slot[host] = slot + interval
        wait = slot - now
        if wait > 0:
            await asyncio.sleep(wait)

    def release(self) -> None:
        self._sem.release()


class _RobotsCache:
    def __init__(self, client: "PolicyHttpClient") -> None:
        self._client = client
        self._cache: dict[str, RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host not in self._cache:
            self._cache[host] = await self._fetch_robots(f"{parts.scheme}://{host}")
        robots = self._cache[host]
        if robots is None:
            return True  # no robots.txt → allowed
        try:
            return robots.can_fetch(self._client.policy.user_agent, url)
        except Exception:  # noqa: BLE001 - malformed robots must not crash jobs
            return True

    async def _fetch_robots(self, origin: str) -> RobotFileParser | None:
        url = f"{origin}/robots.txt"
        try:
            # Bypass the robots gate (no recursion) but keep ALL policy
            # enforcement: SSRF validation, size cap, retries off.
            from app.scrapers.core.netguard import validate_url

            validate_url(url, self._client.url_policy, resolve=False)
            resp = await self._client._request_once("GET", url)
        except Exception:  # noqa: BLE001 - unreachable robots.txt means allowed
            return None
        if resp.status_code != 200 or len(resp.content) > 512 * 1024:
            return None
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


@dataclass
class Response:
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding or "utf-8", errors="replace")
        except LookupError:  # unknown charset
            return self.content.decode("utf-8", errors="replace")

    @property
    def encoding(self) -> str | None:
        return self.headers.get("content-type", "").split("charset=")[-1].split(";")[0].strip().strip('"') or None

    def json(self):
        import json

        return json.loads(self.content)


class PolicyHttpClient:
    """Async, policy-enforcing HTTP client bound to one job.

    `transport` is a test hook (httpx.MockTransport) — production code never
    passes it; the mock-provider rule (§47) is honored the same way.
    """

    def __init__(self, policy: HttpPolicy, url_policy: UrlPolicy, *, transport=None) -> None:
        self.policy = policy
        self.url_policy = url_policy
        self._limiter = _HostLimiter(policy)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(policy.request_timeout),
            headers={"User-Agent": policy.user_agent, "Accept-Language": "en"},
            follow_redirects=False,  # redirects validated hop-by-hop (SSRF)
            transport=transport,
        )
        self.robots = _RobotsCache(self)
        self.request_count = 0
        self.bytes_received = 0
        self.error_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core
    async def get(self, url: str, **kwargs) -> Response:
        return await self.request("GET", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs) -> Response:
        url = validate_url(url, self.url_policy)
        if self.policy.respect_robots and not await self.robots.allowed(url):
            raise ScraperBlockedTargetError(f"Disallowed by robots.txt: {url}")

        delay = 0.5
        last_error: Exception | None = None
        for attempt in range(self.policy.max_retries + 1):
            await self._limiter.acquire(urlsplit(url).hostname or "")
            try:
                resp = await self._request_once(method, url, **kwargs)
                self.request_count += 1
                if resp.status_code in RETRYABLE_STATUS and attempt < self.policy.max_retries:
                    retry_after = float(resp.headers.get("retry-after") or 0)
                    delay = max(retry_after, min(delay * 2 * (1 + random.random() * 0.3), self.policy.backoff_max))
                    logger.info(
                        "Retryable HTTP status; backing off",
                        extra={"extra_fields": {"status": resp.status_code, "delay": round(delay, 2), "attempt": attempt + 1}},
                    )
                    await asyncio.sleep(delay)
                    continue
                return resp
            except (ScraperNetworkError, ScraperTimeoutError) as exc:
                self.error_count += 1
                last_error = exc
                if attempt >= self.policy.max_retries:
                    raise
                delay = min(delay * 2 * (1 + random.random() * 0.3), self.policy.backoff_max)
                logger.info(
                    "Transient error; retrying",
                    extra={"extra_fields": {"error": exc.message, "delay": round(delay, 2), "attempt": attempt + 1}},
                )
                await asyncio.sleep(delay)
            finally:
                self._limiter.release()
        raise last_error or ScraperNetworkError("Request failed")  # pragma: no cover

    async def _request_once(self, method: str, url: str, **kwargs) -> Response:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                req = self._client.build_request(method, current, **kwargs)
                resp = await self._client.send(req, stream=True)
            except httpx.TimeoutException as exc:
                raise ScraperTimeoutError(f"Request timed out: {current}") from exc
            except httpx.HTTPError as exc:
                raise ScraperNetworkError(f"{type(exc).__name__} requesting {current}") from exc

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                resp.close()
                if not location:
                    return Response(resp.status_code, dict(resp.headers), b"", current)
                from app.scrapers.core.netguard import normalize_url

                current = normalize_url(current, location)
                # SSRF re-validation on EVERY hop (brief §41)
                try:
                    current = validate_url(current, self.url_policy)
                except Exception as exc:
                    raise ScraperBlockedTargetError(str(exc)) from exc
                continue

            try:
                declared = int(resp.headers.get("content-length") or 0)
                if declared > self.policy.max_response_bytes:
                    await resp.aclose()
                    raise ScraperNetworkError(
                        f"Response too large ({declared} bytes) — refusing to download"
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes(CHUNK):
                    size += len(chunk)
                    if size > self.policy.max_response_bytes:
                        await resp.aclose()
                        raise ScraperNetworkError(
                            f"Response exceeded {self.policy.max_response_bytes} bytes mid-stream"
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
                self.bytes_received += size
                return Response(resp.status_code, dict(resp.headers), content, str(resp.url))
            finally:
                await resp.aclose()
        raise ScraperNetworkError(f"Too many redirects: {url}")

    # ----------------------------------------------------------- convenience
    async def get_json(self, url: str, **kwargs):
        resp = await self.get(url, **kwargs)
        if resp.status_code >= 400:
            raise ScraperNetworkError(f"HTTP {resp.status_code} for {url}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ScraperNetworkError(f"Invalid JSON from {url}") from exc

    async def get_html(self, url: str, **kwargs) -> Response:
        resp = await self.get(url, **kwargs)
        if resp.status_code >= 400:
            raise ScraperNetworkError(f"HTTP {resp.status_code} for {url}")
        ctype = resp.headers.get("content-type", "")
        if ctype and not any(t in ctype for t in ("text/html", "application/xhtml", "text/plain", "application/xml")):
            raise ScraperNetworkError(f"Unsupported content-type {ctype!r} for {url}")
        return resp
