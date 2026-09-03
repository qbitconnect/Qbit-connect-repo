"""Email normalization service (Phase 7 §16).

    trim whitespace
    lowercase the DOMAIN (never the local part — local-part semantics are
        case-sensitive in the RFC and some providers honor that)
    validate format; reject obviously malformed values
    NEVER perform unsafe transformations that could alter a valid address

Result: (ok, email_normalized | None, reason). The normalized value is what
suppression/deduplication keys use; the original string is preserved for
display where provided by the operator (§16: store email + email_normalized).
"""

from __future__ import annotations

import re

#: pragmatic RFC-5322-ish validation — strict enough to reject malformed
#: input, loose enough for real-world addresses (quoted local parts are rare
#: and rejected on purpose; they are a spam-crafting vector in bulk mail)
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)

OK = "OK"
INVALID = "INVALID"
EMPTY = "EMPTY"


def normalize_email(address: str | None) -> tuple[bool, str | None, str | None]:
    """(ok, normalized, reason) — pure function, no I/O, never raises."""
    if address is None:
        return False, None, EMPTY
    value = str(address).strip()
    if not value:
        return False, None, EMPTY
    # internal whitespace can never be part of a deliverable address here
    if re.search(r"\s", value):
        return False, None, INVALID
    if value.count("@") != 1:
        return False, None, INVALID
    local, _, domain = value.partition("@")
    if not local or not domain:
        return False, None, INVALID
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False, None, INVALID
    if not _LOCAL_RE.match(local):
        return False, None, INVALID
    domain = domain.lower().rstrip(".")
    if len(domain) > 253 or not _DOMAIN_RE.match(domain):
        return False, None, INVALID
    if len(local) > 64:
        return False, None, INVALID
    return True, f"{local}@{domain}", OK


def is_email(address: str | None) -> bool:
    ok, _n, _r = normalize_email(address)
    return ok


class EmailNormalizationService:
    """Service wrapper for the pure functions (used by eligibility + import)."""

    @staticmethod
    def normalize(address: str | None) -> tuple[bool, str | None, str | None]:
        return normalize_email(address)

    @staticmethod
    def validate(address: str | None) -> str | None:
        """Return a normalized address or raise-free None + reason via tuple."""
        ok, normalized, reason = normalize_email(address)
        return normalized if ok else None

    @staticmethod
    def reason(address: str | None) -> str | None:
        ok, _n, reason = normalize_email(address)
        return None if ok else (reason or INVALID)
