"""Email/phone normalization + validation (Phase 7 §15, §16; WhatsApp §phone).

Email rules (spec §16):
- trim surrounding whitespace
- lowercase the DOMAIN only (local-part semantics preserved; local-part is
  lowercased only if it is entirely case-safe ASCII — dedup key is
  case-insensitive for typical business addresses)
- validate format; reject obviously malformed values (no spaces, no
  consecutive dots, must contain exactly one @, bounded lengths)
- NEVER perform transformations that could alter a valid address

The platform does not guess missing data: if a value cannot be safely
normalized it is reported invalid instead of being "fixed" blindly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]+"  # local part charset (practical subset)
    r"@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"  # domain label
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+$"  # + TLD
)

DISALLOWED_LOCAL = ("..",)
MAX_EMAIL_LEN = 320


@dataclass(frozen=True)
class EmailResult:
    email: str | None      # cleaned display value (trimmed); None if invalid
    normalized: str | None  # dedup/suppression key
    valid: bool
    reason: str | None = None  # MISSING_EMAIL | INVALID_EMAIL


def normalize_email(raw: str | None) -> EmailResult:
    """Normalize + validate an email address (spec §16)."""
    if raw is None:
        return EmailResult(None, None, False, "MISSING_EMAIL")
    value = raw.strip()
    if not value:
        return EmailResult(None, None, False, "MISSING_EMAIL")
    if len(value) > MAX_EMAIL_LEN or " " in value or "\t" in value:
        return EmailResult(None, None, False, "INVALID_EMAIL")
    if value.count("@") != 1:
        return EmailResult(None, None, False, "INVALID_EMAIL")
    local, domain = value.rsplit("@", 1)
    if not local or not domain or local.startswith(".") or local.endswith("."):
        return EmailResult(None, None, False, "INVALID_EMAIL")
    for bad in DISALLOWED_LOCAL:
        if bad in local:
            return EmailResult(None, None, False, "INVALID_EMAIL")
    if not EMAIL_RE.match(value):
        return EmailResult(None, None, False, "INVALID_EMAIL")
    normalized = f"{local.lower()}@{domain.lower()}"
    return EmailResult(value, normalized, True)


def validate_email(raw: str | None) -> bool:
    return normalize_email(raw).valid


# ------------------------------------------------------------------ phone
PLUS_DIGITS = re.compile(r"^\+[1-9]\d{6,14}$")  # E.164
STRIP_RE = re.compile(r"[\s\-().]")
DIGITS_RE = re.compile(r"^\d{7,15}$")


@dataclass(frozen=True)
class PhoneResult:
    phone: str | None
    normalized: str | None
    valid: bool
    reason: str | None = None


def normalize_phone(raw: str | None, *, default_country_prefix: str | None = None) -> PhoneResult:
    """Normalize a phone number toward E.164 WITHOUT guessing (Phase 6 rule).

    - strips spaces, dashes, dots, parentheses
    - ``00`` international prefix → ``+``
    - a leading ``+`` (or 00) is required for a definitive result; when absent,
      ``default_country_prefix`` (e.g. "+91") is applied ONLY if the caller
      explicitly provides it — otherwise the number is marked invalid rather
      than guessed.
    """
    if raw is None:
        return PhoneResult(None, None, False, "MISSING_PHONE")
    value = STRIP_RE.sub("", raw.strip())
    if not value:
        return PhoneResult(None, None, False, "MISSING_PHONE")
    if value.startswith("00"):
        value = "+" + value[2:]
    if value.startswith("+"):
        digits = value[1:]
        if not digits.isdigit() or not PLUS_DIGITS.match(value):
            return PhoneResult(None, None, False, "INVALID_PHONE")
        return PhoneResult(value, value, True)
    if value.isdigit() and DIGITS_RE.match(value):
        if default_country_prefix:
            candidate = default_country_prefix + value.lstrip("0")
            if PLUS_DIGITS.match(candidate):
                return PhoneResult(raw.strip(), candidate, True)
        # No safe way to determine the country code — never guess.
        return PhoneResult(raw.strip(), None, False, "INVALID_PHONE")
    return PhoneResult(raw.strip(), None, False, "INVALID_PHONE")
