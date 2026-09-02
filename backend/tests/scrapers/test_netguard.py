"""SSRF / private-network / URL-policy tests (brief §41, §42, §43)."""

from __future__ import annotations

import pytest

from app.scrapers.core.exceptions import ScraperValidationError
from app.scrapers.core.netguard import (
    UrlPolicy,
    canonical_url,
    in_same_domain,
    is_private_ip,
    validate_url,
)

POLICY = UrlPolicy()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://example.com",
        "http://user:pass@example.com/",
        "http://example.com:8080/",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.10/",
        "http://172.16.0.1/",
        "http://[::1]/",
        "http://metadata.google.internal/",
    ],
)
def test_blocked_urls(url: str):
    with pytest.raises(ScraperValidationError):
        validate_url(url, POLICY, resolve=False)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.0.9", "169.254.1.1", "::1", "0.0.0.0"])
def test_is_private_ip_true(ip: str):
    assert is_private_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_is_private_ip_false(ip: str):
    assert is_private_ip(ip) is False


def test_valid_public_url_passes():
    assert validate_url("https://example.com/page", POLICY, resolve=False) == "https://example.com/page"


def test_dns_failure_is_blocked():
    with pytest.raises(ScraperValidationError, match="DNS"):
        validate_url("http://this-domain-definitely-does-not-exist-qbit.invalid/", POLICY, resolve=True)


def test_allowed_hosts_escape_hatch_for_tests():
    policy = UrlPolicy(allowed_hosts={"fixture.internal"})
    assert validate_url("http://fixture.internal:80/x", policy, resolve=False)


def test_canonical_url_dedup_form():
    assert (
        canonical_url("https://WWW.Example.com/a/?utm_source=x&b=2&a=1")
        == "https://www.example.com/a/?a=1&b=2"  # host lowercased, tracking dropped
    )
    assert canonical_url("https://example.com:443/x") == "https://example.com/x"
    assert canonical_url("http://example.com#frag") == "http://example.com/"


def test_same_domain_registrable():
    assert in_same_domain("https://www.example.com/a", "https://shop.example.com/b")
    assert in_same_domain("https://example.co.uk/x", "https://news.example.co.uk/y")
    assert not in_same_domain("https://example.com", "https://notexample.com")
    assert not in_same_domain("https://example.com", "https://google.com")


def test_private_targets_require_expensive_opt_in():
    strict = UrlPolicy()
    relaxed = UrlPolicy(allow_private_targets=True)
    with pytest.raises(ScraperValidationError):
        validate_url("http://192.168.1.1/", strict, resolve=False)
    # allowed only when the isolated-test flag is on (never in production —
    # config.validate_runtime refuses it)
    assert validate_url("http://192.168.1.1/", relaxed, resolve=False)
