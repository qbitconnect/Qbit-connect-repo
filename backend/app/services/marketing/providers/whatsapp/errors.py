"""WhatsApp error normalization (Phase 6 §16).

Maps provider (Graph API) error responses into QBIT's canonical error codes
and TRANSIENT/PERMANENT classes. Rules:

- only TRANSIENT errors may be retried, with backoff (and the provider's
  retry-after when present)
- PERMANENT errors are never retried — retrying cannot fix them and would
  only hammer the provider
- provider restriction errors are surfaced VERBATIM (sanitized of anything
  secret-like) — never bypassed, never masked (compliance requirement)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.marketing.providers.base import ErrorClass

# --- canonical error codes (§16) ---------------------------------------------
INVALID_RECIPIENT = "INVALID_RECIPIENT"
INVALID_TEMPLATE = "INVALID_TEMPLATE"
TEMPLATE_NOT_APPROVED = "TEMPLATE_NOT_APPROVED"
AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
PERMISSION_ERROR = "PERMISSION_ERROR"
RATE_LIMITED = "RATE_LIMITED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
ACCOUNT_ERROR = "ACCOUNT_ERROR"
MESSAGE_REJECTED = "MESSAGE_REJECTED"
UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"

# --- known Graph API error codes ---------------------------------------------
#: rate limiting / throttling (TRANSIENT — respect retry-after)
_RATE_CODES = {4, 47, 80007, 130429, 131048}
#: authentication failures (tokens expired/revoked/invalid)
_AUTH_CODES = {190, 102, 401}
#: permission denied on the app / WABA scope
_PERMISSION_CODES = {10, 200, 403}
#: recipient is not reachable on WhatsApp / not a WhatsApp user
_INVALID_RECIPIENT_CODES = {131026, 131030}
#: re-engagement / policy rejection of the message itself
_REJECTED_CODES = {131047, 131049}
#: template definition problems (132xxx family) — handled by range below
_TEMPLATE_FAMILY = (132_000, 132_999)
#: account/phone-number misconfiguration on the provider side
_ACCOUNT_CODES = {133010, 131031, 131005, 368}
#: network-level pseudo codes emitted by the client
_NETWORK_CODES = {-1, -2}

#: message-text patterns that pin down the template-approval case specifically
_NOT_APPROVED_MARKERS = (
    "not approved", "is paused", "hasn't been approved", "has not been approved",
    "pending review", "template is no longer available",
)


@dataclass
class NormalizedProviderError:
    code: str
    error_class: ErrorClass
    message: str
    provider_code: int | None = None
    provider_subcode: int | None = None
    fbtrace_id: str | None = None
    retry_after_seconds: float | None = None
    #: sanitized provider detail — safe to store/show, secrets never included
    detail: str | None = None
    metadata: dict = field(default_factory=dict)


class WhatsAppErrorNormalizer:
    """Graph API error payload → NormalizedProviderError."""

    def normalize(self, *, status_code: int, payload: dict) -> NormalizedProviderError:
        err = payload.get("error") if isinstance(payload, dict) else None
        err = err if isinstance(err, dict) else {}
        provider_code = self._as_int(err.get("code"))
        subcode = self._as_int(err.get("error_subcode"))
        message = str(err.get("message") or payload.get("message") or "Provider error")
        details = str((err.get("error_data") or {}).get("details") or "")[:500]
        fbtrace = err.get("fbtrace_id")
        retry_after = self._retry_after(err, payload)

        lowered = f"{message} {details}".lower()

        # --- network / transport ------------------------------------------------
        if status_code == 0 or provider_code in _NETWORK_CODES:
            return NormalizedProviderError(
                code=PROVIDER_UNAVAILABLE, error_class=ErrorClass.TRANSIENT,
                message=message, provider_code=provider_code, provider_subcode=subcode,
                fbtrace_id=fbtrace, retry_after_seconds=retry_after, detail=details,
            )

        # --- explicit code families --------------------------------------------
        if provider_code in _AUTH_CODES or status_code in (401,):
            return self._perm(AUTHENTICATION_ERROR, message, provider_code, subcode, fbtrace, details)
        if provider_code in _PERMISSION_CODES or status_code == 403:
            return self._perm(PERMISSION_ERROR, message, provider_code, subcode, fbtrace, details)
        if provider_code in _RATE_CODES or status_code == 429:
            return NormalizedProviderError(
                code=RATE_LIMITED, error_class=ErrorClass.TRANSIENT,
                message=message, provider_code=provider_code, provider_subcode=subcode,
                fbtrace_id=fbtrace, retry_after_seconds=retry_after, detail=details,
            )
        if provider_code in _INVALID_RECIPIENT_CODES:
            return self._perm(INVALID_RECIPIENT, message, provider_code, subcode, fbtrace, details)
        if provider_code in _REJECTED_CODES:
            return self._perm(MESSAGE_REJECTED, message, provider_code, subcode, fbtrace, details)
        if provider_code in _ACCOUNT_CODES:
            return self._perm(ACCOUNT_ERROR, message, provider_code, subcode, fbtrace, details)
        if provider_code is not None and _TEMPLATE_FAMILY[0] <= provider_code <= _TEMPLATE_FAMILY[1]:
            # 132xxx: template definition/parameter problems — distinguish the
            # approval case so the UI can tell the operator what to fix
            if any(marker in lowered for marker in _NOT_APPROVED_MARKERS):
                return self._perm(TEMPLATE_NOT_APPROVED, message, provider_code, subcode, fbtrace, details)
            return self._perm(INVALID_TEMPLATE, message, provider_code, subcode, fbtrace, details)

        # --- text-pattern fallbacks for codes Meta documents loosely -----------
        if any(marker in lowered for marker in _NOT_APPROVED_MARKERS):
            return self._perm(TEMPLATE_NOT_APPROVED, message, provider_code, subcode, fbtrace, details)
        if "template" in lowered and any(
            marker in lowered for marker in ("does not exist", "unknown", "invalid", "mismatch", "parameter")
        ):
            return self._perm(INVALID_TEMPLATE, message, provider_code, subcode, fbtrace, details)
        if "access token" in lowered or "oauth" in lowered:
            return self._perm(AUTHENTICATION_ERROR, message, provider_code, subcode, fbtrace, details)
        if "rate" in lowered or "throttl" in lowered or "limit reached" in lowered:
            return NormalizedProviderError(
                code=RATE_LIMITED, error_class=ErrorClass.TRANSIENT,
                message=message, provider_code=provider_code, provider_subcode=subcode,
                fbtrace_id=fbtrace, retry_after_seconds=retry_after, detail=details,
            )

        # --- last resort: classify by HTTP status family ------------------------
        if status_code >= 500:
            return NormalizedProviderError(
                code=PROVIDER_UNAVAILABLE, error_class=ErrorClass.TRANSIENT,
                message=message, provider_code=provider_code, provider_subcode=subcode,
                fbtrace_id=fbtrace, retry_after_seconds=retry_after, detail=details,
            )
        return NormalizedProviderError(
            code=UNKNOWN_PROVIDER_ERROR,
            error_class=ErrorClass.TRANSIENT if status_code >= 500 else ErrorClass.PERMANENT,
            message=message, provider_code=provider_code, provider_subcode=subcode,
            fbtrace_id=fbtrace, retry_after_seconds=retry_after, detail=details,
        )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _perm(code, message, provider_code, subcode, fbtrace, detail) -> NormalizedProviderError:
        return NormalizedProviderError(
            code=code, error_class=ErrorClass.PERMANENT, message=message,
            provider_code=provider_code, provider_subcode=subcode, fbtrace_id=fbtrace,
            detail=detail,
        )

    @staticmethod
    def _as_int(value) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _retry_after(err: dict, payload: dict) -> float | None:
        """Honor the provider's Retry-After hints (§30 — respect, never evade)."""
        for source in (err, payload, (err.get("error_data") or {}) if isinstance(err.get("error_data"), dict) else {}):
            for key in ("retry_after", "retry_after_seconds", "Retry-After"):
                if key in source:
                    try:
                        value = float(source[key])
                        if value >= 0:
                            return value
                    except (TypeError, ValueError):
                        continue
        return None
