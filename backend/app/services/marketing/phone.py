"""Phone normalization for WhatsApp recipients (Phase 6 §11).

Rules:
- output is E.164-ish: optional leading '+', digits only, 8-15 digits total
- strips spaces, hyphens, brackets, dots, slashes and unicode digits
- NEVER guesses country codes blindly: without an explicit '+' (or a caller-
  supplied default country prefix) the number is marked invalid instead of
  being padded with a guessed prefix — a wrong guess means a message to the
  wrong person, which is worse than an honest failure
"""

from __future__ import annotations

import re

_PUNCT = re.compile(r"[\s()\-\u2013\u2014./\\,]")
_NON_DIGIT = re.compile(r"[^0-9]")
# Arabic-Indic (٠-٩ / ۰-۹) and Devanagari (०-९) digit variants → ASCII
_UNICODE_DIGIT_MAP: dict[int, str] = {ord(c): str(i) for i, c in enumerate("0123456789")}
for _seq in ("٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹", "०१२३४५६७८९"):
    for _i, _ch in enumerate(_seq):
        _UNICODE_DIGIT_MAP[ord(_ch)] = str(_i)
UNICODE_DIGITS = _UNICODE_DIGIT_MAP

MIN_DIGITS = 8
MAX_DIGITS = 15


def normalize_recipient_phone(
    value: str | None, *, default_country_prefix: str | None = None
) -> tuple[bool, str | None, str | None]:
    """Normalize a raw phone string for WhatsApp delivery.

    Returns (ok, e164_or_None, reason). `ok=False` always carries a reason;
    the function never raises for user input.
    """
    raw = str(value or "").strip()
    if not raw:
        return False, None, "MISSING_PHONE"

    # explicit + marks an international number (the only trusted form)
    explicit_international = raw.startswith("+") or raw.startswith("00")
    cleaned = raw.translate(UNICODE_DIGITS)
    cleaned = _PUNCT.sub("", cleaned)
    had_plus = cleaned.startswith("+")
    cleaned = cleaned.lstrip("+")
    # '00' international prefix → treat as explicit
    if cleaned.startswith("00"):
        cleaned = cleaned[2:]
        explicit_international = True
    if had_plus:
        explicit_international = True

    digits = _NON_DIGIT.sub("", cleaned)
    if not digits:
        return False, None, "INVALID_PHONE"
    if len(digits) < MIN_DIGITS:
        return False, None, "INVALID_PHONE_TOO_SHORT"
    if len(digits) > MAX_DIGITS:
        return False, None, "INVALID_PHONE_TOO_LONG"

    if not explicit_international:
        # no '+', no default country context → refuse to guess (§11)
        prefix = _clean_default_prefix(default_country_prefix)
        if prefix is None:
            return False, None, "COUNTRY_CODE_REQUIRED"
        digits = prefix + digits
        if len(digits) > MAX_DIGITS:
            return False, None, "INVALID_PHONE_TOO_LONG"

    return True, f"+{digits}", None


def _clean_default_prefix(prefix: str | None) -> str | None:
    value = _NON_DIGIT.sub("", str(prefix or ""))
    if not value:
        return None
    return value


def is_valid_e164(value: str | None) -> bool:
    ok, _, _ = normalize_recipient_phone(value)
    return ok


def mask_number(e164: str | None) -> str:
    """Display mask: +49•••••••678 (country code + last 3 digits)."""
    raw = "".join(ch for ch in str(e164 or "") if ch.isdigit())
    if len(raw) < 6:
        return "\u2022\u2022\u2022"
    return f"+{raw[:2]}\u2022\u2022\u2022\u2022\u2022{raw[-3:]}"


class PhoneNormalizationService:
    """Service wrapper so callers depend on the abstraction, not the function."""

    def normalize(
        self, value: str | None, *, default_country_prefix: str | None = None
    ) -> tuple[bool, str | None, str | None]:
        return normalize_recipient_phone(value, default_country_prefix=default_country_prefix)

    def is_valid(self, value: str | None) -> bool:
        return is_valid_e164(value)

    def mask(self, value: str | None) -> str:
        return mask_number(value)
