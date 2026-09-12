"""QBIT CONNECT — Validation & Quality Engine (Brief §15, §16).

Validates individual extracted records: phone format, email syntax, URL structure,
business identity existence, and assigns quality confidence scores.
"""

from __future__ import annotations

import enum
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse


class RecordQuality(str, enum.Enum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    UNVERIFIED = "UNVERIFIED"


class ConfidenceScore(str, enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNVERIFIED = "UNVERIFIED"


@dataclass
class ValidationResult:
    is_valid: bool
    quality: RecordQuality
    confidence: ConfidenceScore
    has_valid_phone: bool
    has_valid_email: bool
    has_valid_url: bool
    normalized_phone: str | None = None
    normalized_email: str | None = None
    canonical_url: str | None = None
    errors: list[str] = field(default_factory=list)
    fields_present: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["quality"] = self.quality.value
        data["confidence"] = self.confidence.value
        return data


_EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
)

_INVALID_EMAIL_PATTERNS = (
    "example.com", "domain.com", "test.com", "sentry.io", "wixpress.com",
    ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", "noreply",
)

_PHONE_CLEAN_RE = re.compile(r"[^\d+]")


class RecordValidator:
    """Validates raw extracted item against data quality rules."""

    @classmethod
    def validate_record(cls, record: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        present = [k for k, v in record.items() if v not in (None, "", [], {})]

        # 1. Business identity
        name = record.get("name") or record.get("business_name") or record.get("title") or record.get("company")
        if not name or len(str(name).strip()) < 2:
            errors.append("Missing business identity / name")

        # 2. Phone validation
        raw_phone = record.get("phone") or record.get("contact_phone") or record.get("mobile")
        norm_phone = None
        has_valid_phone = False
        if raw_phone:
            cleaned = _PHONE_CLEAN_RE.sub("", str(raw_phone)).strip()
            digits = re.sub(r"\D", "", cleaned)
            # Indian numbers or standard international (7 to 15 digits)
            if 7 <= len(digits) <= 15:
                norm_phone = cleaned
                has_valid_phone = True
            else:
                errors.append(f"Invalid phone digit count ({len(digits)})")

        # 3. Email validation
        raw_email = record.get("email") or record.get("contact_email")
        norm_email = None
        has_valid_email = False
        if raw_email:
            email_cand = str(raw_email).strip().lower()
            if _EMAIL_REGEX.match(email_cand) and not any(p in email_cand for p in _INVALID_EMAIL_PATTERNS):
                norm_email = email_cand
                has_valid_email = True
            else:
                errors.append(f"Invalid email format: {raw_email[:30]}")

        # 4. URL validation
        raw_url = record.get("website") or record.get("url") or record.get("source_url")
        canon_url = None
        has_valid_url = False
        if raw_url:
            u_cand = str(raw_url).strip()
            if not u_cand.startswith(("http://", "https://")):
                u_cand = "https://" + u_cand
            try:
                parsed = urlparse(u_cand)
                if parsed.netloc and "." in parsed.netloc:
                    canon_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
                    has_valid_url = True
                else:
                    errors.append(f"Invalid website host: {raw_url[:30]}")
            except Exception:
                errors.append(f"Malformed URL: {raw_url[:30]}")

        # Quality scoring
        is_valid = bool(name and (has_valid_phone or has_valid_email or has_valid_url or record.get("address")))
        quality: RecordQuality
        confidence: ConfidenceScore

        if is_valid:
            if has_valid_phone and (has_valid_email or has_valid_url):
                quality = RecordQuality.VALID
                confidence = ConfidenceScore.HIGH
            elif has_valid_phone or has_valid_email:
                quality = RecordQuality.VALID
                confidence = ConfidenceScore.MEDIUM
            else:
                quality = RecordQuality.PARTIAL
                confidence = ConfidenceScore.LOW
        else:
            quality = RecordQuality.INVALID
            confidence = ConfidenceScore.LOW

        return ValidationResult(
            is_valid=is_valid,
            quality=quality,
            confidence=confidence,
            has_valid_phone=has_valid_phone,
            has_valid_email=has_valid_email,
            has_valid_url=has_valid_url,
            normalized_phone=norm_phone,
            normalized_email=norm_email,
            canonical_url=canon_url,
            errors=errors,
            fields_present=present,
        )
