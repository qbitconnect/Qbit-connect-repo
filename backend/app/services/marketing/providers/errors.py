"""Provider error normalization (Phase 7 §22, §23; WhatsApp §errors).

Every provider failure is mapped to a NORMALIZED CATEGORY plus a RETRY CLASS:

    TRANSIENT  → exponential backoff retry while attempts remain
    PERMANENT  → no retry (never hammer a rejecting provider)
    UNKNOWN    → treated as TRANSIENT for a small number of attempts

Categories (email, spec §23): INVALID_RECIPIENT, AUTHENTICATION_ERROR,
CONNECTION_ERROR, TLS_ERROR, RATE_LIMITED, MAILBOX_UNAVAILABLE,
MESSAGE_REJECTED, PROVIDER_UNAVAILABLE, CONFIGURATION_ERROR, UNKNOWN.

Categories (WhatsApp): INVALID_RECIPIENT, INVALID_TEMPLATE,
TEMPLATE_NOT_APPROVED, AUTHENTICATION_ERROR, PERMISSION_ERROR, RATE_LIMITED,
PROVIDER_UNAVAILABLE, ACCOUNT_ERROR, MESSAGE_REJECTED, UNKNOWN_PROVIDER_ERROR.

RATE_LIMITED backoff is *compliance* with provider guidance — the opposite of
evasion (spec §42, §64).
"""

from __future__ import annotations

# --- normalized error codes -------------------------------------------------
INVALID_RECIPIENT = "INVALID_RECIPIENT"
INVALID_TEMPLATE = "INVALID_TEMPLATE"
TEMPLATE_NOT_APPROVED = "TEMPLATE_NOT_APPROVED"
AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
PERMISSION_ERROR = "PERMISSION_ERROR"
CONNECTION_ERROR = "CONNECTION_ERROR"
TLS_ERROR = "TLS_ERROR"
RATE_LIMITED = "RATE_LIMITED"
MAILBOX_UNAVAILABLE = "MAILBOX_UNAVAILABLE"
MESSAGE_REJECTED = "MESSAGE_REJECTED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
ACCOUNT_ERROR = "ACCOUNT_ERROR"
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
UNKNOWN = "UNKNOWN"

TRANSIENT = "TRANSIENT"
PERMANENT = "PERMANENT"
UNKNOWN_CLASS = "UNKNOWN"

#: category → retry class (spec §22)
RETRY_CLASSIFICATION: dict[str, str] = {
    INVALID_RECIPIENT: PERMANENT,
    INVALID_TEMPLATE: PERMANENT,
    TEMPLATE_NOT_APPROVED: PERMANENT,
    AUTHENTICATION_ERROR: PERMANENT,
    PERMISSION_ERROR: PERMANENT,
    CONNECTION_ERROR: TRANSIENT,
    TLS_ERROR: PERMANENT,
    RATE_LIMITED: TRANSIENT,
    MAILBOX_UNAVAILABLE: TRANSIENT,   # greylisting/soft availability
    MESSAGE_REJECTED: PERMANENT,
    PROVIDER_UNAVAILABLE: TRANSIENT,
    ACCOUNT_ERROR: PERMANENT,
    CONFIGURATION_ERROR: PERMANENT,
    UNKNOWN: UNKNOWN_CLASS,
}


def retry_class(error_code: str | None) -> str:
    if not error_code:
        return UNKNOWN_CLASS
    return RETRY_CLASSIFICATION.get(error_code, UNKNOWN_CLASS)


def backoff_seconds(
    attempt: int, *, base_seconds: float = 30.0, max_seconds: float = 900.0
) -> float:
    """Exponential backoff with cap: base * 2^(attempt-1), max ``max_seconds``."""
    if attempt < 1:
        attempt = 1
    value = base_seconds * (2 ** (attempt - 1))
    return min(value, max_seconds)


