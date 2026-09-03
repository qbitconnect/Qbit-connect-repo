"""Email error normalization (Phase 7 §22, §23).

    EmailErrorNormalizer
        SMTP / Email-API failure → canonical code + retry class

Canonical codes (§23):
    INVALID_RECIPIENT, AUTHENTICATION_ERROR, CONNECTION_ERROR, TLS_ERROR,
    RATE_LIMITED, MAILBOX_UNAVAILABLE, MESSAGE_REJECTED, PROVIDER_UNAVAILABLE,
    CONFIGURATION_ERROR, UNKNOWN

Retry classes (§22):
    TRANSIENT  — temporary network failure, provider unavailable, temporary
                 SMTP (4xx) failure → the queue may retry with backoff
    PERMANENT  — invalid recipient, invalid sender, authentication
                 configuration error, rejected address → NEVER retried
    CONFIGURATION — account/provider misconfiguration → never retried, the
                 operator must fix the account first

Uncertain-delivery rule (§20): a timeout after message data was handed to the
provider is classified DELIVERY_STATE_UNKNOWN and NOT retried automatically —
the platform never risks a duplicate email when provider acceptance is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.marketing.providers.base import ErrorClass

#: canonical error codes emitted by the email stack
INVALID_RECIPIENT = "INVALID_RECIPIENT"
AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
CONNECTION_ERROR = "CONNECTION_ERROR"
TLS_ERROR = "TLS_ERROR"
RATE_LIMITED = "RATE_LIMITED"
MAILBOX_UNAVAILABLE = "MAILBOX_UNAVAILABLE"
MESSAGE_REJECTED = "MESSAGE_REJECTED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
UNKNOWN_STATE = "DELIVERY_STATE_UNKNOWN"   # §20 — never auto-retried
UNKNOWN = "UNKNOWN_PROVIDER_ERROR"


@dataclass(frozen=True)
class NormalizedEmailError:
    code: str
    error_class: ErrorClass
    message: str
    #: provider hint, honored by the queue backoff (never used to evade limits)
    retry_after_seconds: float | None = None


def _err(code: str, error_class: ErrorClass, message: str,
         retry_after: float | None = None) -> NormalizedEmailError:
    return NormalizedEmailError(
        code=code, error_class=error_class,
        message=(message or code)[:500], retry_after_seconds=retry_after,
    )


class EmailErrorNormalizer:
    """Map raw SMTP replies / API failures onto canonical, sanitized errors.

    Raw provider text is truncated and never includes credentials; the
    normalizer's output is what reaches operators and the UI (§23: "Do not
    expose raw technical secrets to end users").
    """

    #: SMTP permanent-failure substrings (5xx replies and hard mailbox errors)
    _HARD_MARKERS = (
        "user unknown", "no such user", "recipient address rejected",
        "unknown user", "invalid recipient", "no such mailbox",
        "address rejected", "not our customer", "mailbox unavailable",
        "relay access denied",
    )
    _AUTH_MARKERS = (
        "authentication", "auth failed", "535", "534", "530", "538",
        "invalid credentials", "username and password not accepted",
    )
    _TLS_MARKERS = ("tls", "ssl", "certificate", "starttls")
    _RATE_MARKERS = ("rate limit", "too many", "throttl", "421", "450")

    def normalize_smtp(self, *, exception: Exception | None = None,
                       smtp_code: int | None = None,
                       message: str | None = None) -> NormalizedEmailError:
        """Normalize an SMTP-layer failure."""
        raw = " ".join(str(message or (exception if exception is not None else "") or "").split())
        low = raw.lower()

        if smtp_code is not None:
            if 500 <= smtp_code <= 599:
                if any(m in low for m in self._AUTH_MARKERS):
                    # canned message — server text can echo submitted credentials
                    return _err(AUTHENTICATION_ERROR, ErrorClass.PERMANENT,
                                "SMTP authentication failed — check username/password")
                if any(m in low for m in self._HARD_MARKERS):
                    return _err(INVALID_RECIPIENT if ("recipient" in low or "user" in low)
                                else MAILBOX_UNAVAILABLE, ErrorClass.PERMANENT,
                                raw or "SMTP permanent failure")
                if any(m in low for m in self._TLS_MARKERS):
                    return _err(TLS_ERROR, ErrorClass.PERMANENT, raw or "SMTP TLS failure")
                return _err(MESSAGE_REJECTED, ErrorClass.PERMANENT, raw or "SMTP permanent failure")
            if 400 <= smtp_code <= 499:
                if any(m in low for m in self._RATE_MARKERS):
                    return _err(RATE_LIMITED, ErrorClass.TRANSIENT, raw or "SMTP rate limited")
                return _err(MAILBOX_UNAVAILABLE, ErrorClass.TRANSIENT, raw or "SMTP temporary failure")

        if exception is not None:
            name = type(exception).__name__.lower()
            if any(m in low for m in self._AUTH_MARKERS):
                return _err(AUTHENTICATION_ERROR, ErrorClass.PERMANENT,
                            "SMTP authentication failed — check username/password")
            if any(m in low for m in self._TLS_MARKERS):
                return _err(TLS_ERROR, ErrorClass.PERMANENT, raw or "SMTP TLS failure")
            if "timeout" in name or "timed out" in low:
                # §20: acceptance unknown → never auto-retry (duplicate risk)
                return _err(UNKNOWN_STATE, ErrorClass.PERMANENT,
                            "Delivery state unknown (timeout) — not retried to prevent duplicates")
            if ("refused" in low or "connect" in name or "connection" in low
                    or "network" in low or "disconnected" in low):
                return _err(CONNECTION_ERROR, ErrorClass.TRANSIENT, raw or "SMTP connection failed")
            if "dns" in low or "getaddrinfo" in low or "resolve" in low:
                return _err(PROVIDER_UNAVAILABLE, ErrorClass.TRANSIENT, raw or "SMTP host unreachable")
        return _err(UNKNOWN, ErrorClass.PERMANENT, raw or "Unknown SMTP error")

    def normalize_api(self, *, status_code: int | None = None,
                      payload: dict | None = None,
                      exception: Exception | None = None) -> NormalizedEmailError:
        """Normalize a generic Email-API failure (transport or HTTP layer)."""
        payload = payload if isinstance(payload, dict) else {}
        raw = " ".join(str(payload.get("error") or payload.get("message") or
                           (exception if exception is not None else "") or "").split())
        low = raw.lower()

        if exception is not None:
            name = type(exception).__name__.lower()
            if "timeout" in name or "timed out" in low:
                return _err(UNKNOWN_STATE, ErrorClass.PERMANENT,
                            "Delivery state unknown (timeout) — not retried to prevent duplicates")
            if ("connect" in name or "connection" in low or "refused" in low
                    or "network" in low):
                return _err(PROVIDER_UNAVAILABLE, ErrorClass.TRANSIENT, raw or "Email API unreachable")
            return _err(PROVIDER_UNAVAILABLE, ErrorClass.TRANSIENT, raw or "Email API request failed")

        if status_code is None:
            return _err(UNKNOWN, ErrorClass.PERMANENT, raw or "Unknown email API error")
        if status_code in (401, 403):
            return _err(AUTHENTICATION_ERROR, ErrorClass.PERMANENT,
                        raw or "Email API rejected the credentials")
        if status_code == 404:
            return _err(CONFIGURATION_ERROR, ErrorClass.CONFIGURATION,
                        raw or "Email API endpoint not found — check API_BASE_URL")
        if status_code in (400, 422):
            if "recipient" in low or "address" in low:
                return _err(INVALID_RECIPIENT, ErrorClass.PERMANENT,
                            raw or "Recipient rejected by provider")
            return _err(MESSAGE_REJECTED, ErrorClass.PERMANENT, raw or "Message rejected by provider")
        if status_code == 429:
            retry_after = payload.get("retry_after") or payload.get("retry_after_seconds")
            try:
                hint = float(retry_after) if retry_after is not None else None
            except (TypeError, ValueError):
                hint = None
            return _err(RATE_LIMITED, ErrorClass.TRANSIENT, raw or "Email API rate limited",
                        retry_after=hint)
        if 500 <= status_code <= 599:
            return _err(PROVIDER_UNAVAILABLE, ErrorClass.TRANSIENT,
                        raw or "Email API unavailable")
        if 200 <= status_code < 300:
            return _err(UNKNOWN, ErrorClass.PERMANENT, raw or "Unexpected email API response")
        return _err(UNKNOWN, ErrorClass.PERMANENT, raw or "Unknown email API error")
