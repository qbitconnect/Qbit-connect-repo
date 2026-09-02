"""Outbound target safety — SSRF / private-network protection (brief §41).

Every URL an actor fetches passes `validate_url()`:

- scheme must be http/https (file://, ftp://, gopher:// ... are refused)
- userinfo (user:pass@host) is refused
- port must be in the configured allowlist (default 80/443)
- the host is DNS-resolved and EVERY returned address is checked against
  loopback / RFC1918 / link-local / CGNAT / reserved / multicast / 0.0.0.0 /
  cloud-metadata ranges (169.254.169.254 is link-local and therefore covered)
- IPv6 loopback/ULA/link-local are covered as well

`QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS=true` relaxes resolution checks for
isolated test environments ONLY — production default is deny. The check is
repeated for every redirect hop by the policy HTTP client.

Known limitation (documented in the actor README): the resolved address is
validated at request time; a hostile authoritative DNS server could still
return different addresses per lookup (DNS rebinding). Self-hosted operators
who need hard guarantees should run workers without internal network access.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.core.logging import get_logger
from app.scrapers.core.exceptions import ScraperValidationError

logger = get_logger("qbit.scrapers.netguard")

ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_ALLOWED_PORTS = {80, 443}

_METADATA_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}


class UrlPolicy:
    """Configurable URL policy (ports, private-target allowance)."""

    def __init__(
        self,
        *,
        allowed_ports: set[int] | None = None,
        allow_private_targets: bool = False,
        allowed_hosts: set[str] | None = None,
    ) -> None:
        self.allowed_ports = allowed_ports or set(DEFAULT_ALLOWED_PORTS)
        self.allow_private_targets = allow_private_targets
        # Explicit operator allowlist that bypasses the private-target block
        # (used by tests against a local fixture server; never set in production).
        self.allowed_hosts = {h.lower() for h in (allowed_hosts or set())}


def _ip_is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_global", True) is False
    )


def is_private_ip(ip: str) -> bool:
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return _ip_is_forbidden(parsed)


def normalize_url(base: str | None, url: str) -> str:
    """Resolve relative → absolute and drop fragments."""
    if base:
        return urljoin(base, url)
    return url


def canonical_url(url: str) -> str:
    """Normalized form for dedup: lowercase scheme/host, sorted query,
    default ports stripped, trailing slash kept, fragments dropped,
    common tracking parameters removed."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "http"
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    query_pairs = [p for p in sorted(filter(None, parts.query.split("&"))) if not p.lower().startswith(("utm_", "fbclid=", "gclid="))]
    return urlunsplit((scheme, host, path, "&".join(query_pairs), ""))


def _registrable_domain(hostname: str) -> str:
    """Best-effort eTLD+1 without the `tldextract` dependency.

    Handles common two-part public suffixes (co.uk, com.au, ...); good enough
    to keep a crawler inside the target site's domain (brief §42).
    """
    labels = hostname.lower().rstrip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    two_part_tld = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne"}
    if labels[-2] in two_part_tld and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def in_same_domain(url_a: str, url_b: str) -> bool:
    return _registrable_domain(urlsplit(url_a).hostname or "") == _registrable_domain(
        urlsplit(url_b).hostname or ""
    )


def validate_url(
    url: str,
    policy: UrlPolicy,
    *,
    resolve: bool = True,
) -> str:
    """Validate one outbound URL; returns the URL or raises ScraperValidationError."""
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise ScraperValidationError(f"Malformed URL: {url[:200]!r}") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ScraperValidationError(
            f"Blocked URL scheme {parts.scheme!r}: only http/https are allowed"
        )
    if not parts.hostname:
        raise ScraperValidationError("URL has no hostname")
    if parts.username or parts.password:
        raise ScraperValidationError("URLs with embedded credentials are not allowed")

    host = parts.hostname.lower().rstrip(".")
    if host in _METADATA_HOSTNAMES or host == "localhost" or host.endswith(".localhost"):
        raise ScraperValidationError("Localhost and cloud metadata endpoints are blocked")

    if host in policy.allowed_hosts:
        # Test fixture escape hatch — private targets explicitly allowed.
        return url.strip()

    if policy.allow_private_targets:
        return url.strip()

    # IP-literal hosts are checked WITHOUT DNS (defense in depth: the policy
    # must hold even when callers skip resolution, e.g. validate-only paths).
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and _ip_is_forbidden(literal_ip):
        raise ScraperValidationError(
            "Blocked: target is a private, loopback, link-local or reserved address"
        )

    port = parts.port
    if port is not None and port not in policy.allowed_ports:
        raise ScraperValidationError(f"Port {port} is not in the allowed port list")

    if resolve:
        for ip in resolve_host(host):
            if _ip_is_forbidden(ipaddress.ip_address(ip)):
                logger.warning(
                    "Blocked private-network target",
                    extra={"extra_fields": {"host": host, "ip_class": str(ip)}},
                )
                raise ScraperValidationError(
                    "Blocked: target resolves to a private, loopback, link-local or reserved address"
                )
    return url.strip()


def resolve_host(host: str) -> list[str]:
    """Resolve a hostname to all of its addresses (sync — small TTL cost)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ScraperValidationError(f"DNS resolution failed for {host!r}") from exc
    return [info[4][0] for info in infos]


async def validate_url_async(url: str, policy: UrlPolicy) -> str:
    # getaddrinfo in a thread so the event loop is never blocked by slow DNS.
    return await asyncio.to_thread(validate_url, url, policy, resolve=True)