def classify_smtp_failure(exc: BaseException) -> tuple[str, str]:
    """Map an SMTP layer exception to (category, sanitized_message).

    Recognizes smtplib-style status codes (4xx transient / 5xx permanent) and
    aiosmtplib errors. Raw credentials never appear in the message.
    """
    name = type(exc).__name__
    text = str(exc) or name
    # Never leak anything that looks like AUTH material in the message.
    sanitized = text.replace("\n", " ").replace("\r", " ")[:300]

    code = getattr(exc, "smtp_code", None)
    smtp_error = getattr(exc, "smtp_error", b"") or b""
    try:
        err_text = smtp_error.decode("utf-8", "ignore") if isinstance(smtp_error, bytes) else str(smtp_error)
    except Exception:  # pragma: no cover
        err_text = ""

    if "Authentication" in name or "auth" in text.lower() and code == 535:
        return AUTHENTICATION_ERROR, "SMTP authentication failed"
    if code is not None:
        if code == 421:
            return PROVIDER_UNAVAILABLE, sanitized
        if 400 <= code < 500:
            return CONNECTION_ERROR, sanitized
        if 500 <= code < 600:
            if code == 535:
                return AUTHENTICATION_ERROR, "SMTP authentication failed"
            if code in (550, 551, 553):
                return INVALID_RECIPIENT, sanitized
            if code == 552:
                return MAILBOX_UNAVAILABLE, sanitized
            if code in (554,):
                return MESSAGE_REJECTED, sanitized
            return MESSAGE_REJECTED, sanitized
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return CONNECTION_ERROR, "SMTP connection timed out"
    if "connection" in lowered and ("refused" in lowered or "reset" in lowered):
        return CONNECTION_ERROR, sanitized
    if "ssl" in lowered or "tls" in lowered or "certificate" in lowered:
        return TLS_ERROR, sanitized
    if "authentication" in lowered or "badcredentials" in lowered or "535" in lowered:
        return AUTHENTICATION_ERROR, "SMTP authentication failed"
    if "rate" in lowered and "limit" in lowered:
        return RATE_LIMITED, sanitized
    return UNKNOWN, sanitized


def classify_http_failure(status: int | None, body: str) -> tuple[str, str]:
    """Map an HTTP API failure to (category, sanitized_message)."""
    snippet = (body or "").replace("\n", " ").replace("\r", " ")[:300]
    lowered = snippet.lower()
    if status == 401 or status == 403:
        return (AUTHENTICATION_ERROR if status == 401 else PERMISSION_ERROR), snippet
    if status == 429:
        return RATE_LIMITED, snippet
    if status == 400:
        if "recipient" in lowered or "phone number" in lowered or "email" in lowered:
            return INVALID_RECIPIENT, snippet
        if "template" in lowered:
            return INVALID_TEMPLATE, snippet
        return MESSAGE_REJECTED, snippet
    if status == 404:
        return INVALID_RECIPIENT, snippet
    if status is not None and 500 <= status < 600:
        return PROVIDER_UNAVAILABLE, snippet
    return UNKNOWN, snippet


# --- WhatsApp Cloud API error mapping ---------------------------------------
_WA_CODE_MAP: dict[int, str] = {
    130429: RATE_LIMITED,          # rate limit hit
    131048: RATE_LIMITED,          # spam rate limit (must honor, not evade)
    131047: RATE_LIMITED,          # re-engagement message
    131026: MESSAGE_REJECTED,      # undeliverable
    131049: MESSAGE_REJECTED,      # marketing message outside window
    131051: INVALID_RECIPIENT,     # unsupported recipient
    470: INVALID_RECIPIENT,        # invalid recipient
    100: INVALID_TEMPLATE,         # parameter / template not found
    132000: INVALID_TEMPLATE,      # param count mismatch
    132001: INVALID_TEMPLATE,      # template does not exist
    132005: TEMPLATE_NOT_APPROVED, # template not approved
    132006: TEMPLATE_NOT_APPROVED,
    200: PERMISSION_ERROR,
    190: AUTHENTICATION_ERROR,     # access token invalid
    368: ACCOUNT_ERROR,            # temporarily blocked
    133016: ACCOUNT_ERROR,
}


def classify_whatsapp_failure(error_code: int | None, body: str) -> tuple[str, str]:
    if error_code is not None and error_code in _WA_CODE_MAP:
        return _WA_CODE_MAP[error_code], (body or "")[:300]
    snippet = (body or "")[:300]
    lowered = snippet.lower()
    if "rate" in lowered or "limit" in lowered:
        return RATE_LIMITED, snippet
    if "access token" in lowered or "authentication" in lowered:
        return AUTHENTICATION_ERROR, snippet
    return UNKNOWN, snippet
